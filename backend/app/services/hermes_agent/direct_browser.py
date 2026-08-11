from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import random
import re
import signal
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded


logger = logging.getLogger(__name__)

AGENT_BROWSER = "/opt/gmv/bin/agent-browser"
CDP_URL = os.getenv("HERMES_CDP_URL", "http://127.0.0.1:9222")
PACING_SCOPE = "default-project"
VISUAL_STAGES = {"VISUAL_PREVIEW", "FINAL_ASSETS"}
DEFAULT_CHATGPT_URL = "https://chatgpt.com/"
NORMAL_CHAT_STAGES = VISUAL_STAGES
STAGE_ROLE_PROMPTS = {
    "FACTS": "Act as a product-facts researcher. Build a traceable, project-isolated product truth packet from the supplied files and brief.",
    "VISUAL_PREVIEW": "Act as an AI art director. Generate the requested ordered reference-board sequence with consistent fictional characters, scene, actions, and exact product packaging.",
    "CREATIVE_REVIEW": "Act only as a visual acceptance inspector. Compare visible pixels with the signed reference plan; never rewrite story, copy, conversion, audio, or production direction.",
    "FINAL_ASSETS": "Act as an AI art director producing or repairing the approved ordered reference board for splitting.",
    "EDIT_PACKAGE": "Act as a short-form creative director writing concise, per-video editor guidance and publishing copy.",
}


class ChatGPTStageError(RuntimeError):
    def __init__(self, message: str, *, raw_text: str = "", chat_url: str | None = None) -> None:
        super().__init__(message)
        self.raw_text = raw_text
        self.chat_url = chat_url


def _session_name() -> str:
    return "hermes-cdp-" + "".join(character if character.isalnum() else "-" for character in CDP_URL)[-48:]


def _agent_browser_runtime_dir(owner_uid: int | None = None, *, create: bool = True) -> Path:
    """Return a host-visible runtime directory for one Unix owner.

    Celery workers run with ``PrivateTmp=yes``.  A lock under ``/tmp`` is
    therefore invisible to the root maintenance process and to workers in
    another systemd unit.  Keep locks and activity markers below ``/run/gmv``
    so every execution path observes the same browser-session lease.
    """
    uid = os.getuid() if owner_uid is None else int(owner_uid)
    base = Path(os.getenv("HERMES_AGENT_BROWSER_RUNTIME_DIR", "/run/gmv/agent-browser-runtime"))
    path = base / str(uid)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _agent_browser_activity_path(session: str, owner_uid: int | None = None) -> Path:
    return _agent_browser_runtime_dir(owner_uid) / f"{session}.activity"


def _acquire_browser_stage_lease(session: str):
    """Hold a host-visible lease for the complete browser-stage lifetime.

    The native client daemonizes, so its parent becomes PID 1 even while a
    healthy Celery stage is still using it.  The orphan reaper therefore
    cannot infer liveness from the daemon's parent process.  A flock survives
    normal prefork execution and is released automatically if the worker is
    killed, giving the reaper an authoritative lifecycle boundary.
    """
    import fcntl

    path = _agent_browser_runtime_dir() / f"{session}.stage.lock"
    lease = path.open("a+", encoding="utf-8")
    fcntl.flock(lease.fileno(), fcntl.LOCK_EX)
    lease.seek(0)
    lease.truncate()
    lease.write(str(os.getpid()))
    lease.flush()
    return lease


def _touch_agent_browser_activity(session: str) -> None:
    """Best-effort activity marker used by the orphan reaper."""
    try:
        _agent_browser_activity_path(session).touch(exist_ok=True)
    except OSError as exc:
        # Browser operation remains authoritative.  A missing marker merely
        # delays cleanup until the stage-finally hook runs.
        logger.warning("Could not update agent-browser activity for %s: %s", session, exc)


def _navigation_timeout() -> int:
    return int(os.getenv("HERMES_BROWSER_NAVIGATION_TIMEOUT_SECONDS", "60"))


def _rate_limit_marker(value: str) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in (
        "request too frequent", "too many requests", "rate limit", "rate limited",
        "try again in a few minutes", "please wait a few minutes",
        "请求过于频繁", "请稍等几分钟", "暂时限制你访问",
    )) or _quota_limit_marker(text)


def _quota_limit_marker(value: str) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in (
        "plan limit for image generation",
        "plan limit for image generations",
        "image generation limit",
        "image generations requests",
        "create more images when the limit resets",
        "you've hit the plus plan limit",
        "you have hit the plus plan limit",
        "usage limit",
        "reached your limit",
        "chatgpt_upload_limit",
        "once最多可上传 0 个文件",
        "一次最多可上传 0 个文件",
        "最多可上传 0 个文件",
        "maximum of 0 files",
        "upload up to 0 files",
        "额度",
        "限额",
    ))


def _pacing_state_path() -> Path:
    # A Chrome slot may be reused by another project after completion. Human
    # pacing and learned cooldowns belong to the project that observed them;
    # inheriting the previous project's timer makes a fresh project appear
    # stalled even though it has never sent a request.
    route = f"{CDP_URL or 'default'}|{PACING_SCOPE or 'default-project'}"
    digest = hashlib.sha256(route.encode("utf-8", errors="ignore")).hexdigest()[:20]
    default_root = f"/tmp/hermes-browser-pacing-{os.getuid()}"
    root = Path(os.getenv("HERMES_BROWSER_PACING_STATE_DIR", default_root))
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not os.access(root, os.W_OK):
            raise PermissionError(f"pacing directory is not writable: {root}")
    except OSError:
        # A legacy root-owned /tmp directory must never silently disable
        # project-scoped pacing for the unprivileged browser worker.
        root = Path(default_root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root / f"{digest}.json"


def _load_pacing_state() -> dict[str, Any]:
    try:
        value = json.loads(_pacing_state_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_pacing_state(value: dict[str, Any]) -> None:
    path = _pacing_state_path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _explicit_rate_limit_seconds(text: str) -> int:
    value = str(text or "")
    units = (
        (r"(\d+)\s*(?:hours?|hrs?|小时)", 3600),
        (r"(\d+)\s*(?:minutes?|mins?|分钟)", 60),
        (r"(\d+)\s*(?:seconds?|secs?|秒)", 1),
    )
    total = 0
    for pattern, multiplier in units:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            total += int(match.group(1)) * multiplier
    return max(30, total) if total else 0


def _record_rate_limit(text: str) -> int:
    now = time.time()
    state = _load_pacing_state()
    previous = max(0, int(state.get("learned_cooldown_seconds") or 0))
    explicit = _explicit_rate_limit_seconds(text)
    consecutive = max(0, int(state.get("consecutive_rate_limits") or 0)) + 1
    if explicit:
        learned = explicit
    elif previous:
        learned = int(previous * min(2.0, 1.35 + consecutive * 0.1))
    else:
        learned = 300
    learned = max(120, min(86400 if explicit else 14400, learned))
    jitter = (
        random.randint(10, max(20, min(90, learned // 50)))
        if explicit
        else random.randint(max(10, learned // 20), max(20, learned // 8))
    )
    retry_after = learned + jitter
    state.update({
        "consecutive_rate_limits": consecutive,
        "learned_cooldown_seconds": learned,
        "last_rate_limit_at": now,
        "cooldown_until": now + retry_after,
        "last_rate_limit_text": str(text or "")[:1000],
    })
    _save_pacing_state(state)
    return retry_after


def _record_chatgpt_success() -> None:
    now = time.time()
    state = _load_pacing_state()
    last_rate = float(state.get("last_rate_limit_at") or 0)
    if last_rate > 0:
        observed = max(60, min(14400, int(now - last_rate)))
        previous = max(0, int(state.get("learned_cooldown_seconds") or 0))
        state["learned_cooldown_seconds"] = observed if not previous else int(previous * 0.65 + observed * 0.35)
    state["consecutive_rate_limits"] = 0
    state["cooldown_until"] = 0
    state["last_success_at"] = now
    _save_pacing_state(state)


def _pace_before_send(
    prompt_length: int, attachment_count: int, *, packet: dict[str, Any] | None = None,
) -> None:
    """Apply account/route pacing without pretending to be a different user."""
    now = time.time()
    state = _load_pacing_state()
    cooldown_until = float(state.get("cooldown_until") or 0)
    if cooldown_until > now:
        remaining = max(30, int(cooldown_until - now))
        raise RuntimeError(f"CHATGPT_RATE_LIMIT_COOLDOWN_ACTIVE: retry_after_seconds={remaining}")
    minimum = max(8, int(os.getenv("HERMES_CHATGPT_MIN_SEND_INTERVAL_SECONDS", "24")))
    learned = max(0, int(state.get("learned_send_interval_seconds") or 0))
    target_interval = max(minimum, learned)
    last_send = float(state.get("last_send_at") or 0)
    wait_for_interval = max(0.0, last_send + target_interval - now)
    reading_pause = min(8.0, 1.5 + max(0, prompt_length) / 5000.0 + max(0, attachment_count) * 0.35)
    if packet:
        reading_pause = max(
            reading_pause,
            _request_pacing_delay_seconds(
                packet,
                prompt_length=prompt_length,
                attachment_count=attachment_count,
            ),
        )
    jitter = random.uniform(1.0, 4.0)
    time.sleep(wait_for_interval + reading_pause + jitter)
    sent_at = time.time()
    state["last_send_at"] = sent_at
    state["last_send_prompt_length"] = int(prompt_length)
    state["last_send_attachment_count"] = int(attachment_count)
    state["learned_send_interval_seconds"] = target_interval
    _save_pacing_state(state)


def _request_pacing_delay_seconds(
    packet: dict[str, Any], *, prompt_length: int, attachment_count: int,
    now: datetime | None = None,
) -> float:
    """Calculate review/reservation delay for one project request."""
    current = now or datetime.now()
    not_before_raw = str(packet.get("chatgpt_send_not_before") or "").strip()
    if not_before_raw:
        try:
            not_before = datetime.fromisoformat(not_before_raw)
        except ValueError:
            not_before = None
        if not_before is not None and not_before > current:
            return max(0.0, (not_before - current).total_seconds())
    stage = str(packet.get("current_stage") or "").upper()
    base = 5.0 + min(12.0, max(0, int(prompt_length)) / 1200.0)
    attachments = max(0, int(attachment_count)) * 1.5
    if stage in VISUAL_STAGES:
        base += 4.0
    return base + attachments


def _acquire_browser_lock(
    session: str,
    *,
    wait_seconds: int | None = None,
    owner_uid: int | None = None,
):
    import fcntl

    wait = wait_seconds
    if wait is None:
        wait = int(os.getenv("HERMES_BROWSER_LOCK_TIMEOUT_SECONDS", "75"))
    configured_lock_dir = str(os.getenv("HERMES_BROWSER_LOCK_DIR") or "").strip()
    lock_dir = (
        Path(configured_lock_dir)
        if configured_lock_dir
        else _agent_browser_runtime_dir(owner_uid)
    )
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = (lock_dir / f"{session}.lock").open("a+", encoding="utf-8")
    deadline = time.monotonic() + max(1, wait)
    while True:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(str(os.getpid()))
            lock_file.flush()
            return lock_file
        except BlockingIOError:
            if time.monotonic() >= deadline:
                lock_file.close()
                raise TimeoutError(f"Browser command lock timed out for {session}")
            time.sleep(0.5)


def _release_browser_lock(lock_file) -> None:
    try:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def _agent_browser_state_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".agent-browser"


def _agent_browser_daemon_matches(pid: int, session: str) -> bool:
    if pid <= 1:
        return False
    process_root = Path("/proc") / str(pid)
    try:
        command = (process_root / "cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", errors="ignore",
        )
        environment = (process_root / "environ").read_bytes().split(b"\0")
    except OSError:
        return False
    values = {
        item.split(b"=", 1)[0].decode("utf-8", errors="ignore"):
        item.split(b"=", 1)[1].decode("utf-8", errors="ignore")
        for item in environment if b"=" in item
    }
    return (
        "agent-browser" in command
        and values.get("AGENT_BROWSER_DAEMON") == "1"
        and values.get("AGENT_BROWSER_SESSION") == session
    )


def _agent_browser_daemon_pids(
    session: str,
    *,
    owner_uid: int | None = None,
) -> list[int]:
    """Enumerate every matching daemon instead of trusting one PID file.

    ``agent-browser`` writes one PID file per session.  When a newer daemon
    replaces that file, the older daemon becomes unaddressable through its
    state directory and used to survive indefinitely.
    """
    expected_uid = os.getuid() if owner_uid is None else int(owner_uid)
    matches: list[int] = []
    for process_root in Path("/proc").iterdir():
        if not process_root.name.isdigit():
            continue
        try:
            if process_root.stat().st_uid != expected_uid:
                continue
        except OSError:
            continue
        pid = int(process_root.name)
        if _agent_browser_daemon_matches(pid, session):
            matches.append(pid)
    return sorted(set(matches))


def _terminate_agent_browser_daemons(pids: list[int]) -> list[int]:
    targets = sorted(set(int(pid) for pid in pids if int(pid) > 1))
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.warning("Could not terminate agent-browser daemon pid=%s: %s", pid, exc)

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not any(Path(f"/proc/{pid}").exists() for pid in targets):
            break
        time.sleep(0.1)

    stopped: list[int] = []
    for pid in targets:
        if Path(f"/proc/{pid}").exists():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                logger.warning("Could not kill agent-browser daemon pid=%s: %s", pid, exc)
                continue
        stopped.append(pid)
    return stopped


def _reset_agent_browser_daemon(session: str, *, reason: str = "recoverable_failure") -> bool:
    """Drop local CDP clients without closing the remote Windows Chrome slot."""
    state_dir = _agent_browser_state_dir()
    pid_path = state_dir / f"{session}.pid"
    pid = 0
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass

    daemon_pids = _agent_browser_daemon_pids(session)
    if pid > 1 and _agent_browser_daemon_matches(pid, session):
        daemon_pids.append(pid)
    stopped_pids = _terminate_agent_browser_daemons(daemon_pids)

    for suffix in ("sock", "pid", "stream", "version", "engine"):
        try:
            (state_dir / f"{session}.{suffix}").unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove stale agent-browser state %s.%s: %s", session, suffix, exc)
    log = logger.info if reason == "stage_complete" else logger.warning
    log(
        "Reset agent-browser daemon for %s (reason=%s, state_pid=%s, matched=%s, stopped=%s)",
        session, reason, pid or "unknown", sorted(set(daemon_pids)), stopped_pids,
    )
    return bool(stopped_pids)


def close_agent_browser_session_best_effort(session: str | None = None) -> bool:
    """Close the local daemon at the browser-stage lifecycle boundary."""
    target = str(session or _session_name())
    lock_file = None
    try:
        lock_file = _acquire_browser_lock(target, wait_seconds=15)
        stopped = _reset_agent_browser_daemon(target, reason="stage_complete")
        try:
            _agent_browser_activity_path(target).unlink(missing_ok=True)
        except OSError:
            pass
        return stopped
    except Exception as exc:
        logger.warning("Could not close agent-browser session %s: %s", target, exc)
        return False
    finally:
        if lock_file is not None:
            _release_browser_lock(lock_file)


def _recoverable_agent_browser_error(error: BaseException) -> bool:
    if isinstance(error, TimeoutError):
        return True
    text = str(error or "").lower()
    return any(marker in text for marker in (
        "page.enable",
        "cdp command timed out",
        "browser command timed out",
        "failed to connect to cdp",
        "websocket connect failed",
        "connection refused",
        "server disconnected without sending a response",
    ))


def _run(*args: str, timeout: int = 180, isolated: bool = True) -> dict[str, Any]:
    session = _session_name()
    command = [AGENT_BROWSER]
    if isolated:
        command.extend(["--session", session])
    command.extend(["--cdp", CDP_URL, "--json", *args])
    lock_file = _acquire_browser_lock(session)
    _touch_agent_browser_activity(session)
    try:
        for daemon_attempt in range(2):
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except Exception:
                    process.kill()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except Exception:
                    stdout, stderr = "", ""
                detail = (stderr or stdout or "timeout").strip()
                error: BaseException = TimeoutError(
                    f"Browser command timed out after {timeout}s: {' '.join(args[:3])}; {detail[:500]}"
                )
                if isolated and daemon_attempt == 0:
                    _reset_agent_browser_daemon(session)
                    continue
                raise error from exc
            except BaseException:
                # Celery delivers its soft time limit as an asynchronous exception.
                # Never leave agent-browser or its CDP children alive after the task
                # has relinquished the per-slot lock.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except Exception:
                    process.kill()
                try:
                    process.communicate(timeout=5)
                except Exception:
                    pass
                raise

            raw = (stdout or stderr or "").strip()
            try:
                payload = json.loads(raw)
            except ValueError as exc:
                error = RuntimeError(f"Browser command returned invalid JSON: {raw[:1000]}")
                if isolated and daemon_attempt == 0 and _recoverable_agent_browser_error(error):
                    _reset_agent_browser_daemon(session)
                    continue
                raise error from exc
            if process.returncode or not payload.get("success"):
                error = RuntimeError(str(payload.get("error") or raw)[:2000])
                if isolated and daemon_attempt == 0 and _recoverable_agent_browser_error(error):
                    _reset_agent_browser_daemon(session)
                    continue
                raise error
            return payload
        raise RuntimeError("Browser command failed after rebuilding its local CDP session")
    finally:
        _touch_agent_browser_activity(session)
        _release_browser_lock(lock_file)


def _eval(expression: str, *, isolated: bool = True) -> Any:
    payload = _run("eval", expression, isolated=isolated)
    value = (payload.get("data") or {}).get("result")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _eval_timeout(expression: str, *, timeout: int = 30, isolated: bool = True) -> Any:
    payload = _run("eval", expression, timeout=timeout, isolated=isolated)
    value = (payload.get("data") or {}).get("result")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _list_tabs() -> list[dict[str, Any]]:
    try:
        payload = _run("tab", "list", timeout=30)
        tabs = (payload.get("data") or {}).get("tabs") or []
        return [tab for tab in tabs if isinstance(tab, dict)]
    except Exception:
        return []


def _activate_tab(tab_id: str) -> bool:
    if not tab_id:
        return False
    try:
        _run("tab", tab_id, timeout=30)
        return True
    except Exception:
        return False


def _conversation_record_score(record: dict[str, Any]) -> tuple[int, int, int]:
    text = str(record.get("text") or "")
    generations = [
        int(value)
        for value in re.findall(r"::visual-generation:(\d+)", text)
        if str(value).isdigit()
    ]
    return (
        max(generations, default=0),
        1 if "BROWSER REQUEST MARKER" in text else 0,
        min(len(text), 99999),
    )


def _canonical_conversation_records(records: Any) -> list[dict[str, Any]]:
    """Collapse ChatGPT's adjacent React render branches into logical turns."""

    canonical: list[dict[str, Any]] = []
    for raw in list(records or []):
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        record = dict(raw)
        record["role"] = role
        record["text"] = str(record.get("text") or "")
        record["generatedImages"] = list(dict.fromkeys(
            str(value)
            for value in list(record.get("generatedImages") or [])
            if str(value).strip()
        ))
        if not canonical or canonical[-1].get("role") != role:
            canonical.append(record)
            continue
        previous = canonical[-1]
        images = list(dict.fromkeys(
            list(previous.get("generatedImages") or [])
            + list(record.get("generatedImages") or [])
        ))
        preferred = record if _conversation_record_score(record) >= _conversation_record_score(previous) else previous
        merged = dict(preferred)
        merged["generatedImages"] = images
        canonical[-1] = merged
    return canonical


def _page_state(*, isolated: bool = True) -> dict[str, Any]:
    expression = r'''JSON.stringify((() => {
      const roleMessages = [...document.querySelectorAll('[data-message-author-role="assistant"]')];
      const turnMessages = [...document.querySelectorAll('[data-turn="assistant"]')];
      const roleUserMessages = [...document.querySelectorAll('[data-message-author-role="user"]')];
      const turnUserMessages = [...document.querySelectorAll('[data-turn="user"]')];
      // ChatGPT has shipped both message-role and turn-based DOMs. Prefer the
      // inner role nodes when present to avoid counting each turn twice.
      const messages = roleMessages.length ? roleMessages : turnMessages;
      const userMessages = roleUserMessages.length ? roleUserMessages : turnUserMessages;
      const mainText = document.querySelector('main')?.innerText || '';
      const bodyText = document.body?.innerText || '';
      // ChatGPT renders notices in a portal outside <main>. Using mainText ||
      // bodyText made visible rate-limit dialogs invisible whenever <main>
      // existed, so the acknowledgement button was never reached.
      const fallbackText = `${bodyText}\n${mainText}`;
      // The user's own stage packet contains schema_version/stage/result too.
      // Whole-page text therefore cannot prove that ChatGPT answered.
      const rawAssistantMessageTexts = messages.map(message => message.innerText || '');
      const rawUserMessageTexts = userMessages.map(message => message.innerText || '');
          const imageRecords = [...document.querySelectorAll('img')]
        .filter(img => {
          const rect = img.getBoundingClientRect();
          const src = img.currentSrc || img.src || img.alt || '';
          const alt = (img.alt || '').toLowerCase();
          const generated = alt.includes('\u5df2\u751f\u6210\u56fe\u7247') || alt.includes('generated image');
          const displayed = rect.width >= 160 && rect.height >= 120;
          const chromeUi = /avatar|profile|logo|icon|sprite/i.test(src + ' ' + alt);
          // Generated-image nodes can be briefly present before their lazy
          // bitmap has decoded, and ChatGPT may virtualize them out again on
          // the next render. The explicit generated alt text is authoritative
          // even while naturalWidth/naturalHeight are still zero.
          const decodedMedia = img.naturalWidth >= 256 && img.naturalHeight >= 180 && displayed;
          return !chromeUi && (generated || decodedMedia);
        })
        .map(img => {
          const src = img.currentSrc || img.src || img.alt || '';
          const alt = (img.alt || '').toLowerCase();
          const assistant = !!img.closest('[data-message-author-role="assistant"]');
          const turnAssistant = !!img.closest('[data-turn="assistant"]');
          const user = !!img.closest('[data-message-author-role="user"]');
          const turnUser = !!img.closest('[data-turn="user"]');
          const explicitGenerated = alt.includes('\u5df2\u751f\u6210\u56fe\u7247') || alt.includes('generated image');
          const generated = explicitGenerated || (!(user || turnUser) && (assistant || turnAssistant));
          return {src, generated, assistant, turnAssistant, user, turnUser};
        })
        .filter(item => item.src);
      const uniqueImages = [...new Set(imageRecords.map(item => item.src))];
      const generatedImages = [...new Set(imageRecords.filter(item => item.generated).map(item => item.src))];
      const userImages = [...new Set(imageRecords.filter(item => item.user).map(item => item.src))];
      // ChatGPT can render expanded/collapsed copies under different nested
      // data-message-id nodes. Extract each id's own content without descendant
      // message nodes, then collapse adjacent same-role render branches into one
      // logical turn. This preserves request/result attribution without treating
      // a React duplicate as another browser submission.
      const textWithoutNestedMessages = node => {
        const clone = node.cloneNode(true);
        [...clone.querySelectorAll('[data-message-id]')].forEach(child => child.remove());
        return (clone.innerText || clone.textContent || '').trim();
      };
      const roleInsideMessage = node => {
        const candidates = [
          node,
          ...node.querySelectorAll(
            '[data-message-author-role="user"],[data-message-author-role="assistant"],'
            + '[data-turn="user"],[data-turn="assistant"]'
          ),
        ];
        const own = candidates.find(candidate =>
          candidate.closest('[data-message-id]') === node
          && (candidate.getAttribute('data-message-author-role') || candidate.getAttribute('data-turn'))
        );
        return own?.getAttribute('data-message-author-role')
          || own?.getAttribute('data-turn')
          || '';
      };
      const messageIdNodes = [...document.querySelectorAll('[data-message-id]')];
      const seenMessageIds = new Set();
      const idConversationRecords = messageIdNodes
        .filter(node => {
          const id = node.getAttribute('data-message-id') || '';
          if (!id || seenMessageIds.has(id)) return false;
          seenMessageIds.add(id);
          return true;
        })
        .map(node => {
          const role = roleInsideMessage(node);
          const images = [...node.querySelectorAll('img')]
            .filter(img => img.closest('[data-message-id]') === node)
            .map(img => {
              const src = img.currentSrc || img.src || img.alt || '';
              const alt = (img.alt || '').toLowerCase();
              const explicitGenerated = alt.includes('\u5df2\u751f\u6210\u56fe\u7247') || alt.includes('generated image');
              return role === 'assistant' && (explicitGenerated || src) ? src : '';
            })
            .filter(Boolean);
          return {
            _node: node,
            turnKey: node.closest('[data-testid^="conversation-turn-"]')?.getAttribute('data-testid') || '',
            messageId: node.getAttribute('data-message-id') || '',
            role,
            text: textWithoutNestedMessages(node),
            generatedImages: [...new Set(images)],
          };
        })
        .filter(record => record.role === 'user' || record.role === 'assistant');
      const rawConversationNodes = [
        ...document.querySelectorAll(
          '[data-message-author-role="user"],[data-message-author-role="assistant"],'
          + '[data-turn="user"],[data-turn="assistant"]'
        )
      ];
      const fallbackConversationRecords = rawConversationNodes.filter((node, index, all) => {
        const container = node.closest('[data-testid^="conversation-turn-"]') || node;
        return all.findIndex(candidate =>
          (candidate.closest('[data-testid^="conversation-turn-"]') || candidate) === container
        ) === index;
      }).map(node => node.closest('[data-testid^="conversation-turn-"]') || node)
        .sort((left, right) => {
          if (left === right) return 0;
          return left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
        })
        .map(message => {
          const roleNode = message.matches?.('[data-message-author-role]')
            ? message
            : message.querySelector?.('[data-message-author-role]');
          const turnNode = message.matches?.('[data-turn]')
            ? message
            : message.querySelector?.('[data-turn="user"],[data-turn="assistant"]');
          const role = roleNode?.getAttribute('data-message-author-role')
            || turnNode?.getAttribute('data-turn')
            || '';
          const images = [...message.querySelectorAll('img')]
            .map(img => img.currentSrc || img.src || img.alt || '')
            .filter(Boolean);
          return {
            _node: message,
            turnKey: message.closest('[data-testid^="conversation-turn-"]')?.getAttribute('data-testid') || '',
            role,
            text: message.innerText || '',
            generatedImages: [...new Set(images)],
          };
        })
        .filter(record => record.role === 'user' || record.role === 'assistant');
      const idTurnRoles = new Set(idConversationRecords
        .filter(record => record.turnKey)
        .map(record => `${record.turnKey}::${record.role}`));
      const fallbackOnlyRecords = fallbackConversationRecords.filter(record =>
        !record.turnKey || !idTurnRoles.has(`${record.turnKey}::${record.role}`)
      );
      const extractedConversationRecords = (idConversationRecords.length
        ? [...idConversationRecords, ...fallbackOnlyRecords]
        : fallbackConversationRecords
      ).sort((left, right) => {
        if (left._node === right._node) return 0;
        return left._node.compareDocumentPosition(right._node) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
      }).map(record => {
        const {_node, turnKey, ...serializable} = record;
        return serializable;
      });
      const recordScore = record => {
        const text = record.text || '';
        const generation = [...text.matchAll(/::visual-generation:(\d+)/g)]
          .map(match => Number(match[1]) || 0)
          .reduce((highest, value) => Math.max(highest, value), 0);
        return generation * 1000000
          + (text.includes('BROWSER REQUEST MARKER') ? 100000 : 0)
          + Math.min(text.length, 99999);
      };
      const conversationRecords = extractedConversationRecords.reduce((records, record) => {
        const previous = records.length ? records[records.length - 1] : null;
        if (!previous || previous.role !== record.role) {
          records.push(record);
          return records;
        }
        const generatedImages = [...new Set([
          ...(previous.generatedImages || []),
          ...(record.generatedImages || []),
        ])];
        const preferred = recordScore(record) >= recordScore(previous) ? record : previous;
        records[records.length - 1] = {...preferred, generatedImages};
        return records;
      }, []);
      const assistantRecords = conversationRecords.filter(record => record.role === 'assistant');
      const userRecords = conversationRecords.filter(record => record.role === 'user');
      const messageTexts = assistantRecords.map(record => record.text || '');
      const userMessageTexts = userRecords
        .filter(record => record.role === 'user')
        .map(record => record.text || '');
      const latestText = assistantRecords.length ? (assistantRecords[assistantRecords.length - 1].text || '') : '';
      const latestUserText = userRecords.length ? (userRecords[userRecords.length - 1].text || '') : '';
      const activityText = (fallbackText || '').slice(-4000);
      const rateLimitPattern = /request too frequent|too many requests|rate limit|rate limited|try again in a few minutes|please wait a few minutes|\u8bf7\u6c42\u8fc7\u4e8e\u9891\u7e41|\u8bf7\u7a0d\u7b49\u51e0\u5206\u949f|\u6682\u65f6\u9650\u5236\u4f60\u8bbf\u95ee/i;
      const quotaLimitPattern = /plan limit for image generations?|image generation limit|image generations requests|create more images when the limit resets|you(?:'| ha)ve hit the plus plan limit|usage limit|reached your limit|\u989d\u5ea6|\u9650\u989d/i;
      const isVisible = element => {
        if (!element) return false;
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0
          && rect.width > 0 && rect.height > 0;
      };
      const acknowledgementButtons = [...document.querySelectorAll('button,[role="button"]')]
        .filter(isVisible)
        .filter(item => /^(\u660e\u767d\u4e86|got it|understood|ok|okay)$/i.test(
          ((item.innerText || item.textContent || '') + ' ' + (item.getAttribute('aria-label') || '')).trim()
        ));
      const visibleDialogTexts = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"]')]
        .filter(isVisible)
        .map(item => item.innerText || item.textContent || '')
        .filter(Boolean);
      const rateLimitDialogText = visibleDialogTexts.find(text => rateLimitPattern.test(text)) || '';
      const quotaLimitText = quotaLimitPattern.test(latestText || '') ? latestText : '';
      // The visible acknowledgement control is a strong signal for portal
      // implementations that omit role=dialog. Requiring visibility avoids
      // treating old conversation text about rate limits as a live modal.
      const rateLimited = Boolean(rateLimitDialogText || quotaLimitText)
        || (acknowledgementButtons.length > 0 && rateLimitPattern.test(fallbackText || ''));
      const composer = document.querySelector('#prompt-textarea,[contenteditable="true"][role="textbox"]');
      const loginControl = [...document.querySelectorAll('a,button,[role="button"]')]
        .filter(isVisible)
        .some(item => /^(log in|sign in|\u767b\u5f55|\u767b\u5165)$/i.test(
          ((item.innerText || item.textContent || '') + ' ' + (item.getAttribute('aria-label') || '')).trim()
        ));
      const authUrl = /auth\.openai\.com|\/auth\/|email-verification|login|signin/i.test(location.href || '');
      const verificationRequired = /check your inbox|email verification|verification code|\u68c0\u67e5\u4f60\u7684\u6536\u4ef6\u7bb1|\u9a8c\u8bc1\u7801|\u4f7f\u7528\u5bc6\u7801\u7ee7\u7eed/i.test(fallbackText || '');
      // Anonymous ChatGPT still exposes a composer, but it cannot create the
      // images or accept the files required by Content Factory. A visible,
      // exact login control is therefore authoritative even when the anonymous
      // composer exists. Account-switch menu labels such as "log in to another
      // account" do not match the exact control pattern above.
      const loginRequired = loginControl || (!composer && (authUrl || verificationRequired));
      // Completed ChatGPT turns retain a static "Thinking" label. Treating
      // that historical text as live activity leaves stages running forever.
      // The stop control is the authoritative text-stream signal; only the
      // explicit image-generation placeholders are useful text fallbacks.
      const generatingMedia = /\u56fe\u50cf\u6b63\u5728\u751f\u6210|\u56fe\u7247\u6b63\u5728\u751f\u6210|\u56fe\u50cf\u51c6\u5907\u597d\u65f6\u63a5\u6536\u901a\u77e5|Get notified when (the )?image is ready/i.test(activityText);
      const stopControl = !!document.querySelector(
        '[data-testid="stop-button"], button[aria-label*="Stop"], button[aria-label*="\u505c\u6b62"]'
      );
      const busy = stopControl || (generatingMedia && generatedImages.length === 0);
      const generationFailed = [
        '\u56fe\u50cf\u751f\u6210\u5931\u8d25',
        '\u56fe\u7247\u751f\u6210\u5931\u8d25',
        '\u751f\u6210\u56fe\u7247\u65f6\u51fa\u73b0\u95ee\u9898',
        '\u65e0\u6cd5\u751f\u6210\u56fe\u7247',
        'Image generation failed', 'There was an error generating'
      ].some(text => latestText.includes(text));
      const policyRefusal = [
        '\u65e0\u6cd5\u6839\u636e\u8be5\u811a\u672c\u751f\u6210\u753b\u9762',
        '\u4e0e\u5f53\u524d\u89c6\u89c9\u89c4\u8303\u4e0d\u517c\u5bb9',
        '\u4e0d\u80fd\u5e2e\u52a9\u751f\u6210\u8be5\u753b\u9762',
        "I can't help create that image",
        'I cannot generate this image',
        'incompatible with the current visual guidelines'
      ].some(text => latestText.includes(text));
      return {
        busy,
        generationFailed,
        policyRefusal,
        loginRequired,
        rateLimited,
        // A short-lived generic dialog and a model-specific quota response can
        // coexist. Preserve both and let Python prefer the quota response,
        // which usually carries the authoritative reset duration.
        rateLimitText: rateLimited ? ([quotaLimitText, rateLimitDialogText].filter(Boolean).join('\n\n') || activityText) : '',
        quotaLimitText,
        rateLimitDialogText,
        quotaLimited: Boolean(quotaLimitText),
        rateLimitDialogVisible: Boolean(rateLimitDialogText),
        rateLimitAcknowledgementVisible: acknowledgementButtons.length > 0,
        count: assistantRecords.length,
        userCount: userRecords.length,
        imageCount: uniqueImages.length,
        images: uniqueImages,
        userImages,
        generatedImageCount: generatedImages.length,
        generatedImages,
        conversationRecords,
        rawAssistantMessageCount: rawAssistantMessageTexts.length,
        rawUserMessageCount: rawUserMessageTexts.length,
        messageTexts,
        userMessageTexts,
        latestUserText,
        text: latestText,
        url: location.href
      };
    })())'''
    value = _eval_timeout(
        expression,
        timeout=int(os.getenv("HERMES_PAGE_STATE_TIMEOUT_SECONDS", "20")),
        isolated=isolated,
    )
    if not isinstance(value, dict):
        return {}
    canonical_records = _canonical_conversation_records(value.get("conversationRecords"))
    if canonical_records:
        assistant_records = [record for record in canonical_records if record.get("role") == "assistant"]
        user_records = [record for record in canonical_records if record.get("role") == "user"]
        value["conversationRecords"] = canonical_records
        value["messageTexts"] = [str(record.get("text") or "") for record in assistant_records]
        value["userMessageTexts"] = [str(record.get("text") or "") for record in user_records]
        value["count"] = len(assistant_records)
        value["userCount"] = len(user_records)
        value["text"] = str(assistant_records[-1].get("text") or "") if assistant_records else ""
        value["latestUserText"] = str(user_records[-1].get("text") or "") if user_records else ""
    # Keep a Python-side guard as well as the DOM expression. ChatGPT has used
    # both Chinese translations ("image" -> 图像/图片) across UI releases;
    # missing one must never turn a terminal image failure into a text timeout.
    page_text = "\n".join(
        [str(value.get("text") or "")]
        + [str(item or "") for item in list(value.get("messageTexts") or [])[-3:]]
    )
    if _chatgpt_generation_failed(page_text):
        value["generationFailed"] = True
    return value


def _chatgpt_generation_failed(text: str | None) -> bool:
    value = str(text or "").lower()
    return any(marker in value for marker in (
        "图像生成失败",
        "图片生成失败",
        "生成图片时出现问题",
        "无法生成图片",
        "image generation failed",
        "there was an error generating",
    ))


def _wait_selector(selector: str, timeout_seconds: int = 90) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            payload = _run("get", "count", selector, timeout=30)
            if int((payload.get("data") or {}).get("count") or 0) > 0:
                return
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError(f"Browser element did not appear: {selector}")


def _composer_ready(timeout_seconds: int = 20) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            count = _run("get", "count", "#prompt-textarea", timeout=20)
            if int((count.get("data") or {}).get("count") or 0) > 0:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def _enter_chat_composer(stage_url: str) -> None:
    if _composer_ready(20):
        return
    try:
        _eval(r'''(() => {
          const buttons=[...document.querySelectorAll('button,a')];
          const node=buttons.find(x=>/问问 ChatGPT|Ask ChatGPT|Start chat|开始聊天|开始使用/i.test((x.innerText||'')+' '+(x.getAttribute('aria-label')||'')));
          if(node){node.click(); return true}
          return false;
        })()''')
        time.sleep(5)
    except Exception:
        pass
    if _composer_ready(20):
        return
    _run("open", "https://chatgpt.com/", timeout=_navigation_timeout())
    if not _composer_ready(60):
        _run("open", stage_url, timeout=_navigation_timeout())
        if not _composer_ready(30):
            raise TimeoutError("Browser element did not appear: #prompt-textarea")


def _clear_composer() -> None:
    try:
        _eval(r'''(() => {
          const composer = document.querySelector('form textarea')?.closest('form')
            || document.querySelector('#prompt-textarea')?.closest('form')
            || document.querySelector('[contenteditable="true"]')?.closest('form')
            || document;
          for (const button of [...composer.querySelectorAll('button')]) {
            const label=((button.getAttribute('aria-label')||'')+' '+(button.innerText||'')+' '+(button.title||'')).trim();
            if (/移除文件|删除文件|取消上传|Remove file|Remove attachment|Delete file|Cancel upload|Close|×/i.test(label)) button.click();
          }
          return true;
        })()''')
        _eval(r'''(() => {
          for (const button of [...document.querySelectorAll('button')]) {
            const label=(button.getAttribute('aria-label')||'')+' '+(button.innerText||'');
            if (/移除文件|Remove file|删除文件|Remove attachment/i.test(label)) button.click();
          }
          const editor=document.querySelector('#prompt-textarea,[contenteditable="true"][role="textbox"],div.ProseMirror');
          if (editor) {
            editor.focus();
            document.execCommand('selectAll', false, null);
            document.execCommand('delete', false, null);
            const selection = window.getSelection();
            const range = document.createRange();
            range.selectNodeContents(editor);
            selection.removeAllRanges();
            selection.addRange(range);
            document.execCommand('delete', false, null);
            editor.innerHTML = '<p><br></p>';
            editor.textContent = '';
            editor.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, cancelable:true, inputType:'deleteContentBackward'}));
            editor.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'deleteContentBackward', data:null}));
            editor.dispatchEvent(new Event('change', {bubbles:true}));
          }
          const textarea=document.querySelector('textarea');
          if (textarea) {
            const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(textarea),'value')?.set;
            setter ? setter.call(textarea, '') : textarea.value='';
            textarea.dispatchEvent(new Event('input',{bubbles:true}));
          }
          return true;
        })()''')
        time.sleep(1)
    except Exception:
        pass






def _composer_text_length() -> int:
    value = _eval(r'''(() => {
      const visible = node => {
        if (!node) return false;
        const rect = node.getBoundingClientRect();
        const style = window.getComputedStyle(node);
        return rect.width >= 120 && rect.height >= 24
          && rect.bottom > window.innerHeight * 0.45
          && rect.top < window.innerHeight + 80
          && style.display !== 'none'
          && style.visibility !== 'hidden'
          && style.opacity !== '0';
      };
      const editors = [...document.querySelectorAll('#prompt-textarea,[contenteditable="true"][role="textbox"],div.ProseMirror,textarea')]
        .filter(visible)
        .filter(node => {
          const label = ((node.getAttribute('aria-label') || '') + ' ' + (node.getAttribute('placeholder') || '')).toLowerCase();
          const text = (node.innerText || node.textContent || node.value || '').trim();
          if (text.length > 20000 && !/message|prompt|ask|send|chatgpt|输入|消息|提示/.test(label)) return false;
          return true;
        })
        .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
      const editor = editors[0] || null;
      const text=(editor?.innerText || editor?.textContent || editor?.value || '');
      return text.length;
    })()''')
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _composer_text_value() -> str:
    value = _eval(r'''(() => {
      const visible = node => {
        if (!node) return false;
        const rect = node.getBoundingClientRect();
        const style = window.getComputedStyle(node);
        return rect.width >= 120 && rect.height >= 24
          && rect.bottom > window.innerHeight * 0.45
          && rect.top < window.innerHeight + 80
          && style.display !== 'none'
          && style.visibility !== 'hidden'
          && style.opacity !== '0';
      };
      const editor = [...document.querySelectorAll('#prompt-textarea,[contenteditable="true"][role="textbox"],div.ProseMirror,textarea')]
        .filter(visible)
        .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
      if (!editor) return '';
      return editor.value ?? editor.innerText ?? editor.textContent ?? '';
    })()''')
    return str(value or "")


def _normalized_composer_text(value: str) -> str:
    # ProseMirror represents paragraph boundaries with browser-generated blank
    # lines, so innerText can contain more structural newlines than the payload.
    # Compare the exact ordered non-whitespace content while tolerating only
    # whitespace-run differences. Corrupted ids, punctuation, digits, or words
    # still fail this integrity check.
    # ChatGPT's ProseMirror editor can inject non-rendering cursor/selection
    # markers and object placeholders while React reconciles long drafts. They
    # are not user-visible prompt content and must not turn an exact draft into
    # a false integrity failure. Visible punctuation, ids, digits, and words
    # remain untouched and therefore still fail closed when corrupted.
    editor_artifacts = dict.fromkeys(
        map(
            ord,
            "\u200b\u200c\u200d\u200e\u200f\u2060\u2066\u2067\u2068\u2069\ufeff\ufffc",
        ),
        None,
    )
    text = str(value or "").replace("\xa0", " ").translate(editor_artifacts)
    return re.sub(r"\s+", " ", text, flags=re.UNICODE).strip()


def _composer_text_matches(expected: str) -> bool:
    normalized_expected = _normalized_composer_text(expected)
    try:
        normalized_actual = _normalized_composer_text(_composer_text_value())
    except Exception:
        normalized_actual = ""
    if normalized_actual == normalized_expected:
        return True
    # Some mocked or brittle editor states return an empty text payload while
    # still having the right character count after keyboard insertion. Keep the
    # exact string check strict when text is visible, but allow a length
    # backstop when capture is effectively unavailable.
    if not normalized_actual and _composer_text_length() == len(expected):
        return True
    return False


def _composer_text_difference(expected: str, actual: str) -> str:
    """Return a bounded, escaped diagnostic for an exact composer mismatch."""
    wanted = _normalized_composer_text(expected)
    rendered = _normalized_composer_text(actual)
    limit = min(len(wanted), len(rendered))
    mismatch = next((index for index in range(limit) if wanted[index] != rendered[index]), limit)
    start = max(0, mismatch - 24)
    end_wanted = min(len(wanted), mismatch + 24)
    end_rendered = min(len(rendered), mismatch + 24)

    def describe(value: str, left: int, right: int) -> str:
        window = value[left:right]
        codepoints = " ".join(f"U+{ord(character):04X}" for character in value[mismatch:mismatch + 6])
        return f"{json.dumps(window, ensure_ascii=True)} codes=[{codepoints}]"

    return (
        f"normalized_expected={len(wanted)}, normalized_actual={len(rendered)}, "
        f"mismatch_at={mismatch}, expected_window={describe(wanted, start, end_wanted)}, "
        f"actual_window={describe(rendered, start, end_rendered)}"
    )


def _prompt_text_chunks(text: str, *, max_chars: int = 2000) -> list[str]:
    """Split a long prompt without changing a single character.

    A single agent-browser command can exceed the Windows bridge/Chrome command
    payload limit. ProseMirror also loses or reorders text when multiple inserts
    race its React rerender. Keep chunks modest and prefer natural boundaries;
    the caller verifies the complete accumulated draft after every insert.
    """
    value = str(text or "")
    limit = max(256, int(max_chars or 2000))
    chunks: list[str] = []
    offset = 0
    while offset < len(value):
        end = min(len(value), offset + limit)
        if end < len(value):
            floor = offset + max(128, int(limit * 0.6))
            newline = value.rfind("\n", floor, end)
            space = value.rfind(" ", floor, end)
            boundary = max(newline, space)
            if boundary > offset:
                end = boundary + 1
        chunks.append(value[offset:end])
        offset = end
    return chunks


def _wait_for_composer_exact(
    expected: str,
    *,
    timeout: float = 12.0,
    target_length: int | None = None,
) -> bool:
    deadline = time.monotonic() + max(1.0, float(timeout))
    normalized_expected = _normalized_composer_text(expected)
    fallback_length = (
        len(expected) if target_length is None else max(len(expected), int(target_length or 0))
    )
    while time.monotonic() < deadline:
        try:
            current = _normalized_composer_text(_composer_text_value())
        except Exception:
            current = ""
        if current == normalized_expected:
            return True
        if not current and len(normalized_expected) > 0:
            try:
                if _composer_text_length() >= fallback_length:
                    return True
            except Exception:
                pass
        # A non-prefix value cannot become the expected accumulated draft through
        # a delayed React rerender. Let one short render cycle pass, then fail.
        if current and not normalized_expected.startswith(current):
            time.sleep(0.25)
            if _normalized_composer_text(_composer_text_value()) != normalized_expected:
                return False
        time.sleep(0.12)
    return False


def _insert_prompt_text_stably(text: str, *, timeout: int = 180) -> None:
    chunks = _prompt_text_chunks(
        text,
        max_chars=int(os.getenv("HERMES_PROMPT_INSERT_CHUNK_CHARS", "2000") or 2000),
    )
    accumulated = ""
    for index, chunk in enumerate(chunks, start=1):
        _run("focus", "#prompt-textarea", timeout=30)
        _run("keyboard", "inserttext", chunk, timeout=min(max(30, timeout), 90))
        accumulated += chunk
        if not _wait_for_composer_exact(accumulated, target_length=len(text)):
            actual = _composer_text_value()
            raise RuntimeError(
                "CHATGPT_PROMPT_INTEGRITY_MISMATCH: "
                f"stable chunk {index}/{len(chunks)} diverged "
                f"(expected={len(accumulated)}, actual={len(actual)}); "
                f"{_composer_text_difference(accumulated, actual)}"
            )
        if index < len(chunks):
            time.sleep(random.uniform(0.18, 0.42))


def _set_composer_text_dom(text: str) -> int:
    payload = json.dumps(text)
    value = _eval(f'''(() => {{
      const text = {payload};
      const visible = node => {{
        if (!node) return false;
        const rect = node.getBoundingClientRect();
        const style = window.getComputedStyle(node);
        return rect.width >= 120 && rect.height >= 24
          && rect.bottom > window.innerHeight * 0.45
          && rect.top < window.innerHeight + 80
          && style.display !== 'none'
          && style.visibility !== 'hidden'
          && style.opacity !== '0';
      }};
      const editor = [...document.querySelectorAll('#prompt-textarea,[contenteditable="true"][role="textbox"],div.ProseMirror,textarea,[contenteditable="true"]')]
        .filter(visible)
        .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
      if (!editor) return 0;
      editor.focus();
      if (editor.tagName === 'TEXTAREA' || editor.tagName === 'INPUT') {{
        const proto = editor.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
        if (setter) setter.call(editor, text);
        else editor.value = text;
        editor.dispatchEvent(new Event('input', {{bubbles: true, composed: true}}));
        editor.dispatchEvent(new Event('change', {{bubbles: true, composed: true}}));
        return (editor.value || '').length;
      }}
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(editor);
      selection.removeAllRanges();
      selection.addRange(range);
      let inserted = false;
      try {{
        inserted = document.execCommand('insertText', false, text);
      }} catch (error) {{
        inserted = false;
      }}
      const expected = Math.min(80, Math.max(10, Math.floor(text.length / 20)));
      if (!inserted || (editor.innerText || editor.textContent || '').length < expected) {{
        editor.innerHTML = '';
        for (const block of text.split(/\\n\\n+/)) {{
          const p = document.createElement('p');
          p.textContent = block || '\\n';
          editor.appendChild(p);
        }}
      }}
      try {{
        editor.dispatchEvent(new InputEvent('beforeinput', {{bubbles: true, composed: true, inputType: 'insertText', data: text.slice(0, 512)}}));
        editor.dispatchEvent(new InputEvent('input', {{bubbles: true, composed: true, inputType: 'insertText', data: text.slice(0, 512)}}));
      }} catch (error) {{
        editor.dispatchEvent(new Event('input', {{bubbles: true, composed: true}}));
      }}
      editor.dispatchEvent(new Event('change', {{bubbles: true, composed: true}}));
      editor.focus();
      return (editor.innerText || editor.textContent || '').length;
    }})()''')
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fill_prompt_text(text: str, *, timeout: int = 180) -> None:
    errors: list[str] = []

    # Long single-command inserts are truncated by some local bridge/Chrome
    # combinations. Sequential inserts are safe only when every accumulated
    # draft is verified after the corresponding ProseMirror render completes.
    try:
        _clear_composer_strict()
        _insert_prompt_text_stably(text, timeout=timeout)
        if _composer_text_matches(text):
            _wake_composer_for_send(text)
            if _composer_text_matches(text) and bool(_attachment_upload_state().get("sendable")):
                return
            errors.append(
                "stable inserttext was exact but React kept submit disabled"
            )
        else:
            errors.append("stable inserttext failed exact composer integrity check")
    except Exception as exc:
        errors.append(f"stable inserttext failed: {exc}")

    try:
        _clear_composer_strict()
    except Exception as exc:
        errors.append(f"clear before DOM input failed: {exc}")

    try:
        inserted_len = _set_composer_text_dom(text)
        _wake_composer_for_send(text)
        time.sleep(1)
        if _composer_text_matches(text) and bool(_attachment_upload_state().get("sendable")):
            return
        errors.append(
            "DOM editor input failed exact/application-ready check, "
            f"len={inserted_len}; {_composer_text_difference(text, _composer_text_value())}"
        )
    except Exception as exc:
        errors.append(f"DOM editor input failed: {exc}")

    if os.getenv("HERMES_ALLOW_CLIPBOARD_FALLBACK", "").strip().lower() in {"1", "true", "yes"}:
        try:
            _clear_composer_strict()
        except Exception as exc:
            errors.append(f"clear before clipboard paste failed: {exc}")

        try:
            _run("focus", "#prompt-textarea", timeout=30)
            _run("clipboard", "write", text, timeout=timeout)
            _run("clipboard", "paste", timeout=60)
            time.sleep(1)
            if _composer_text_matches(text):
                return
            errors.append("clipboard paste failed exact composer integrity check")
        except Exception as exc:
            errors.append(f"clipboard paste failed: {exc}")

    raise RuntimeError("ChatGPT composer did not retain the prompt text: " + "; ".join(errors))


def _preferred_response_text(state: dict[str, Any]) -> str:
    messages = [str(value or "").strip() for value in (state.get("messageTexts") or [])]
    for candidate in reversed(messages):
        if _looks_like_stage_request(candidate):
            continue
        cleaned = _complete_stage_json_text(candidate)
        if cleaned and all(token in cleaned for token in ('"schema_version"', '"project_id"', '"stage"', '"result"')):
            return cleaned
        if all(token in candidate for token in ('"schema_version"', '"project_id"', '"stage"', '"result"')):
            return candidate
    text = str(state.get("text") or "")
    if _looks_like_stage_request(text):
        return ""
    return _complete_stage_json_text(text) or text


def _escape_unescaped_json_string_quotes(text: str) -> str:
    """Repair dialogue quotes in an otherwise complete JSON response.

    ChatGPT occasionally emits values such as
    ``"dialogue": "She says, "Really?""``.  The browser collector must not
    classify that complete response as a truncated stream and submit the same
    stage again.  Escape only quotes that cannot terminate a JSON string; the
    task layer still performs the authoritative envelope validation.
    """
    source = str(text or "")
    output: list[str] = []
    in_string = False
    escaped = False
    length = len(source)
    for index, char in enumerate(source):
        if char == "\\" and in_string:
            output.append(char)
            escaped = not escaped
            continue
        if char == '"' and not escaped:
            if not in_string:
                in_string = True
                output.append(char)
                continue
            lookahead = index + 1
            while lookahead < length and source[lookahead] in " \t\r\n":
                lookahead += 1
            next_char = source[lookahead] if lookahead < length else ""
            if next_char in {":", ",", "}", "]", ""}:
                in_string = False
                output.append(char)
            else:
                output.append('\\"')
            continue
        escaped = False
        output.append(char)
    return "".join(output)


def _complete_stage_json_text(text: str) -> str:
    """Return the first complete JSON object embedded in ChatGPT text.

    ChatGPT's DOM text often appends copy/share/button artifacts after the
    assistant answer. Returning the balanced JSON object lets the stage finish
    instead of waiting forever for the full DOM text to end with "}".
    """
    raw = str(text or "").replace("\u00a0", " ")
    if not raw:
        return ""
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char != "{":
            continue
        tail = raw[index:]
        candidates = [tail]
        repaired_tail = _escape_unescaped_json_string_quotes(tail)
        if repaired_tail != tail:
            candidates.append(repaired_tail)
        for candidate in candidates:
            try:
                value, end = decoder.raw_decode(candidate)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            snippet = candidate[:end].strip()
            if all(token in snippet for token in ('"schema_version"', '"project_id"', '"stage"', '"result"')):
                return snippet
    return ""


def _looks_like_stage_request(text: str) -> bool:
    compact = str(text or "").replace("\u00a0", " ").lower()
    if not compact:
        return False
    request_markers = (
        "stage packet",
        "execute exactly this content-factory stage",
        "execute this content-factory visual stage",
        "required_result_fields",
        "required_next_stage",
        "current_stage",
        "browser_asset_paths",
        "previous_outputs",
        "do not publish, purchase",
        "end with one complete json object",
    )
    return any(marker in compact for marker in request_markers)


def _looks_like_incomplete_stage_json(text: str, project_id: str, stage: str) -> bool:
    compact = str(text or "").replace("\u00a0", " ").strip()
    if not compact or _looks_like_stage_request(compact):
        return False
    if not compact.startswith("{"):
        return False
    # Streaming can temporarily expose only ``{"schema_version":``. Requiring
    # project/stage/result tokens before classifying it as incomplete causes
    # that early fragment to be treated as a final non-JSON answer and the
    # project resubmits while ChatGPT is still finishing the original turn.
    # Any assistant turn that starts as JSON and is structurally open must keep
    # waiting, even before its identifying fields have streamed into the DOM.
    in_string = False
    escape = False
    depth = 0
    for char in compact:
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
    return in_string or depth > 0 or not compact.rstrip().endswith("}")


def _looks_like_image_analysis_placeholder(text: str) -> bool:
    """Recognize ChatGPT's transient vision-analysis assistant turn.

    The vision UI can remove its stop button while retaining a short
    ``Analyzing images`` placeholder. That turn is not an answer and must not
    start non-JSON or truncated-response timers.
    """
    compact = re.sub(r"\s+", " ", str(text or "").replace("\u00a0", " ")).strip()
    if not compact:
        return False
    return bool(
        re.fullmatch(
            r"(?:正在)?分析\s*(?:\d+\s*)?(?:幅|张)?\s*图片(?:中|…|\.\.\.)?",
            compact,
            flags=re.IGNORECASE,
        )
        or re.fullmatch(
            r"(?:still\s+)?analy[sz]ing\s+(?:the\s+)?(?:\d+\s+)?(?:uploaded\s+)?images?(?:…|\.\.\.)?",
            compact,
            flags=re.IGNORECASE,
        )
    )


def _looks_like_response_processing_placeholder(text: str) -> bool:
    """Recognize short live-status turns that are not assistant answers."""
    if _looks_like_image_analysis_placeholder(text):
        return True
    compact = re.sub(r"\s+", " ", str(text or "").replace("\u00a0", " ")).strip()
    return bool(
        re.fullmatch(
            r"(?:正在思考|思考中|正在处理|处理中)(?:…|\.\.\.)?",
            compact,
            flags=re.IGNORECASE,
        )
        or re.fullmatch(
            r"(?:thinking|still thinking|working|still working|processing)(?:…|\.\.\.)?",
            compact,
            flags=re.IGNORECASE,
        )
    )


def _dismiss_rate_limit_acknowledgement() -> bool:
    try:
        dismissed = bool(_eval(r'''(() => {
          const isVisible = element => {
            if (!element) return false;
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0
              && rect.width > 0 && rect.height > 0;
          };
          const buttons = [...document.querySelectorAll('button,[role="button"]')];
          const button = buttons.find(item => isVisible(item) && /^(\u660e\u767d\u4e86|got it|understood|ok|okay)$/i.test(
            ((item.innerText || item.textContent || '') + ' ' + (item.getAttribute('aria-label') || '')).trim()
          ));
          if (!button) return false;
          button.scrollIntoView({block: 'center', inline: 'center'});
          button.focus();
          button.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
          button.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
          button.click();
          return true;
        })()'''))
        if dismissed:
            time.sleep(0.5)
        return dismissed
    except Exception:
        return False


def _dismiss_nonblocking_chatgpt_overlays() -> int:
    """Close upgrade/promotion notices that cover the composer.

    These notices are not execution failures and often aren't rendered with a
    dialog role. Only close containers whose own text matches a known product
    promotion, so conversation controls and user content are left untouched.
    """
    try:
        dismissed = int(_eval(r'''(() => {
          const visible = element => {
            if (!element) return false;
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden'
              && Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
          };
          const promo = /提高结构化工作的准确性|升级套餐|获取\s*Pro|升级到\s*Pro|Get\s*Pro|Try\s*Pro|Upgrade(?:\s+to)?\s+Pro|unlock\s+pro/i;
          const viewportArea = Math.max(1, innerWidth * innerHeight);
          const candidates = [...document.querySelectorAll('[role="dialog"],[role="status"],[aria-modal="true"],[data-radix-portal] > *,[class*="toast" i],[class*="popover" i],body *')]
            .filter(visible)
            .filter(node => {
              const text = (node.innerText || node.textContent || '').slice(0, 1200);
              if (!promo.test(text)) return false;
              const rect = node.getBoundingClientRect();
              const positioned = ['fixed', 'sticky'].includes(getComputedStyle(node).position)
                || node.matches('[role="dialog"],[role="status"],[aria-modal="true"],[data-radix-portal] > *');
              return positioned && rect.width * rect.height < viewportArea * 0.75;
            })
            .sort((left, right) => {
              const leftRect = left.getBoundingClientRect();
              const rightRect = right.getBoundingClientRect();
              return (leftRect.width * leftRect.height) - (rightRect.width * rightRect.height);
            });
          let count = 0;
          for (const container of candidates) {
            const buttons = [...container.querySelectorAll('button,[role="button"]')].filter(visible);
            const close = buttons.find(button => /^(close|dismiss|关闭|取消|×|x)$/i.test(
              ((button.getAttribute('aria-label') || '') + ' ' + (button.title || '') + ' ' + (button.innerText || button.textContent || '')).trim()
            )) || buttons.find(button => {
              const label = ((button.getAttribute('aria-label') || '') + ' ' + (button.title || '')).trim();
              return /close|dismiss|关闭/i.test(label) || (button.querySelector('svg') && !(button.innerText || '').trim());
            });
            if (!close) continue;
            close.click();
            count += 1;
          }
          return count;
        })()''') or 0)
        if dismissed:
            time.sleep(0.4)
        return dismissed
    except Exception:
        return 0


def _chatgpt_interruption_state() -> dict[str, Any]:
    """Classify visible ChatGPT overlays and dismiss only harmless ones."""
    value = _eval_timeout(r'''(() => {
      const visible = node => {
        if (!node) return false;
        const style = getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden'
          && Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
      };
      const nodes = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"],[role="status"],[data-radix-portal] > *')]
        .filter(visible);
      const node = nodes[nodes.length - 1];
      if (!node) return {present:false, dismissed:false, blocked:'', text:'', control:''};
      const text = (node.innerText || node.textContent || '').trim().slice(0, 2000);
      let blocked = '';
      if (/usage limit|reached (your|the) limit|try again later|额度|限额/i.test(text)) blocked = 'quota';
      else if (/log in|sign in|session expired|登录|会话已过期/i.test(text)) blocked = 'login';
      else if (/upload limit|too many files|文件上传.*限制|上传.*上限/i.test(text)) blocked = 'upload_limit';
      if (blocked) return {present:true, dismissed:false, blocked, text, control:''};
      const safe = /^(not now|maybe later|later|no thanks|close|dismiss|cancel|got it|ok|okay|稍后|不用了|关闭|取消|知道了|×|x)$/i;
      const controls = [...node.querySelectorAll('button,[role="button"]')].filter(visible);
      const button = controls.find(item => safe.test(
        ((item.innerText || item.textContent || '') + ' ' + (item.getAttribute('aria-label') || '') + ' ' + (item.title || '')).trim()
      )) || controls.find(item => item.querySelector('svg') && !(item.innerText || '').trim());
      if (!button) return {present:true, dismissed:false, blocked:'', text, control:''};
      const control = ((button.innerText || button.textContent || '') + ' ' + (button.getAttribute('aria-label') || '')).trim();
      button.click();
      return {present:true, dismissed:true, blocked:'', text, control};
    })()''', timeout=20)
    return value if isinstance(value, dict) else {
        "present": False, "dismissed": False, "blocked": "", "text": "", "control": "",
    }


def _dismiss_chatgpt_interruptions(*, rounds: int = 3) -> dict[str, Any]:
    final: dict[str, Any] = {"present": False, "dismissed": False, "blocked": "", "text": ""}
    error_codes = {
        "quota": "CHATGPT_QUOTA_LIMIT",
        "login": "CHATGPT_SESSION_LOGIN_REQUIRED",
        "upload_limit": "CHATGPT_UPLOAD_LIMIT",
    }
    for _ in range(max(1, int(rounds))):
        final = _chatgpt_interruption_state()
        blocked = str(final.get("blocked") or "")
        if blocked:
            raise ChatGPTStageError(
                f"{error_codes.get(blocked, 'CHATGPT_BLOCKING_INTERRUPTION')}: {str(final.get('text') or '')[:500]}",
                raw_text=str(final.get("text") or ""),
            )
        if not final.get("present") or not final.get("dismissed"):
            return final
        time.sleep(0.4)
    return final


def _acknowledge_rate_limit_popup() -> bool:
    value = _eval_timeout(r'''(() => {
      const visible = node => {
        if (!node) return false;
        const style = getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
      };
      const ratePattern = /request too frequent|too many requests|rate limit|try again in a few minutes|\u8bf7\u6c42\u8fc7\u4e8e\u9891\u7e41|\u8bf7\u7a0d\u7b49\u51e0\u5206\u949f/i;
      const labels = /^(got it|understood|ok|okay|\u660e\u767d\u4e86)$/i;
      const dialogs = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"]')].filter(visible);
      const dialog = dialogs.find(node => ratePattern.test(node.innerText || node.textContent || ''));
      if (!dialog) return {found:false, clicked:false};
      const button = [...dialog.querySelectorAll('button,[role="button"]')].filter(visible).find(node => labels.test(
        ((node.innerText || node.textContent || '') + ' ' + (node.getAttribute('aria-label') || '')).trim()
      ));
      if (!button) return {found:true, clicked:false};
      button.click();
      return {found:true, clicked:true};
    })()''', timeout=20)
    return bool(isinstance(value, dict) and value.get("clicked"))


def _raise_if_chatgpt_login_required(state: dict[str, Any]) -> None:
    if state.get("loginRequired"):
        raise ChatGPTStageError("CHATGPT_SESSION_LOGIN_REQUIRED")


def dismiss_chatgpt_rate_limit_dialogs_best_effort(*, cdp_url: str) -> dict[str, Any]:
    """Dismiss visible acknowledgement dialogs without sending a new prompt.

    Long adaptive cooldowns intentionally prevent stage execution. This small
    housekeeping probe still runs during that cooldown so a portal-rendered
    notice cannot remain over the composer until a person clicks it.
    """
    route = str(cdp_url or "").strip().rstrip("/")
    result: dict[str, Any] = {
        "inspected": 0, "detected": 0, "dismissed": 0,
        "nonblocking_overlays_dismissed": 0, "errors": [],
    }
    if not route:
        return result
    global CDP_URL
    previous_route = CDP_URL
    active_tab = ""
    try:
        CDP_URL = route
        tabs = _list_tabs()
        active_tab = next((str(tab.get("tabId") or "") for tab in tabs if tab.get("active")), "")
        for tab in tabs:
            tab_id = str(tab.get("tabId") or "").strip()
            tab_url = str(tab.get("url") or "").strip().lower()
            if not tab_id or "chatgpt.com" not in tab_url:
                continue
            result["inspected"] += 1
            if not _activate_tab(tab_id):
                continue
            result["nonblocking_overlays_dismissed"] += _dismiss_nonblocking_chatgpt_overlays()
            state = _page_state()
            if not state.get("rateLimited"):
                continue
            result["detected"] += 1
            if _dismiss_rate_limit_acknowledgement():
                result["dismissed"] += 1
    except Exception as exc:
        result["errors"].append(str(exc)[:500])
    finally:
        if active_tab:
            try:
                _activate_tab(active_tab)
            except Exception:
                pass
        CDP_URL = previous_route
    return result


def _raise_if_rate_limited(state: dict[str, Any]) -> None:
    if not state.get("rateLimited"):
        return
    rate_text = str(
        state.get("quotaLimitText")
        or state.get("rateLimitText")
        or state.get("rateLimitDialogText")
        or state.get("text")
        or "ChatGPT request rate limited"
    )
    dismissed = _dismiss_rate_limit_acknowledgement()
    retry_after = _record_rate_limit(rate_text)
    logger.warning(
        "ChatGPT rate-limit dialog detected; acknowledgement_clicked=%s retry_after_seconds=%s",
        dismissed,
        retry_after,
    )
    raise ChatGPTStageError(
        f"CHATGPT_TEMPORARY_RATE_LIMIT: retry_after_seconds={retry_after}",
        raw_text=rate_text,
        chat_url=str(state.get("url") or "") or None,
    )


def _raise_if_live_capacity_limited() -> None:
    """Probe the current project slot without navigating away from a late reply."""
    try:
        state = _page_state()
    except Exception:
        # CDP transport failures are classified by the normal browser path.
        return
    _raise_if_chatgpt_login_required(state)
    _raise_if_rate_limited(state)


def _stage_response_from_state(packet: dict[str, Any], state: dict[str, Any]) -> tuple[str, str | None] | None:
    project_id = str(packet.get("project_id") or "").strip()
    stage = str(packet.get("current_stage") or "").strip()
    execution_id = str(packet.get("execution_id") or "").strip()
    if not project_id or not stage:
        return None
    needles = [
        '"schema_version"',
        '"result"',
        '"next_stage"',
        project_id,
        stage,
    ]
    strict_needles = list(needles)
    if execution_id:
        strict_needles.extend(('"execution_id"', execution_id))
    messages = [str(value or "").strip() for value in state.get("messageTexts") or []]
    text = str(state.get("text") or "").strip()
    if text:
        messages.append(text)
    for candidate in reversed(messages):
        compact = candidate.replace("\u00a0", " ")
        if _looks_like_stage_request(compact):
            continue
        cleaned = _complete_stage_json_text(compact)
        # Browser recovery must never return a merely JSON-looking partial
        # assistant turn. The caller treats any recovered value as complete
        # and would otherwise fail parsing, then resend the same stage while
        # the account is cooling down.
        if not cleaned:
            continue
        searchable = cleaned
        if all(needle in searchable for needle in strict_needles):
            return searchable, str(state.get("url") or "") or None
        if (
            '"schema_version"' in searchable
            and '"result"' in searchable
            and '"next_stage"' in searchable
            and project_id in searchable
            and stage in searchable
            and '"current_stage"' not in searchable
        ):
            return searchable, str(state.get("url") or "") or None
    return None


def _existing_stage_response(packet: dict[str, Any]) -> tuple[str, str | None] | None:
    """Recover a late ChatGPT response before sending the stage again.

    A task may resume after a worker/bridge restart with a late ChatGPT answer
    already visible. Recovery is intentionally limited to this project's bound
    browser slot: projects may run in parallel across slots, but one project
    must never read another slot's conversations.
    """
    if str(packet.get("current_stage") or "") in VISUAL_STAGES:
        return None
    global CDP_URL
    primary_url = str(
        packet.get("browser_cdp_url")
        or CDP_URL
        or os.getenv("HERMES_CDP_URL")
        or "http://127.0.0.1:9222"
    ).rstrip("/")

    original_url = CDP_URL
    try:
        CDP_URL = primary_url
        tabs = _list_tabs()
        active_tab = next((str(tab.get("tabId") or "") for tab in tabs if tab.get("active")), "")
        tab_ids = [str(tab.get("tabId") or "") for tab in tabs if str(tab.get("tabId") or "")]
        if not tab_ids:
            tab_ids = [""]
        try:
            for tab_id in tab_ids:
                if tab_id:
                    _activate_tab(tab_id)
                    time.sleep(0.5)
                for isolated in (True, False):
                    try:
                        state = _page_state(isolated=isolated)
                    except Exception:
                        continue
                    recovered = _stage_response_from_state(packet, state)
                    if recovered is not None:
                        return recovered
                    # A stale stop button can outlive a completed assistant
                    # message. A complete project/execution JSON envelope is
                    # authoritative even while ChatGPT still reports busy.
                    if state.get("busy"):
                        continue
        finally:
            if active_tab:
                _activate_tab(active_tab)
    finally:
        CDP_URL = primary_url or original_url
    return None


def _wait_for_late_stage_response(
    packet: dict[str, Any], *, timeout_seconds: int = 90,
) -> tuple[str, str | None] | None:
    """Give a submitted request time to settle before any fresh resend.

    ChatGPT can finish after the browser waiter times out, especially when a
    promotional portal covers the composer. Polling the existing project tab
    is cheaper and safer than opening another conversation.
    """
    deadline = time.monotonic() + max(5, int(timeout_seconds))
    while time.monotonic() < deadline:
        try:
            _dismiss_nonblocking_chatgpt_overlays()
        except Exception:
            pass
        recovered = _existing_stage_response(packet)
        if recovered is not None:
            return recovered
        time.sleep(4)
    return None


def _reference_image_count(packet: dict[str, Any]) -> int:
    previous = dict(packet.get("previous_outputs") or {})
    review = dict(previous.get("CREATIVE_REVIEW") or {})
    production = dict(
        previous.get("MEDIA_DESIGN")
        or previous.get("PRODUCTION_PLAN")
        or {}
    )
    compiled = dict(production.get("compiled_media_design") or production)
    ticket = dict(compiled.get("visual_job_ticket") or {})
    values = [
        review.get("reference_image_count"),
        ticket.get("reference_image_count"),
        ticket.get("final_reference_count"),
        ticket.get("required_reference_images"),
        ticket.get("REFERENCE_IMAGE_COUNT"),
        ticket.get("FINAL_REFERENCE_COUNT"),
        ticket.get("REQUIRED_REFERENCE_IMAGES"),
    ]
    for value in values:
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            return max(1, min(64, count))
    refs = (
        ticket.get("reference_plan")
        or ticket.get("REFERENCE_PLAN")
        or ticket.get("reference_images")
        or ticket.get("REFERENCE_IMAGE_PLAN")
        or ticket.get("asset_manifest")
        or []
    )
    if isinstance(refs, list) and refs:
        return max(1, min(64, len(refs)))
    text = json.dumps({"review": review, "ticket": ticket}, ensure_ascii=False).lower()
    if any(marker in text for marker in ("3x2", "3 x 2", "2x3", "2 x 3", "6宫格", "六宫格", "six-panel", "6-panel")):
        return 6
    return 6


def _visual_browser_boards(packet: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Use the API visual planner as the single board-layout authority."""
    from app.services.hermes_agent.content_factory_api import build_visual_api_prompts

    return list(build_visual_api_prompts(packet))


def _expected_visual_count(packet: dict[str, Any], stage: str) -> int:
    if stage != "VISUAL_PREVIEW":
        return 1
    try:
        return max(1, len(_visual_browser_boards(packet)))
    except Exception:
        return max(1, math.ceil(_reference_image_count(packet) / 7))


def _visual_browser_board_instruction(packet: dict[str, Any], stage: str) -> tuple[str, int]:
    if stage != "VISUAL_PREVIEW":
        reference_count = _reference_image_count(packet)
        aspect_ratio = str(packet.get("video_aspect_ratio") or "9:16")
        columns = 1 if reference_count == 1 else (2 if reference_count <= 4 else 3)
        rows = (reference_count + columns - 1) // columns
        return (
            f"Generate exactly one final reference canvas containing {reference_count} ordered {aspect_ratio} panels "
            f"in a clean {columns}x{rows} grid. Use bright, straight, edge-to-edge gutters and no nested frames.",
            1,
        )

    boards = _visual_browser_boards(packet)
    board_count = max(1, len(boards))
    if board_count == 1:
        return (
            "Generate exactly one storyboard-board image. Follow this board specification exactly:\n"
            + boards[0][0],
            1,
        )

    sections = []
    for prompt, spec in boards:
        sections.append(
            f"BOARD IMAGE {int(spec['board_index'])} OF {int(spec['board_count'])}:\n{prompt}"
        )
    return (
        f"Generate exactly {board_count} separate storyboard-board images in this single response, one image for each numbered specification below. "
        "Do not combine the boards into one image, omit a board, repeat a board, or add any extra image. "
        "The images must appear in board-number order so the server can split them into one global reference sequence.\n\n"
        + "\n\n".join(sections),
        board_count,
    )


_SEQUENTIAL_BOARD_INSTRUCTION_TOKEN = "__HERMES_SEQUENTIAL_BOARD_INSTRUCTION__"


def _visual_browser_single_board_instruction(
    prompt: str,
    spec: dict[str, Any],
) -> str:
    board_index = int(spec.get("board_index") or 1)
    board_count = int(spec.get("board_count") or 1)
    return (
        f"Generate exactly one storyboard-board image: board {board_index} of {board_count}. "
        "Generate no other image in this response. Follow this board specification exactly:\n"
        + str(prompt).strip()
    )


def _visual_board_execution_marker(packet: dict[str, Any], board_index: int, board_count: int) -> str:
    execution_id = str(packet.get("execution_id") or "").strip()
    generation = max(
        0,
        int(packet.get("visual_fresh_regeneration_count") or 0),
        int(packet.get("response_fresh_regeneration_count") or 0),
    )
    if bool(packet.get("force_fresh_response")):
        generation = max(generation, int(packet.get("automatic_retry_count") or 0), 1)
    generation_marker = f"::visual-generation:{generation:02d}" if generation else ""
    return (
        f"{execution_id}{generation_marker}"
        f"::visual-board:{board_index:02d}/{board_count:02d}"
    )


def _visual_source_instruction(files: list[str] | tuple[str, ...]) -> str:
    if files:
        return (
            "Execute this visual stage using the attached approved input references only as identity and continuity authority. "
        )
    return (
        "No input reference image is attached for this project stage. Generate from the approved creative reference plan and "
        "structured controls; do not ask for uploads. "
    )


def _product_visual_lock(packet: dict[str, Any]) -> str:
    if not bool(packet.get("product_required", True)):
        return (
            "This is a product-free project. Do not show or invent any product, package, label, logo, "
            "brand, packshot, sales card, or product placeholder. "
        )
    product_value = packet.get("product")
    product_fields = dict(product_value) if isinstance(product_value, dict) else {}
    product_name = str(
        packet.get("product_name")
        or product_fields.get("name")
        or product_fields.get("product_name")
        or (product_value if isinstance(product_value, str) else "")
        or "the user's product"
    ).strip()
    brand_name = str(
        packet.get("brand_name")
        or product_fields.get("brand")
        or product_fields.get("brand_name")
        or ""
    ).strip()
    authority_parts = [product_name] if brand_name and product_name.lower().startswith(brand_name.lower()) else [brand_name, product_name]
    authority_name = " ".join(dict.fromkeys(part for part in authority_parts if part))
    return (
        f"Preserve the exact {authority_name} shown in the uploaded product authority image: same package shape, label family, cap/lid, "
        "colorway, proportions, and product-use physics. Do not substitute another product, generic packaging, charts, UI screens, "
        "or unrelated imagery. "
    )


def _humanize_project_text(value: Any) -> str:
    """Render user-authored requirements as readable prose, not JSON escapes."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    # Imports from spreadsheets/JSON sometimes persist the two literal
    # characters backslash+n. Convert those too, but leave other escapes alone.
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "  ")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _packet_for_prompt(packet: dict[str, Any], stage: str | None = None) -> dict[str, Any]:
    """Return the minimum stage contract; runtime and unrelated history never reach ChatGPT."""
    stage_name = str(stage or packet.get("current_stage") or "").upper()
    common = {
        "project_id", "execution_id", "product", "market", "current_stage",
        "video_variant_index", "video_variant_total", "video_variant_mode",
        "required_result_fields", "required_next_stage",
    }
    stage_keys = {
        "FACTS": {
            "browser_assets", "product_facts_rules",
        },
        "VISUAL_PREVIEW": {
            "previous_outputs", "browser_assets", "video_model",
            "video_reference_limit", "video_resolution", "video_language_label",
            "visual_repair_instruction",
        },
        "CREATIVE_REVIEW": {
            "previous_outputs", "browser_assets", "video_model",
            "video_reference_limit",
        },
        "FINAL_ASSETS": {
            "previous_outputs", "browser_assets", "visual_repair_instruction",
        },
        "VIDEO_PROMPTS": {
            "previous_outputs", "browser_assets", "browser_upload_mode",
            "browser_upload_note", "previous_variant_briefs",
            "video_duration_range_seconds", "recommended_video_duration_seconds",
            "video_segment_durations_seconds", "video_segment_count", "video_model",
            "video_reference_limit", "video_resolution", "video_language",
            "video_language_label",
        },
        "EDIT_PACKAGE": {
            "previous_outputs", "video_evidence", "video_resolution",
            "video_language", "video_language_label",
        },
    }
    allowed = common | stage_keys.get(stage_name, set())
    value = {
        key: item
        for key, item in packet.items()
        if key in allowed and item not in (None, "", [], {})
    }
    previous_allowed = {
        "VISUAL_PREVIEW": {"DIRECTOR", "PRODUCTION_PLAN", "MEDIA_DESIGN"},
        "CREATIVE_REVIEW": {"PRODUCTION_PLAN", "MEDIA_DESIGN", "VISUAL_PREVIEW"},
        "FINAL_ASSETS": {"PRODUCTION_PLAN", "MEDIA_DESIGN", "CREATIVE_REVIEW", "VISUAL_PREVIEW"},
        "VIDEO_PROMPTS": {"DIRECTOR", "PRODUCTION_PLAN", "MEDIA_DESIGN", "FINAL_ASSETS"},
        "EDIT_PACKAGE": {"VIDEO_PROMPTS"},
    }
    if isinstance(value.get("previous_outputs"), dict):
        keep_previous = previous_allowed.get(stage_name, set())
        value["previous_outputs"] = {
            key: item
            for key, item in dict(value["previous_outputs"]).items()
            if key in keep_previous
        }
    if value.get("browser_assets"):
        value.pop("project_assets", None)
    return value


def _wait_for_answer(
    previous_count: int,
    previous_images: set[str],
    timeout_seconds: int = 600,
    minimum_images: int = 1,
    packet: dict[str, Any] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    stable_since = None
    incomplete_since = None
    text_only_since = None
    no_response_since = None
    non_json_since = None
    busy_without_progress_since = None
    last_text = ""
    last_progress_signature = ""
    last_visual_signature = ""
    visual_stable_since = None
    last_overlay_probe = 0.0
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        if time.monotonic() - last_overlay_probe >= 12:
            _dismiss_nonblocking_chatgpt_overlays()
            last_overlay_probe = time.monotonic()
        state = _page_state()
        last_state = state
        text = str(state.get("text") or "")
        response_processing_placeholder = _looks_like_response_processing_placeholder(
            _preferred_response_text(state) or text
        )
        if response_processing_placeholder:
            # ChatGPT can retain a short live-status assistant turn after its
            # stop button disappears. Mark it busy before any non-JSON,
            # incomplete, or no-response classifier runs.
            state["busy"] = True
            state["responseProcessingPlaceholder"] = True
        if packet is not None and minimum_images <= 0:
            recovered = _stage_response_from_state(packet, state)
            if recovered is not None:
                recovered_text, recovered_url = recovered
                # A complete response is authoritative even when a stale stop
                # control or portal-rendered notice still covers the page.
                # Returning it before interruption handling prevents a second
                # identical request after ChatGPT has already completed work.
                state["text"] = recovered_text
                state["messageTexts"] = list(state.get("messageTexts") or []) + [recovered_text]
                # This path is specifically the resilient collector: a fully
                # formed answer has been harvested before any overlay/error
                # classifier is allowed to interrupt the stage.
                state["completedBehindPopup"] = True
                if recovered_url:
                    state["url"] = recovered_url
                return state
            if int(state.get("count") or 0) > previous_count and not state.get("busy"):
                raw_text = _preferred_response_text(state) or text
                if "cannot read properties of undefined" in raw_text.lower():
                    raise ChatGPTStageError(
                        "ChatGPT stage returned an internal UI/tool error: Cannot read properties of undefined",
                        raw_text=raw_text,
                        chat_url=str(state.get("url") or "") or None,
                    )
                if _looks_like_incomplete_stage_json(
                    raw_text,
                    str(packet.get("project_id") or ""),
                    str(packet.get("current_stage") or ""),
                ):
                    non_json_since = None
                else:
                    non_json_since = non_json_since or time.monotonic()
                if non_json_since and time.monotonic() - non_json_since >= 45:
                    raise ChatGPTStageError(
                        "ChatGPT stage returned text but not a valid project JSON envelope",
                        raw_text=raw_text,
                        chat_url=str(state.get("url") or "") or None,
                    )
            else:
                non_json_since = None
        # Uploaded references become visible in the user turn only after send.
        # They must never satisfy a visual-generation result.
        new_images = [
            value for value in (state.get("generatedImages") or [])
            if value not in previous_images
        ]
        # Never fall back to arbitrary newly visible images. Uploaded files
        # move from the composer into the user's turn only after send, so they
        # are "new" at exactly the moment a generated result is still pending.
        # Only DOM records explicitly classified as generated media are valid.
        progress_signature = json.dumps(
            {
                "count": int(state.get("count") or 0),
                "text": text[-500:],
                "images": new_images,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if state.get("busy"):
            if progress_signature != last_progress_signature:
                busy_without_progress_since = time.monotonic()
            else:
                busy_without_progress_since = busy_without_progress_since or time.monotonic()
            busy_limit = (
                int(os.getenv("HERMES_RESPONSE_PROCESSING_NO_PROGRESS_SECONDS", "300"))
                if state.get("responseProcessingPlaceholder")
                else int(os.getenv("HERMES_BUSY_NO_PROGRESS_SECONDS", "150"))
            )
            if time.monotonic() - busy_without_progress_since >= busy_limit:
                raise ChatGPTStageError(
                    f"CHATGPT_RESPONSE_STILL_RUNNING: no observable progress for {busy_limit} seconds",
                    raw_text=_preferred_response_text(state) or text,
                    chat_url=str(state.get("url") or "") or None,
                )
        else:
            busy_without_progress_since = None
        last_progress_signature = progress_signature
        image_goal_met = minimum_images > 0 and len(new_images) >= minimum_images
        visual_signature = json.dumps(new_images[-minimum_images:] if image_goal_met else [], ensure_ascii=False)
        if image_goal_met:
            if visual_signature != last_visual_signature:
                last_visual_signature = visual_signature
                visual_stable_since = time.monotonic()
            else:
                visual_stable_since = visual_stable_since or time.monotonic()
        else:
            last_visual_signature = ""
            visual_stable_since = None
        visual_stable_seconds = int(os.getenv("HERMES_VISUAL_STABLE_WITH_BUSY_SECONDS", "18"))
        visual_complete_while_busy = (
            minimum_images > 0
            and image_goal_met
            and visual_stable_since is not None
            and time.monotonic() - visual_stable_since >= max(8, visual_stable_seconds)
        )
        if visual_complete_while_busy:
            state["newImages"] = new_images
            return state
        complete = (
            not state.get("busy")
            and (
                image_goal_met
                if minimum_images > 0
                else (packet is None and int(state.get("count") or 0) > previous_count and len(text) >= 80)
            )
        )
        if complete and text == last_text:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= 8:
                state["newImages"] = new_images
                return state
        else:
            stable_since = None
        if image_goal_met and state.get("rateLimited") and not state.get("busy"):
            # The media is already in the assistant turn. Do not discard it
            # merely because a rate-limit acknowledgement appeared afterward.
            state["newImages"] = new_images
            state["completedBehindPopup"] = True
            return state
        _raise_if_chatgpt_login_required(state)
        _raise_if_rate_limited(state)
        if state.get("generationFailed"):
            raise RuntimeError("ChatGPT image generation failed")
        if state.get("policyRefusal") and not state.get("busy"):
            raise RuntimeError("Visual GPT refused the project brief under stale visual guidelines")
        incomplete = (
            int(state.get("count") or 0) > previous_count
            and not state.get("busy")
            and (minimum_images <= 0 or not new_images)
            and len(text) < 80
            and text == last_text
        )
        if incomplete:
            incomplete_since = incomplete_since or time.monotonic()
            incomplete_grace = max(
                45,
                int(os.getenv("HERMES_INCOMPLETE_TEXT_GRACE_SECONDS", "90")),
            )
            if time.monotonic() - incomplete_since >= incomplete_grace:
                raise ChatGPTStageError(
                    "ChatGPT stage returned an incomplete or truncated text response",
                    raw_text=_preferred_response_text(state) or text,
                    chat_url=str(state.get("url") or "") or None,
                )
        else:
            incomplete_since = None
        text_only_visual = (
            minimum_images > 0
            and int(state.get("count") or 0) > previous_count
            and not state.get("busy")
            and not new_images
            and len(text.strip()) >= 20
            and text == last_text
        )
        if text_only_visual:
            text_only_since = text_only_since or time.monotonic()
            if time.monotonic() - text_only_since >= 240:
                raise RuntimeError(
                    "ChatGPT stage returned text without a generated image: "
                    + text.strip()[:500]
                )
        else:
            text_only_since = None
        no_response = (
            int(state.get("count") or 0) <= previous_count
            and (minimum_images <= 0 or not new_images)
            and not state.get("busy")
        )
        if no_response:
            no_response_since = no_response_since or time.monotonic()
            no_response_limit = (
                int(os.getenv("HERMES_VISUAL_NO_RESPONSE_SECONDS", "90"))
                if minimum_images > 0
                else max(540, timeout_seconds - 30)
            )
            if time.monotonic() - no_response_since >= no_response_limit:
                raise ChatGPTStageError(
                    "ChatGPT stage stopped without returning a response",
                    raw_text=_preferred_response_text(state) or text,
                    chat_url=str(state.get("url") or "") or None,
                )
        else:
            no_response_since = None
        last_text = text
        time.sleep(5)
    if last_state.get("busy"):
        raise ChatGPTStageError(
            "CHATGPT_RESPONSE_STILL_RUNNING: browser response exceeded this collector window",
            raw_text=_preferred_response_text(last_state) or str(last_state.get("text") or ""),
            chat_url=str(last_state.get("url") or "") or None,
        )
    raise TimeoutError("Timed out waiting for the ChatGPT stage response")














def _attachment_count() -> int:
    value = _eval(r'''(() => {
      const composer = document.querySelector('form textarea')?.closest('form')
        || document.querySelector('#prompt-textarea')?.closest('form')
        || document.querySelector('[contenteditable="true"]')?.closest('form')
        || document;
      const editor = composer.querySelector('#prompt-textarea,[contenteditable="true"][role="textbox"],div.ProseMirror,textarea');
      const outsideEditor = node => !(editor && editor.contains(node));
      const visible = node => {
        const rect = node.getBoundingClientRect();
        return rect.width >= 12 && rect.height >= 12;
      };
      const removeButtons = [...composer.querySelectorAll('button,[role="button"]')].filter(button => {
        if (!outsideEditor(button) || !visible(button)) return false;
        const label=((button.getAttribute('aria-label')||'')+' '+(button.innerText||'')+' '+(button.title||'')).toLowerCase();
        return /remove file|remove attachment|delete file|\u79fb\u9664\u6587\u4ef6|\u5220\u9664\u6587\u4ef6/.test(label);
      }).length;
      const mediaPreviews = [...composer.querySelectorAll('img, video')].filter(node => {
        if (!outsideEditor(node) || !visible(node)) return false;
        const rect = node.getBoundingClientRect();
        if (rect.width < 24 || rect.height < 24) return false;
        const src=(node.currentSrc || node.src || '').toLowerCase();
        const alt=(node.getAttribute('alt') || '').toLowerCase();
        return src.startsWith('blob:') || src.startsWith('data:') || /upload|attachment|preview|\.(png|jpg|jpeg|webp|pdf|mp4|mov)\b/.test(src + ' ' + alt);
      }).length;
      const attachmentChips = [...composer.querySelectorAll('[data-testid], [aria-label], [title]')].filter(node => {
        if (!outsideEditor(node) || !visible(node)) return false;
        const rect = node.getBoundingClientRect();
        if (rect.width > 420 || rect.height > 180) return false;
        const label=((node.getAttribute('data-testid')||'')+' '+(node.getAttribute('aria-label')||'')+' '+(node.getAttribute('title')||'')+' '+(node.innerText||'')).toLowerCase();
        if (/add|attach|upload/.test(label) && !/\.(png|jpe?g|webp|gif|pdf|mp4|mov|webm)\b/.test(label)) return false;
        return /attachment|uploaded|\.(png|jpe?g|webp|gif|pdf|mp4|mov|webm)\b|png|jpe?g|webp|pdf|mp4|mov/.test(label);
      }).length;
      return removeButtons > 0 ? removeButtons : Math.max(mediaPreviews, attachmentChips);
    })()''')
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0




def _direct_cdp_upload_file(selector: str, path: str) -> None:
    """Set a browser-host file through CDP when agent-browser selects silently.

    Chrome runs on the user's Windows device, so DOM.setFileInputFiles must
    receive that device's synced Windows path. The agent-browser upload command
    can occasionally return success without forwarding the file chooser event
    through an SSH CDP tunnel. A direct protocol fallback is deterministic and
    still targets only the uniquely marked input in the current ChatGPT tab.
    """
    import urllib.request
    from websockets.sync.client import connect

    base_url = str(CDP_URL or "").rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise RuntimeError(f"Direct CDP upload requires an HTTP CDP endpoint: {base_url}")
    with urllib.request.urlopen(f"{base_url}/json/list", timeout=5) as response:
        targets = json.load(response)
    page_targets = [
        target for target in list(targets or [])
        if str(target.get("type") or "") == "page"
        and str(target.get("webSocketDebuggerUrl") or "").strip()
    ]
    page_targets.sort(
        key=lambda target: 0 if "chatgpt.com" in str(target.get("url") or "").lower() else 1
    )
    expression = f"document.querySelector({json.dumps(selector)})"
    last_error: BaseException | None = None
    lock_file = _acquire_browser_lock(_session_name())
    try:
        for target in page_targets:
            try:
                with connect(
                    str(target["webSocketDebuggerUrl"]),
                    origin="http://localhost",
                    open_timeout=5,
                    close_timeout=2,
                ) as socket:
                    sequence = 0

                    def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
                        nonlocal sequence
                        sequence += 1
                        request_id = sequence
                        socket.send(json.dumps({"id": request_id, "method": method, "params": params}))
                        while True:
                            message = json.loads(socket.recv(timeout=8))
                            if int(message.get("id") or 0) == request_id:
                                if message.get("error"):
                                    raise RuntimeError(str(message["error"])[:1000])
                                return dict(message.get("result") or {})

                    evaluated = call("Runtime.evaluate", {
                        "expression": expression,
                        "returnByValue": False,
                    })
                    remote = dict(evaluated.get("result") or {})
                    object_id = str(remote.get("objectId") or "").strip()
                    if not object_id or remote.get("subtype") == "null":
                        continue
                    described = call("DOM.describeNode", {"objectId": object_id})
                    backend_node_id = int(dict(described.get("node") or {}).get("backendNodeId") or 0)
                    if backend_node_id <= 0:
                        continue
                    call("DOM.setFileInputFiles", {
                        "files": [str(path)],
                        "backendNodeId": backend_node_id,
                    })
                    return
            except BaseException as exc:
                last_error = exc
        raise RuntimeError(
            f"Direct CDP upload could not find the marked ChatGPT input {selector}: {last_error}"
        )
    finally:
        _release_browser_lock(lock_file)


def _upload_file(path: str, *, expected_count: int | None = None) -> None:
    filename = (
        PureWindowsPath(path).name
        if "\\" in str(path)
        else Path(path).name
    ).strip().lower()
    stem = filename.rsplit(".", 1)[0]
    last_error = None
    # Browser-agent/CDP calls cross the user's SSH tunnel. A disconnected or
    # wedged tunnel used to spend 180 seconds on every status probe and retry
    # the same file twelve times, so one upload could outlive the whole Celery
    # stage lease. Keep retries local and bounded; the project-level recovery
    # policy owns the next attempt after a bridge failure.
    for upload_attempt in range(int(os.getenv("HERMES_CHATGPT_UPLOAD_RETRIES", "3"))):
        before = 0
        try:
            # The current ChatGPT composer only wires its React upload handler
            # after the user opens the plus menu. Setting the legacy hidden
            # #upload-files input directly leaves a File object in the DOM but
            # never creates an attachment tile. Use a trusted browser click to
            # open the menu, then target its live photo/file input. Keep the
            # legacy selector as a bounded fallback for older accounts.
            selector = _prepare_chatgpt_upload_input(force_refresh=upload_attempt > 0)
            before = int(_attachment_upload_state().get("attachmentCount") or 0)
            if upload_attempt > 0:
                try:
                    _direct_cdp_upload_file(selector, path)
                except Exception:
                    # Preserve compatibility with local/non-HTTP CDP providers.
                    _run("upload", selector, path, timeout=90)
            else:
                _run("upload", selector, path, timeout=90)
            deadline = time.monotonic() + 75
            selected_at = time.monotonic()
            accept_grace = max(
                0,
                int(os.getenv("HERMES_CHATGPT_UPLOAD_ACCEPT_SECONDS", "18")),
            )
            visible_since = None
            ready_polls = 0
            while time.monotonic() < deadline:
                state = _attachment_upload_state()
                upload_limit_text = str(state.get("uploadLimitText") or "").strip()
                if state.get("uploadLimited") or upload_limit_text:
                    raise ChatGPTStageError(
                        "CHATGPT_UPLOAD_LIMIT: "
                        + (upload_limit_text[:800] or "ChatGPT currently accepts 0 uploaded files"),
                        raw_text=upload_limit_text,
                    )
                if state.get("failed"):
                    raise RuntimeError(f"ChatGPT rejected upload for {path}: {state}")
                attachment_count = int(state.get("attachmentCount") or 0)
                attachment_text = str(state.get("attachmentText") or "").lower()
                filename_visible = bool(
                    filename
                    and (
                        filename in attachment_text
                        or (len(stem) >= 5 and stem in attachment_text)
                    )
                )
                visible = (
                    attachment_count > before
                    or (
                        expected_count is not None
                        and attachment_count >= int(expected_count)
                    )
                    or filename_visible
                )
                if visible:
                    visible_since = visible_since or time.monotonic()
                    if not state.get("pending") and state.get("sendable"):
                        ready_polls += 1
                        if ready_polls >= 2:
                            return
                    else:
                        ready_polls = 0
                    if time.monotonic() - visible_since >= 60:
                        raise RuntimeError(
                            "ChatGPT accepted the file tile but did not finish processing it; "
                            f"path={path}, state={state}"
                        )
                elif (
                    not state.get("pending")
                    and not state.get("failed")
                    and time.monotonic() - selected_at >= accept_grace
                ):
                    # ChatGPT occasionally leaves the previous React file
                    # input mounted after several successful selections. CDP
                    # can set a file on that stale node without the composer
                    # receiving an attachment event. Rebuild only this input
                    # and retry the same file; already visible attachments stay
                    # in the composer and are never uploaded again.
                    raise RuntimeError(
                        "ChatGPT file input did not accept the selected file; "
                        f"path={path}, state={state}"
                    )
                time.sleep(2 if state.get("pending") else 3)
            raise RuntimeError(f"ChatGPT upload was not visible after selecting {path}: {_attachment_upload_state()}")
        except Exception as exc:
            last_error = exc
            if "CHATGPT_UPLOAD_LIMIT" in str(exc):
                raise
            try:
                state = _attachment_upload_state()
                attachment_count = int(state.get("attachmentCount") or 0)
                attachment_text = str(state.get("attachmentText") or "").lower()
                already_visible = (
                    attachment_count > before
                    or (
                        expected_count is not None
                        and attachment_count >= int(expected_count)
                    )
                    or filename in attachment_text
                    or (len(stem) >= 5 and stem in attachment_text)
                )
                if already_visible and not state.get("pending") and not state.get("failed"):
                    return
            except Exception:
                pass
            time.sleep(5)
    raise RuntimeError(f"Could not upload {path}: {last_error}")


def _prepare_chatgpt_upload_input(*, force_refresh: bool = False) -> str:
    """Return one uniquely marked, currently mounted ChatGPT file input.

    ChatGPT may keep stale hidden inputs in the DOM after several uploads. A
    broad selector then resolves to the old node and CDP reports success even
    though React never receives the file. Marking exactly one live input makes
    each upload deterministic. A silent selection retries after closing and
    reopening the upload menu, without clearing accepted attachments.
    """
    token = f"hermes-{time.time_ns()}"

    def mark_current_input() -> str | None:
        state = _eval_timeout(
            r'''JSON.stringify((() => {
              const token = __TOKEN__;
              const inputs = [...document.querySelectorAll('input[type="file"]')]
                .filter(node => node.isConnected && !node.disabled);
              for (const node of inputs) node.removeAttribute('data-hermes-upload-target');
              const modern = inputs.filter(node => node.matches('[data-testid="upload-photos-input"]'));
              const legacy = inputs.filter(node => node.id === 'upload-files');
              const candidates = modern.length ? modern : (legacy.length ? legacy : inputs);
              const node = candidates[candidates.length - 1];
              if (!node) return {selector: '', count: 0};
              try { node.value = ''; } catch (error) {}
              node.setAttribute('data-hermes-upload-target', token);
              return {
                selector: `input[data-hermes-upload-target="${token}"]`,
                count: inputs.length
              };
            })())'''.replace("__TOKEN__", json.dumps(token)),
            timeout=12,
        )
        if isinstance(state, dict) and str(state.get("selector") or "").strip():
            return str(state["selector"])
        return None

    if force_refresh:
        try:
            _run("press", "Escape", timeout=10)
        except Exception:
            pass
        time.sleep(1)
    else:
        selector = mark_current_input()
        if selector:
            return selector

    try:
        _run("click", '[data-testid="composer-plus-btn"]', timeout=20)
    except Exception:
        # Older composer variants expose #upload-files without a plus menu.
        pass
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        selector = mark_current_input()
        if selector:
            return selector
        time.sleep(1)
    raise RuntimeError("ChatGPT file upload input is unavailable after opening the composer menu")


def _chatgpt_send_button_js(*, click: bool = False) -> str:
    click_js = r'''
        try { editor.focus(); } catch (error) {}
        try { button.scrollIntoView({block: 'center', inline: 'center'}); } catch (error) {}
        const rect = button.getBoundingClientRect();
        const eventInit = {
          bubbles: true,
          cancelable: true,
          composed: true,
          clientX: rect.left + rect.width / 2,
          clientY: rect.top + rect.height / 2,
          button: 0,
          buttons: 1
        };
        for (const eventName of ['pointerover', 'pointerenter', 'mouseover', 'mouseenter', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
          try { button.dispatchEvent(new PointerEvent(eventName, eventInit)); }
          catch (error) {
            try { button.dispatchEvent(new MouseEvent(eventName, eventInit)); } catch (inner) {}
          }
        }
        try { button.click(); } catch (error) {}
        const form = button.closest('form') || composer.closest?.('form') || composer;
        if (form && typeof form.requestSubmit === 'function') {
          try { form.requestSubmit(button); } catch (error) {
            try { form.requestSubmit(); } catch (inner) {}
          }
        }
    ''' if click else ""
    script = r'''JSON.stringify((() => {
      const visible = node => {
        if (!node) return false;
        const rect = node.getBoundingClientRect();
        const style = window.getComputedStyle(node);
        return rect.width >= 16 && rect.height >= 16
          && style.display !== 'none'
          && style.visibility !== 'hidden'
          && style.opacity !== '0';
      };
      const visibleEditor = node => {
        if (!visible(node)) return false;
        const rect = node.getBoundingClientRect();
        return rect.width >= 120 && rect.height >= 24
          && rect.bottom > window.innerHeight * 0.45
          && rect.top < window.innerHeight + 80;
      };
      const editor = [...document.querySelectorAll('#prompt-textarea,[contenteditable="true"][role="textbox"],div.ProseMirror,textarea')]
        .filter(visibleEditor)
        .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
      if (!editor) {
        return {found: false, disabled: true, sendable: false, label: '', testid: '', type: ''};
      }
      const composer = editor.closest('form')
        || editor.closest('[data-testid*="composer"],[class*="composer"],main')
        || editor.parentElement
        || document;
      const labelOf = button => ((button.getAttribute('data-testid') || '') + ' '
        + (button.getAttribute('aria-label') || '') + ' '
        + (button.getAttribute('title') || '') + ' '
        + (button.innerText || '')).toLowerCase();
      const er = editor.getBoundingClientRect();
      const exact = [
        'button[data-testid="send-button"]',
        'button[data-testid="composer-submit-button"]',
        'button[data-testid="fruitjuice-send-button"]'
      ].flatMap(selector => [...document.querySelectorAll(selector)])
        .filter(visible)
        .filter(button => {
          const rect = button.getBoundingClientRect();
          return rect.top >= er.top - 80 && rect.top <= er.bottom + 120 && rect.left >= er.left - 40;
        })[0];
      const candidates = [...composer.querySelectorAll('button')].filter(visible);
      const semantic = candidates.find(button => {
        const label = labelOf(button);
        if (/stop|cancel|voice|microphone|dictate|attach|upload|add|tool|sidebar|new chat/.test(label)) return false;
        return button.type === 'submit' || /send|submit|arrow-up|up-arrow|composer-submit/.test(label);
      });
      const nearEditor = candidates.find(button => {
        const rect = button.getBoundingClientRect();
        if (rect.left < er.left || rect.top < er.top - 30 || rect.top > er.bottom + 90) return false;
        const label = labelOf(button);
        if (/stop|cancel|voice|microphone|dictate|attach|upload|add|tool|sidebar|new chat/.test(label)) return false;
        return button.type === 'submit' || /send|submit|arrow-up|up-arrow|composer-submit/.test(label);
      });
      const button = exact || semantic || nearEditor || null;
      const disabled = !button
        || button.disabled
        || button.getAttribute('aria-disabled') === 'true'
        || button.dataset?.disabled === 'true';
      if (button && !disabled) {
        __CLICK__
      }
      return {
        found: !!button,
        disabled,
        sendable: !!button && !disabled,
        label: button ? labelOf(button).slice(0, 180) : '',
        testid: button?.getAttribute('data-testid') || '',
        type: button?.type || '',
        formFound: !!(button && (button.closest('form') || composer.closest?.('form') || composer))
      };
    })())'''
    return script.replace("__CLICK__", click_js)


def _attachment_upload_state() -> dict[str, Any]:
    # Attachment polling happens many times per file and crosses the remote
    # browser bridge. It must fail quickly when that route is unhealthy so the
    # stage can release its slot and let self-heal retry from the same project.
    state = _eval_timeout(r'''JSON.stringify((() => {
      const visible = node => {
        if (!node) return false;
        const rect = node.getBoundingClientRect();
        const style = window.getComputedStyle(node);
        return rect.width >= 12 && rect.height >= 12
          && style.display !== 'none'
          && style.visibility !== 'hidden'
          && style.opacity !== '0';
      };
      const editor = [...document.querySelectorAll('#prompt-textarea,[contenteditable="true"][role="textbox"],div.ProseMirror,textarea')]
        .filter(node => {
          if (!visible(node)) return false;
          const rect = node.getBoundingClientRect();
          return rect.width >= 120 && rect.height >= 24
            && rect.bottom > window.innerHeight * 0.45
            && rect.top < window.innerHeight + 80;
        })
        .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
      if (!editor) {
        return {
          attachmentCount: 0,
          attachmentText: '',
          pending: false,
          failed: false,
          progressCount: 0,
          cancelUploadCount: 0
        };
      }
      const composer = editor.closest('form')
        || editor.closest('[data-testid*="composer"],[class*="composer"],main')
        || editor.parentElement
        || document;
      const uploadLimitPattern = /一次最多可上传\s*0\s*个文件|最多可上传\s*0\s*个文件|maximum of\s*0\s*files|upload up to\s*0\s*files|file upload limit reached|upload limit reached/i;
      const uploadFailurePattern = /无法上传|could not upload|couldn't upload|unable to upload/i;
      const globalNotices = [
        ...document.querySelectorAll(
          '[role="alert"],[role="status"],[role="dialog"],[aria-live="assertive"],[aria-live="polite"],[class*="toast" i]'
        )
      ]
        .filter(visible)
        .map(node => (node.innerText || node.textContent || '').trim())
        .filter(Boolean);
      const uploadLimitText = globalNotices.find(text =>
        uploadLimitPattern.test(text)
        || (uploadFailurePattern.test(text) && /最多可上传\s*0\s*个文件|maximum of\s*0\s*files|upload up to\s*0\s*files/i.test(text))
      ) || '';
      const outsideEditor = node => !(editor && editor.contains(node));
      const labels = [...composer.querySelectorAll('[aria-label], [title], button')]
        .filter(node => outsideEditor(node))
        .map(node => ((node.getAttribute('aria-label') || '') + ' ' + (node.getAttribute('title') || '') + ' ' + (node.innerText || '')).toLowerCase())
        .join('\n');
      const removeButtons = [...composer.querySelectorAll('button,[role="button"]')].filter(node => {
        if (!outsideEditor(node) || !visible(node)) return false;
        const label=((node.getAttribute('aria-label')||'')+' '+(node.innerText||'')+' '+(node.title||'')).toLowerCase();
        if (/add|attach|upload/.test(label) && !/remove|delete|cancel|close/.test(label)) return false;
        return /remove|delete|cancel upload|close|移除|删除|取消/.test(label);
      }).length;
      const mediaPreviews = [...composer.querySelectorAll('img, video')].filter(node => {
        if (!outsideEditor(node) || !visible(node)) return false;
        const rect = node.getBoundingClientRect();
        if (rect.width < 24 || rect.height < 24) return false;
        const src=(node.currentSrc || node.src || '').toLowerCase();
        const alt=(node.getAttribute('alt') || '').toLowerCase();
        return src.startsWith('blob:') || src.startsWith('data:') || /upload|attachment|preview|\.(png|jpg|jpeg|webp|pdf|mp4|mov|webm)\b/.test(src + ' ' + alt);
      }).length;
      const attachmentChips = [...composer.querySelectorAll('[data-testid], [aria-label], [title]')].filter(node => {
        if (!outsideEditor(node) || !visible(node)) return false;
        const rect = node.getBoundingClientRect();
        if (rect.width > 420 || rect.height > 180) return false;
        const label=((node.getAttribute('data-testid')||'')+' '+(node.getAttribute('aria-label')||'')+' '+(node.getAttribute('title')||'')+' '+(node.innerText||'')).toLowerCase();
        if (/add|attach|upload/.test(label) && !/\.(png|jpe?g|webp|gif|pdf|mp4|mov|webm)\b/.test(label)) return false;
        return /attachment|uploaded|\.(png|jpe?g|webp|gif|pdf|mp4|mov|webm)\b|png|jpe?g|webp|pdf|mp4|mov/.test(label);
      }).length;
      // Each file currently renders a preview, a filename chip, and one remove
      // button. Counting all of those made one file look like two or three
      // attachments and allowed the next upload to start while ChatGPT was
      // still processing the first one. The remove control is the stable
      // one-file/one-control signal; use the looser selectors only as fallback.
      const attachmentCount = removeButtons > 0
        ? removeButtons
        : Math.max(mediaPreviews, attachmentChips);
      const pending = [
        'uploading', 'preparing file', 'processing file', 'waiting for files',
        'waiting for file upload', 'file is uploading',
        '正在上传', '等待文件上传', '处理文件', '正在处理'
      ].some(token => labels.includes(token));
      const failed = [
        'upload failed', 'could not upload', "couldn't upload", 'file unavailable',
        'failed to upload', 'unsupported file',
        '上传失败', '无法上传', '文件不可用', '不支持的文件'
      ].some(token => labels.includes(token));
      const progressCount = [...composer.querySelectorAll('[role="progressbar"], progress, [aria-busy="true"]')]
        .filter(node => outsideEditor(node)).length;
      const cancelUploadCount = [...composer.querySelectorAll('button')].filter(button => {
        if (!outsideEditor(button)) return false;
        const label=((button.getAttribute('aria-label')||'')+' '+(button.innerText||'')).toLowerCase();
        return /cancel upload|stop upload|取消上传/.test(label);
      }).length;
      return {
        attachmentCount,
        attachmentText: labels.slice(-8000),
        pending: pending || progressCount > 0 || cancelUploadCount > 0,
        failed,
        uploadLimited: Boolean(uploadLimitText),
        uploadLimitText: uploadLimitText.slice(0, 2000),
        progressCount,
        cancelUploadCount
      };
    })())''', timeout=25)
    if not isinstance(state, dict):
        state = {}
    button_state = _eval_timeout(_chatgpt_send_button_js(click=False), timeout=20)
    if isinstance(button_state, dict):
        state.update({
            "sendable": bool(button_state.get("sendable")),
            "sendButtonFound": bool(button_state.get("found")),
            "sendButtonDisabled": bool(button_state.get("disabled")),
            "sendButtonLabel": button_state.get("label") or "",
            "sendButtonTestId": button_state.get("testid") or "",
        })
    return state


def _wake_composer_for_send(prompt_text: str) -> int:
    """Refresh ChatGPT's composer state without touching uploaded attachments."""
    if not prompt_text:
        return _composer_text_length()
    if not _composer_text_matches(prompt_text):
        raise RuntimeError("CHATGPT_PROMPT_INTEGRITY_MISMATCH: composer text differs from the stage payload")
    current = len(_composer_text_value())
    try:
        _eval(r'''(() => {
          const visible = node => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width >= 120 && rect.height >= 24
              && rect.bottom > window.innerHeight * 0.45
              && rect.top < window.innerHeight + 80
              && style.display !== 'none'
              && style.visibility !== 'hidden'
              && style.opacity !== '0';
          };
          const editor = [...document.querySelectorAll('#prompt-textarea,[contenteditable="true"][role="textbox"],div.ProseMirror,textarea')]
            .filter(visible)
            .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
          if (!editor) return false;
          editor.focus();
          for (const eventName of ['beforeinput', 'input', 'keyup', 'change']) {
            try {
              editor.dispatchEvent(new InputEvent(eventName, {bubbles: true, composed: true, inputType: 'insertText', data: ' '}));
            } catch (error) {
              editor.dispatchEvent(new Event(eventName, {bubbles: true, composed: true}));
            }
          }
          try {
            editor.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, key: ' ', code: 'Space'}));
            editor.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: ' ', code: 'Space'}));
          } catch (error) {}
          return true;
        })()''')
    except Exception:
        pass
    current = max(current, _composer_text_length())
    if _composer_text_matches(prompt_text):
        try:
            sendable = bool(_attachment_upload_state().get("sendable"))
        except Exception:
            sendable = False
        if not sendable:
            # A real no-op keystroke forces React/ProseMirror to reconcile a
            # DOM-restored draft. Synthetic input events alone can leave the
            # visible text detached from ChatGPT's submit state.
            try:
                _run("focus", "#prompt-textarea", timeout=15)
                _run("press", "End", timeout=10)
                _run("keyboard", "inserttext", " ", timeout=10)
                _run("press", "Backspace", timeout=10)
                time.sleep(0.5)
            except Exception:
                pass
    if not _composer_text_matches(prompt_text):
        raise RuntimeError("CHATGPT_PROMPT_INTEGRITY_MISMATCH: composer changed while enabling submit")
    return max(current, _composer_text_length())


def _wait_until_sendable(timeout_seconds: int = 180, expected_attachments: int = 0, prompt_text: str = "") -> None:
    deadline = time.monotonic() + timeout_seconds
    stable_since = None
    last_wake = 0.0
    disabled_since = None
    last_count = -1
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = _attachment_upload_state()
        last_state = state
        count = int(state.get("attachmentCount") or 0)
        if state.get("failed"):
            raise RuntimeError(f"ChatGPT attachment upload failed before send: {state}")
        if count != last_count:
            stable_since = time.monotonic()
            last_count = count
        stable_for = time.monotonic() - float(stable_since or time.monotonic())
        # ChatGPT frequently collapses many uploaded files into a smaller
        # visible stack. Each `_upload_file(..., expected_count=N)` already
        # waits for the just-selected file to become visible/sendable, so the
        # final send gate should not fail a stage only because the UI renders
        # seven uploads as three visible tiles. Require at least one stable
        # attachment when files are expected, but keep the exact-count path
        # for browsers that expose it.
        collapsed_attachment_ready = (
            expected_attachments > 0
            and count > 0
            and not state.get("pending")
            and stable_for >= 18
        )
        attachment_ready = (
            expected_attachments <= 0
            or count >= expected_attachments
            or collapsed_attachment_ready
        )
        if attachment_ready and not state.get("pending") and stable_for >= (8 if expected_attachments else 0):
            if state.get("sendable"):
                return
            disabled_since = disabled_since or time.monotonic()
            if prompt_text and time.monotonic() - last_wake >= 9:
                last_wake = time.monotonic()
                _wake_composer_for_send(prompt_text)
            if time.monotonic() - disabled_since >= 30 and _composer_text_length() >= 10:
                # Some ChatGPT builds keep the visible submit button hidden or
                # disabled even after the draft is fully inserted, especially
                # after a browser/worker restart. Uploads are stable here, so
                # hand off to _send_prompt, which tries real clicks, form
                # submit, and Enter before deciding the draft is stuck.
                return
        else:
            disabled_since = None
        time.sleep(3)
    raise TimeoutError(f"Attachments did not finish uploading in ChatGPT: {last_state}")


def _prompt_submission_marker(prompt_text: str) -> str:
    if not prompt_text:
        return ""
    board_marker = re.search(
        r"BROWSER REQUEST MARKER \(idempotency only; do not reproduce\):\s*([^\r\n]+)",
        prompt_text,
    )
    if board_marker:
        return board_marker.group(1).strip()
    execution_marker = re.search(r"execution_id must be exactly\s+(\"(?:\\.|[^\"])*\")", prompt_text)
    if execution_marker:
        try:
            return str(json.loads(execution_marker.group(1)))
        except Exception:
            pass
    for token in (
        "execution_id must be exactly ",
        '"execution_id":',
        "STAGE PACKET",
        "Execute exactly this content-factory",
    ):
        if token in prompt_text:
            if token == "execution_id must be exactly ":
                start = prompt_text.find(token)
                return prompt_text[start:start + 120]
            return token
    return prompt_text[:80]


def _prompt_text_submitted(state: dict[str, Any], prompt_text: str, *, previous_count: int) -> bool:
    if not prompt_text:
        return True
    user_text = "\n".join(
        str(value or "") for value in (state.get("userMessageTexts") or [])
    )
    if not user_text:
        user_text = str(state.get("latestUserText") or "")
    marker = _prompt_submission_marker(prompt_text)
    if marker:
        return marker in user_text
    if bool(state.get("busy")) or int(state.get("count") or 0) > previous_count:
        return True
    compact_user = " ".join(user_text.split())
    compact_prompt = " ".join(prompt_text.split())
    # ChatGPT often truncates long submitted prompts in the visible user
    # message. It should still expose the stage identity and a meaningful
    # amount of text. Image-only submissions stay near empty and are rejected.
    if len(compact_user) >= min(240, max(80, len(compact_prompt) // 80)):
        identity_hits = sum(
            1
            for token in ("content-factory", "STAGE PACKET", "project_id", "stage")
            if token in user_text
        )
        if identity_hits >= 2:
            return True
    return False


def _ensure_prompt_submission_started(prompt_text: str, *, previous_count: int, timeout_seconds: int = 18) -> None:
    if not prompt_text:
        return
    deadline = time.monotonic() + timeout_seconds
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_state = _page_state()
        if _prompt_text_submitted(last_state, prompt_text, previous_count=previous_count):
            return
        time.sleep(2)
    latest_user = str(last_state.get("latestUserText") or "").strip()
    raise RuntimeError(
        "ChatGPT consumed the composer but did not submit the stage prompt text; "
        "only attachments or an empty user turn were visible. "
        f"latest_user={latest_user[:300]!r} page={str(last_state)[:500]}"
    )


def _send_prompt(prompt_text: str = "") -> None:
    if prompt_text and not _composer_text_matches(prompt_text):
        raise RuntimeError("CHATGPT_PROMPT_INTEGRITY_MISMATCH: refusing to send a corrupted draft")
    before = _page_state()
    previous_count = int(before.get("count") or 0)
    before_url = str(before.get("url") or "")
    last_state: dict[str, Any] = {}
    consumed_since: float | None = None

    def accepted(state: dict[str, Any]) -> bool:
        nonlocal consumed_since
        try:
            composer_consumed = _composer_text_length() == 0 and _attachment_count() == 0
        except Exception:
            composer_consumed = False
        consumed_since = (consumed_since or time.monotonic()) if composer_consumed else None
        current_url = str(state.get("url") or "")
        submission_signal = (
            int(state.get("count") or 0) > previous_count
            or bool(state.get("busy"))
            or ("/c/" in current_url and current_url != before_url)
        )
        # ChatGPT can consume the composer just before its URL/busy state
        # updates. Treat a stable empty composer as accepted after a short
        # grace period, but never accept while the draft or attachments remain.
        stable_consumed = bool(
            consumed_since is not None and time.monotonic() - consumed_since >= 2
        )
        return composer_consumed and (submission_signal or stable_consumed)

    def wait_for_acceptance(seconds: int = 12) -> bool:
        nonlocal last_state
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            last_state = _page_state()
            if accepted(last_state):
                return True
            time.sleep(1)
        return False

    # ChatGPT changes the submit button selector often. A direct DOM search for
    # either the stable test id or the localized aria label has proven more
    # reliable than a single Playwright selector on the home composer.
    try:
        result = _eval_timeout(r'''JSON.stringify((() => {
          const buttons = [...document.querySelectorAll('button')];
          const button = buttons.find(node => (
            node.getAttribute('data-testid') === 'send-button'
            || node.getAttribute('data-testid') === 'composer-submit-button'
            || /发送提示|send/i.test((node.getAttribute('aria-label') || '') + ' ' + (node.innerText || ''))
          ));
          if (!button) return {clicked: false, reason: 'not_found', button_count: buttons.length};
          const disabled = button.disabled || button.getAttribute('aria-disabled') === 'true';
          if (disabled) return {clicked: false, reason: 'disabled', label: button.getAttribute('aria-label') || ''};
          button.click();
          try { button.closest('form')?.requestSubmit?.(button); } catch (error) {}
          return {clicked: true, label: button.getAttribute('aria-label') || '', testid: button.getAttribute('data-testid') || ''};
        })())''', timeout=15)
        if isinstance(result, dict) and result.get("clicked") and wait_for_acceptance():
            _ensure_prompt_submission_started(prompt_text, previous_count=previous_count)
            return
    except Exception:
        pass

    # Use a real Playwright click next. ChatGPT may ignore synthetic
    # HTMLElement.click()/requestSubmit calls in some UI variants, while other
    # variants hide the button from selector lookup but accept the DOM path.
    selectors = (
        'button[data-testid="send-button"]',
        'button[data-testid="composer-submit-button"]',
        'button[data-testid="fruitjuice-send-button"]',
    )
    for selector in selectors:
        try:
            _run("click", selector, timeout=15)
            if wait_for_acceptance():
                _ensure_prompt_submission_started(prompt_text, previous_count=previous_count)
                return
        except Exception:
            pass

    # Fall back to form submission for UI variants without a stable selector.
    try:
        result = _eval_timeout(_chatgpt_send_button_js(click=True), timeout=15)
        if isinstance(result, dict) and result.get("sendable") and wait_for_acceptance():
            _ensure_prompt_submission_started(prompt_text, previous_count=previous_count)
            return
    except Exception:
        pass

    # Enter is the only reliable keyboard submit shortcut across Windows and
    # macOS. Modifier+Enter can insert a newline in some ChatGPT composers.
    try:
        _run("focus", "#prompt-textarea", timeout=10)
    except Exception:
        try:
            _run("focus", '[contenteditable="true"][role="textbox"]', timeout=10)
        except Exception:
            pass
    try:
        _run("press", "Enter", timeout=15)
        if wait_for_acceptance():
            _ensure_prompt_submission_started(prompt_text, previous_count=previous_count)
            return
    except Exception:
        pass

    # One final real click covers a button that became enabled after focus.
    try:
        _run("click", 'button[data-testid="send-button"]', timeout=15)
        if wait_for_acceptance():
            _ensure_prompt_submission_started(prompt_text, previous_count=previous_count)
            return
    except Exception:
        pass
    raise RuntimeError(
        "ChatGPT did not consume the prompt after real click/form-submit/Enter; "
        f"composer_length={_composer_text_length()} attachments={_attachment_count()} "
        f"button={_attachment_upload_state()} page={str(last_state)[:500]}"
    )


def _force_clear_composer_fast() -> None:
    """Clear ChatGPT drafts with bounded DOM operations.

    ChatGPT can preserve a failed 20k-character draft on the home page. The
    older cleanup path used execCommand and a default 180s browser timeout,
    which could hold a project slot without doing useful work. Keep every
    cleanup attempt short so self-heal can requeue quickly.
    """
    _eval_timeout(r'''(() => {
      const composer = document.querySelector('form textarea')?.closest('form')
        || document.querySelector('#prompt-textarea')?.closest('form')
        || document.querySelector('[contenteditable="true"]')?.closest('form')
        || document;
      const editor = composer.querySelector('#prompt-textarea,[contenteditable="true"][role="textbox"],div.ProseMirror,textarea');
      const outsideEditor = node => !(editor && editor.contains(node));
      const visible = node => {
        const rect = node.getBoundingClientRect();
        return rect.width >= 8 && rect.height >= 8;
      };
      for (const button of [...composer.querySelectorAll('button,[role="button"]')]) {
        if (!outsideEditor(button) || !visible(button)) continue;
        const label=((button.getAttribute('aria-label')||'')+' '+(button.innerText||'')+' '+(button.title||'')).toLowerCase();
        if (/remove file|remove attachment|delete file|cancel upload|close|移除文件|删除文件|取消上传/.test(label)) {
          try { button.click(); } catch (error) {}
        }
      }
      const editors = [...document.querySelectorAll('#prompt-textarea,[contenteditable="true"][role="textbox"],div.ProseMirror,textarea,[contenteditable="true"]')];
      for (const node of editors) {
        try { node.focus(); } catch (error) {}
        if ('value' in node) {
          const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(node), 'value')?.set;
          setter ? setter.call(node, '') : (node.value = '');
        }
        if (node.isContentEditable || node.getAttribute('contenteditable') === 'true' || node.matches('div.ProseMirror')) {
          node.textContent = '';
          node.innerHTML = node.matches('div.ProseMirror') ? '<p><br></p>' : '';
        }
        for (const eventName of ['beforeinput','input','change','keyup']) {
          try {
            node.dispatchEvent(new InputEvent(eventName, {bubbles:true, composed:true, inputType:'deleteContentBackward', data:null}));
          } catch (error) {
            node.dispatchEvent(new Event(eventName, {bubbles:true, composed:true}));
          }
        }
      }
      return true;
    })()''', timeout=10)


def _clear_composer_strict() -> None:
    for attempt in range(7):
        try:
            _force_clear_composer_fast()
        except Exception:
            try:
                _run("focus", "#prompt-textarea", timeout=10)
                _run("press", "Control+A", timeout=10)
                _run("press", "Backspace", timeout=10)
            except Exception:
                pass
        try:
            _eval(r'''(() => {
              const composer = document.querySelector('form textarea')?.closest('form')
                || document.querySelector('#prompt-textarea')?.closest('form')
                || document.querySelector('[contenteditable="true"]')?.closest('form')
                || document;
              const editor = composer.querySelector('#prompt-textarea,[contenteditable="true"][role="textbox"],div.ProseMirror,textarea');
              for (const button of [...composer.querySelectorAll('button,[role="button"]')]) {
                if (editor && editor.contains(button)) continue;
                const label=((button.getAttribute('aria-label')||'')+' '+(button.innerText||'')+' '+(button.title||'')).toLowerCase();
                if (
                  /remove file|remove attachment|delete file|cancel upload/.test(label)
                  || /\u79fb\u9664\u6587\u4ef6|\u5220\u9664\u6587\u4ef6|\u53d6\u6d88\u4e0a\u4f20/.test(label)
                ) button.click();
              }
              return true;
            })()''')
        except Exception:
            pass
        time.sleep(1)
        if _attachment_count() == 0 and _composer_text_length() == 0:
            return
        if attempt == 3:
            try:
                _run("open", DEFAULT_CHATGPT_URL, timeout=_navigation_timeout())
                _composer_ready(60)
            except Exception:
                pass
    raise RuntimeError(
        f"ChatGPT composer still has {_composer_text_length()} characters and "
        f"{_attachment_count()} attachment(s) after cleanup"
    )


def _clear_composer_best_effort() -> None:
    try:
        _clear_composer_strict()
    except Exception:
        try:
            _clear_composer()
        except Exception:
            pass


def _ensure_temporary_chat_best_effort() -> bool:
    """Start a history-free ChatGPT conversation when the current UI supports it.

    Temporary Chat is preferred over deleting conversations after the fact:
    generated media and JSON are persisted locally before the stage returns,
    while the browser account does not accumulate content-factory history.
    UI changes must not stop production, so an unavailable toggle falls back
    to a normal conversation and lets the stage continue.
    """
    if os.getenv("HERMES_CHATGPT_TEMPORARY_CHAT", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    try:
        result = _eval(r'''(() => {
          const visible = node => {
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width >= 8 && rect.height >= 8 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const label = node => ((node.innerText || '') + ' ' + (node.getAttribute('aria-label') || '') + ' ' + (node.title || '')).trim();
          const temporary = value => /temporary\s*(chat)?|临时(?:聊天|对话)?/i.test(value || '');
          const active = node => {
            const pressed = (node.getAttribute('aria-pressed') || '').toLowerCase();
            const checked = (node.getAttribute('aria-checked') || '').toLowerCase();
            const state = (node.getAttribute('data-state') || '').toLowerCase();
            return pressed === 'true' || checked === 'true' || state === 'on' || state === 'checked' || state === 'active';
          };
          const nodes = [...document.querySelectorAll('button,[role="button"],[role="switch"],[role="menuitem"]')]
            .filter(node => visible(node) && temporary(label(node)));
          if (nodes.some(active)) return JSON.stringify({ok:true, active:true, clicked:false});
          const preferred = nodes.find(node => {
            const rect = node.getBoundingClientRect();
            return rect.top < Math.max(220, window.innerHeight * 0.3) && rect.left > window.innerWidth * 0.35;
          }) || nodes[0];
          if (!preferred) return JSON.stringify({ok:false, reason:'temporary-toggle-not-found'});
          preferred.click();
          return JSON.stringify({ok:true, active:false, clicked:true, label:label(preferred)});
        })()''', isolated=False)
        if not isinstance(result, dict) or not result.get("ok"):
            return False
        if result.get("clicked"):
            time.sleep(2)
            # Some accounts show a one-time explainer after enabling Temporary
            # Chat. Confirm only benign acknowledgement/continue controls.
            try:
                _eval(r'''(() => {
                  const visible = node => {
                    const rect = node.getBoundingClientRect();
                    return rect.width >= 8 && rect.height >= 8;
                  };
                  const benign = /^(continue|got it|okay|ok|start temporary chat|继续|知道了|明白了|开始临时聊天)$/i;
                  for (const node of [...document.querySelectorAll('[role="dialog"] button,[data-radix-portal] button')]) {
                    const value = ((node.innerText || '') + ' ' + (node.getAttribute('aria-label') || '')).trim();
                    if (visible(node) && benign.test(value)) {
                      node.click();
                      return true;
                    }
                  }
                  return false;
                })()''', isolated=False)
            except Exception:
                pass
            time.sleep(1)
            _composer_ready(60)
        return True
    except Exception as exc:
        logger.warning("Could not enable ChatGPT Temporary Chat; using normal chat: %s", exc)
        return False


def _ensure_normal_chat_for_visual_stage() -> bool:
    """Move image-generation stages out of Temporary Chat.

    A project keeps one browser slot for its lifetime. A preceding text stage
    may therefore leave that slot on a Temporary Chat composer, where some
    ChatGPT accounts cannot invoke image generation. Composer availability is
    not enough to distinguish the modes, so visual stages verify the URL and
    the visible refusal text before they upload or submit anything.
    """
    def current_url() -> str:
        state = _page_state(isolated=False)
        return str(state.get("url") or "").strip()

    normal_url = "https://chatgpt.com/?temporary-chat=false"
    active_url = current_url()
    normalized_url = active_url.lower()
    already_normal = "temporary-chat=false" in normalized_url
    needs_normal_navigation = (
        not already_normal
        or "temporary-chat=true" in normalized_url
    )
    if needs_normal_navigation:
        _run("open", normal_url, timeout=_navigation_timeout())
        if not _composer_ready(60):
            raise RuntimeError("Normal ChatGPT composer did not become ready for visual generation")
        active_url = current_url()
    # Refusal text from a previous temporary conversation can remain in the
    # sidebar or restored DOM after normal mode is already active. The current
    # URL is the authoritative mode signal; treating historical body text as
    # live state makes every later visual retry fail before it can submit.
    if "temporary-chat=true" in active_url.lower():
        raise RuntimeError("ChatGPT visual stage is still trapped in Temporary Chat")
    return True


def delete_chatgpt_conversation_best_effort(*, cdp_url: str, chat_url: str) -> bool:
    """Delete one captured content-factory chat through its owning browser.

    The caller supplies the exact stage URL and user-owned CDP route. No
    sidebar enumeration is performed, so another project or a user's personal
    conversation cannot be selected accidentally.
    """
    url = str(chat_url or "").strip()
    route = str(cdp_url or "").strip().rstrip("/")
    if not route or not re.match(r"^https://chatgpt\.com/(?:c|g)/", url, flags=re.IGNORECASE):
        return False
    global CDP_URL
    previous_route = CDP_URL
    try:
        CDP_URL = route
        _run("open", url, timeout=_navigation_timeout())
        time.sleep(1.5)
        result = _eval_timeout(r'''(async () => {
          const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
          const visible = node => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width >= 8 && rect.height >= 8 && style.display !== 'none' && style.visibility !== 'hidden';
          };
          const label = node => ((node.innerText || '') + ' ' + (node.getAttribute('aria-label') || '') + ' ' + (node.title || '')).trim();
          const optionPattern = /^(more|more options|conversation options|open conversation options|更多|更多选项|对话选项|打开对话选项)$/i;
          const deletePattern = /^(delete|delete chat|delete conversation|删除|删除聊天|删除对话)$/i;

          const currentPath = location.pathname;
          const exactLink = [...document.querySelectorAll(`a[href="${CSS.escape(currentPath)}"]`)].find(visible);
          const containers = [
            document.querySelector('main header'),
            document.querySelector('main'),
            exactLink?.closest('li'),
            exactLink?.parentElement?.parentElement,
          ].filter(Boolean);
          let menuButton = document.querySelector('button[data-testid="conversation-options-button"]');
          for (const container of containers) {
            if (menuButton) break;
            menuButton = [...container.querySelectorAll('button,[role="button"]')]
              .find(node => visible(node) && optionPattern.test(label(node)));
          }
          if (!menuButton) {
            menuButton = [...document.querySelectorAll('button,[role="button"]')].find(node => {
              if (!visible(node) || !optionPattern.test(label(node))) return false;
              const rect = node.getBoundingClientRect();
              return rect.top < 120 && rect.left > window.innerWidth * 0.5;
            });
          }
          if (!menuButton) return JSON.stringify({ok:false, step:'conversation-menu'});
          menuButton.click();
          await sleep(600);

          const deleteItem = document.querySelector('[data-testid="delete-chat-menu-item"]')
            || [...document.querySelectorAll('[role="menuitem"], [role="menu"] button, [data-radix-menu-content] button')]
            .find(node => visible(node) && deletePattern.test(label(node)));
          if (!deleteItem) return JSON.stringify({ok:false, step:'delete-menuitem'});
          deleteItem.click();
          await sleep(600);

          const confirm = document.querySelector('[data-testid="delete-conversation-confirm-button"]')
            || [...document.querySelectorAll('[role="dialog"] button, [data-radix-portal] button')]
            .find(node => visible(node) && deletePattern.test(label(node)));
          if (!confirm) return JSON.stringify({ok:false, step:'delete-confirm'});
          confirm.click();
          await sleep(800);
          return JSON.stringify({ok:true, step:'deleted'});
        })()''', timeout=20, isolated=False)
        return bool(isinstance(result, dict) and result.get("ok"))
    except Exception as exc:
        logger.warning("Could not delete captured ChatGPT conversation %s: %s", url, exc)
        return False
    finally:
        CDP_URL = previous_route


def _download_visual_from_page_chunks(signature: str, *, attempt: int) -> dict[str, Any] | None:
    """Read a generated image through the authenticated page in small CDP frames.

    ChatGPT estuary URLs require the browser session. Returning a multi-megabyte
    base64 payload in one Runtime.evaluate result can exceed the CDP transport
    limit, so keep it in page memory and retrieve bounded chunks instead.
    """
    target_url = json.dumps(str(signature))
    store_key_value = hashlib.sha256(f"{signature}:{attempt}".encode("utf-8")).hexdigest()[:24]
    store_key = json.dumps(store_key_value)
    initialized: dict[str, Any] | None = None
    try:
        initialized = _eval_timeout(f'''(async () => {{
          const targetUrl = {target_url};
          const storeKey = {store_key};
          window.__hermesVisualDownloads = window.__hermesVisualDownloads || {{}};
          const blobToData = blob => new Promise((resolve, reject) => {{
            const reader = new FileReader();
            reader.onerror = () => reject(reader.error || new Error('FileReader failed'));
            reader.onload = () => resolve(String(reader.result || '').split(',', 2)[1] || '');
            reader.readAsDataURL(blob);
          }});
          try {{
            let mimeType = 'image/png';
            let data = '';
            let source = 'authenticated_fetch';
            let fetchError = '';
            try {{
              const controller = new AbortController();
              const timer = setTimeout(() => controller.abort(), 30000);
              try {{
                const response = await fetch(targetUrl, {{credentials: 'include', signal: controller.signal}});
                if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
                const blob = await response.blob();
                mimeType = blob.type || mimeType;
                data = await blobToData(blob);
              }} finally {{
                clearTimeout(timer);
              }}
            }} catch (error) {{
              fetchError = String(error);
              const absoluteTarget = new URL(targetUrl, location.href).href;
              const image = [...document.images].find(node => {{
                const current = new URL(node.currentSrc || node.src || '', location.href).href;
                return current === absoluteTarget && node.complete && node.naturalWidth > 0 && node.naturalHeight > 0;
              }});
              if (!image) throw new Error(`fetch failed and rendered image missing: ${{fetchError}}`);
              const canvas = document.createElement('canvas');
              canvas.width = image.naturalWidth;
              canvas.height = image.naturalHeight;
              const context = canvas.getContext('2d');
              if (!context) throw new Error('canvas context unavailable');
              context.drawImage(image, 0, 0);
              data = canvas.toDataURL('image/png').split(',', 2)[1] || '';
              mimeType = 'image/png';
              source = 'rendered_image_canvas';
            }}
            if (!data) throw new Error('empty image payload');
            window.__hermesVisualDownloads[storeKey] = {{data, type: mimeType, source}};
            return JSON.stringify({{ok: true, type: mimeType, source, length: data.length}});
          }} catch (error) {{
            delete window.__hermesVisualDownloads[storeKey];
            return JSON.stringify({{ok: false, error: String(error)}});
          }}
        }})()''', timeout=55, isolated=False)
        if not isinstance(initialized, dict) or not initialized.get("ok"):
            return None
        length = int(initialized.get("length") or 0)
        if length <= 0 or length > 80 * 1024 * 1024:
            return None
        chunk_size = 256 * 1024
        chunks: list[str] = []
        for offset in range(0, length, chunk_size):
            end = min(length, offset + chunk_size)
            chunk = _eval_timeout(
                f'''(() => {{
                  const item = window.__hermesVisualDownloads?.[{store_key}];
                  return item ? item.data.slice({offset}, {end}) : '';
                }})()''',
                timeout=20,
                isolated=False,
            )
            if not isinstance(chunk, str) or not chunk:
                return None
            chunks.append(chunk)
        data = "".join(chunks)
        if len(data) != length:
            return None
        return {
            "ok": True,
            "type": str(initialized.get("type") or "image/png"),
            "source": str(initialized.get("source") or "authenticated_fetch"),
            "data": data,
        }
    except Exception:
        return None
    finally:
        try:
            _eval_timeout(
                f'''(() => {{
                  if (window.__hermesVisualDownloads) delete window.__hermesVisualDownloads[{store_key}];
                  return true;
                }})()''',
                timeout=10,
                isolated=False,
            )
        except Exception:
            pass


def _download_visual_via_cdp_resource(signature: str) -> dict[str, Any] | None:
    """Load an authenticated ChatGPT image as a CDP network resource stream.

    This bypasses page CORS and avoids putting a multi-megabyte base64 value in
    one Runtime.evaluate response. It is a fallback for Estuary links that are
    rendered in Chrome but reject ordinary page fetches.
    """
    try:
        from websockets.sync.client import connect

        route = urllib.parse.urlparse(str(CDP_URL).rstrip("/"))
        with urllib.request.urlopen(str(CDP_URL).rstrip("/") + "/json/list", timeout=12) as response:
            targets = json.loads(response.read().decode("utf-8"))
        target = next(
            (
                item for item in targets
                if str(item.get("type") or "") == "page"
                and "chatgpt.com" in str(item.get("url") or "").lower()
            ),
            None,
        )
        if not isinstance(target, dict):
            return None
        debugger = urllib.parse.urlparse(str(target.get("webSocketDebuggerUrl") or ""))
        if not debugger.path:
            return None
        websocket_scheme = "wss" if route.scheme == "https" else "ws"
        debugger_url = urllib.parse.urlunparse((
            websocket_scheme,
            route.netloc,
            debugger.path,
            debugger.params,
            debugger.query,
            debugger.fragment,
        ))
        command_id = 0

        def command(socket, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            nonlocal command_id
            command_id += 1
            expected_id = command_id
            socket.send(json.dumps({"id": expected_id, "method": method, "params": params or {}}))
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                payload = json.loads(socket.recv(timeout=max(1, deadline - time.monotonic())))
                if int(payload.get("id") or 0) != expected_id:
                    continue
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                return dict(payload.get("result") or {})
            raise TimeoutError(f"CDP command timed out: {method}")

        with connect(
            debugger_url,
            origin="http://localhost",
            proxy=None,
            open_timeout=12,
            close_timeout=2,
            max_size=None,
        ) as socket:
            frame_tree = command(socket, "Page.getFrameTree")
            frame_id = str(
                dict(dict(frame_tree.get("frameTree") or {}).get("frame") or {}).get("id") or ""
            )
            if not frame_id:
                return None
            loaded = command(socket, "Network.loadNetworkResource", {
                "frameId": frame_id,
                "url": str(signature),
                "options": {"disableCache": False, "includeCredentials": True},
            })
            resource = dict(loaded.get("resource") or {})
            stream = str(resource.get("stream") or "")
            if not bool(resource.get("success")) or not stream:
                return None
            body = bytearray()
            try:
                while True:
                    item = command(socket, "IO.read", {"handle": stream, "size": 256 * 1024})
                    chunk = str(item.get("data") or "")
                    if chunk:
                        body.extend(base64.b64decode(chunk) if item.get("base64Encoded") else chunk.encode("latin-1"))
                    if len(body) > 80 * 1024 * 1024:
                        return None
                    if item.get("eof"):
                        break
            finally:
                try:
                    command(socket, "IO.close", {"handle": stream})
                except Exception:
                    pass
            if not body:
                return None
            headers = dict(resource.get("headers") or {})
            mime_type = str(
                headers.get("content-type") or headers.get("Content-Type") or "image/png"
            ).split(";", 1)[0]
            return {
                "ok": True,
                "type": mime_type,
                "source": "cdp_network_resource",
                "data": base64.b64encode(bytes(body)).decode("ascii"),
            }
    except Exception as exc:
        logger.warning("CDP authenticated image load failed for %s: %s", str(signature)[:120], exc)
        return None


def _persist_visuals(
    output_dir: Path,
    stage: str,
    image_signatures: list[str],
    expected: int,
    *,
    start_index: int = 1,
    total_expected: int | None = None,
) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    signatures = list(dict.fromkeys(str(value) for value in image_signatures if str(value)))
    saved: list[str] = []
    extension_by_type = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
    total = int(total_expected or expected)
    for offset, signature in enumerate(signatures[-expected:]):
        index = start_index + offset
        result: dict[str, Any] | None = None
        for attempt in range(1, 4):
            candidate = _download_visual_from_page_chunks(signature, attempt=attempt)
            if isinstance(candidate, dict) and candidate.get("ok") and candidate.get("data"):
                result = candidate
                break
            time.sleep(2 * attempt)
        if result is None:
            candidate = _download_visual_via_cdp_resource(signature)
            if isinstance(candidate, dict) and candidate.get("ok") and candidate.get("data"):
                result = candidate
        if result is None or not result.get("data"):
            raise RuntimeError(f"Could not download generated ChatGPT image for {stage}: {signature[:180]}")
        mime_type = str(result.get("type") or "image/png").split(";", 1)[0].lower()
        stem = f"{stage.lower()}-board-{index:02d}" if total > 1 else f"{stage.lower()}-{index}"
        target = output_dir / f"{stem}{extension_by_type.get(mime_type, '.png')}"
        target.write_bytes(base64.b64decode(str(result["data"]), validate=True))
        if target.stat().st_size:
            saved.append(str(target))
    return saved


def _existing_visual_board_path(output_dir: Path, stage: str, board_index: int, board_count: int) -> str | None:
    stem = f"{stage.lower()}-board-{board_index:02d}" if board_count > 1 else f"{stage.lower()}-1"
    for extension in (".png", ".jpg", ".jpeg", ".webp"):
        candidate = output_dir / f"{stem}{extension}"
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return str(candidate)
        except OSError:
            continue
    return None


def _generated_images_for_request(state: dict[str, Any], marker: str) -> list[str]:
    """Return media from the newest completed turn matching ``marker``.

    A bounded fresh recovery can race a late image: the old turn finishes after
    the first probe but just before the fresh prompt is submitted. If both turns
    carry the same idempotency marker, blindly selecting the last marker hides
    the valid image that belongs to the preceding completed turn. Prefer the
    newest matching turn that actually has attributable generated media.
    """
    records = list(state.get("conversationRecords") or [])
    completed_turns: list[list[str]] = []
    for index, record in enumerate(records):
        if str(record.get("role") or "") != "user" or marker not in str(record.get("text") or ""):
            continue
        images: list[str] = []
        for candidate in records[index + 1:]:
            role = str(candidate.get("role") or "")
            if role == "user":
                break
            if role == "assistant":
                images.extend(
                    str(value)
                    for value in list(candidate.get("generatedImages") or [])
                    if str(value).strip()
                )
        if images:
            completed_turns.append(list(dict.fromkeys(images)))
    return completed_turns[-1] if completed_turns else []


def _visual_request_turn_status(state: dict[str, Any], marker: str) -> str:
    """Classify one exact visual request without authorizing a duplicate send."""
    records = list(state.get("conversationRecords") or [])
    matching_indexes = [
        index
        for index, record in enumerate(records)
        if str(record.get("role") or "") == "user"
        and marker in str(record.get("text") or "")
    ]
    if not matching_indexes:
        return "absent"
    if _generated_images_for_request(state, marker):
        return "completed"
    if bool(state.get("busy")):
        return "busy"
    return "terminal_no_media"


def _persist_visual_board_from_state(
    output_dir: Path,
    stage: str,
    state: dict[str, Any],
    marker: str,
    board_index: int,
    board_count: int,
) -> str | None:
    images = _generated_images_for_request(state, marker)
    if not images:
        return None
    saved = _persist_visuals(
        output_dir,
        stage,
        [images[-1]],
        1,
        start_index=board_index,
        total_expected=board_count,
    )
    return saved[0] if saved else None


def _recover_visible_visuals(
    packet: dict[str, Any],
    output_dir: Path,
    stage: str,
    *,
    state: dict[str, Any] | None = None,
    exclude_signatures: set[str] | None = None,
) -> tuple[str, str | None, list[str]] | None:
    if stage not in VISUAL_STAGES:
        return None
    expected = _expected_visual_count(packet, stage)
    try:
        page_state = state if isinstance(state, dict) else _page_state()
        excluded = {str(value) for value in (exclude_signatures or set()) if str(value).strip()}
        generated_images = [
            str(value)
            for value in (page_state.get("generatedImages") or [])
            if str(value).strip() and str(value) not in excluded
        ]
        if len(generated_images) < expected:
            return None
        saved = _persist_visuals(output_dir, stage, generated_images[-expected:], expected)
        if len(saved) != expected:
            return None
        return _preferred_response_text(page_state), str(page_state.get("url") or "") or None, saved
    except Exception:
        return None



def _recover_visible_visuals_from_project_tabs(
    packet: dict[str, Any],
    output_dir: Path,
    stage: str,
    *,
    exclude_signatures: set[str] | None = None,
) -> tuple[str, str | None, list[str]] | None:
    """Find an already-generated visual in this user's bound browser slot.

    Browser focus often moves back to the home composer after a worker restart.
    Looking only at the active tab loses the generated image and causes an
    expensive duplicate visual run. Scan only this CDP slot, and only accept
    tabs whose DOM still contains this project's execution/project marker.
    """
    if stage not in VISUAL_STAGES:
        return None
    execution_id = str(packet.get("execution_id") or "").strip()
    project_id = str(packet.get("project_id") or "").strip()
    if not execution_id and not project_id:
        return None
    try:
        tabs = _list_tabs()
    except Exception:
        return None
    active_tab = next((str(tab.get("tabId") or "") for tab in tabs if tab.get("active")), "")
    tab_ids = [str(tab.get("tabId") or "") for tab in tabs if str(tab.get("tabId") or "")]
    try:
        for tab_id in tab_ids:
            if tab_id:
                _activate_tab(tab_id)
                time.sleep(0.4)
            try:
                state = _page_state(isolated=False)
            except Exception:
                continue
            if execution_id and not _state_has_execution_request(state, execution_id):
                continue
            if not execution_id and project_id and not _state_has_execution_request(state, project_id):
                continue
            recovered = _recover_visible_visuals(
                packet,
                output_dir,
                stage,
                state=state,
                exclude_signatures=exclude_signatures,
            )
            if recovered is not None:
                return recovered
    finally:
        if active_tab:
            try:
                _activate_tab(active_tab)
            except Exception:
                pass
    return None


def _state_has_execution_request(state: dict[str, Any], execution_id: str) -> bool:
    """Return true only when a user turn contains this exact execution marker.

    Whole-page matching is unsafe because project identifiers can also appear
    in assistant output, stale sidebars, and unrelated recovery diagnostics.
    The user turn is the durable idempotency key for one browser submission.
    """
    marker = str(execution_id or "").strip()
    if not marker:
        return False
    return any(
        marker in str(value or "")
        for value in list(state.get("userMessageTexts") or [])
    )


def _collect_inflight_visual_from_project_tabs(
    packet: dict[str, Any],
    output_dir: Path,
    stage: str,
) -> tuple[str, str | None, list[str]] | None:
    """Resume an already-submitted visual request without sending it again.

    Image generation often outlives one Celery collector window. A retry must
    attach to the user turn carrying the same execution_id and wait for its
    assistant media. It may submit again only when that matching turn is no
    longer busy and did not produce the required media.
    """
    if stage not in VISUAL_STAGES:
        return None
    execution_id = str(packet.get("execution_id") or "").strip()
    if not execution_id:
        return None
    expected = _expected_visual_count(packet, stage)
    tabs = _list_tabs()
    active_tab = next((str(tab.get("tabId") or "") for tab in tabs if tab.get("active")), "")
    try:
        for tab in tabs:
            tab_id = str(tab.get("tabId") or "").strip()
            if not tab_id or not _activate_tab(tab_id):
                continue
            time.sleep(0.4)
            try:
                state = _page_state(isolated=False)
            except Exception:
                continue
            if not _state_has_execution_request(state, execution_id):
                continue
            _raise_if_chatgpt_login_required(state)
            _raise_if_rate_limited(state)
            recovered = _recover_visible_visuals(
                packet,
                output_dir,
                stage,
                state=state,
            )
            if recovered is not None:
                return recovered
            if not state.get("busy"):
                return None
            waited = _wait_for_answer(
                0,
                set(),
                timeout_seconds=int(packet.get("visual_inflight_collect_seconds") or 600),
                minimum_images=expected,
                packet=packet,
            )
            generated_images = [
                str(value)
                for value in list(waited.get("generatedImages") or [])
                if str(value).strip()
            ]
            if len(generated_images) < expected:
                raise ChatGPTStageError(
                    "CHATGPT_RESPONSE_STILL_RUNNING: matching visual request has not produced all boards",
                    raw_text=_preferred_response_text(waited),
                    chat_url=str(waited.get("url") or "") or None,
                )
            saved = _persist_visuals(output_dir, stage, generated_images[-expected:], expected)
            if len(saved) != expected:
                raise RuntimeError(
                    f"ChatGPT completed with {len(saved)} persistable visuals; expected {expected}"
                )
            return _preferred_response_text(waited), str(waited.get("url") or "") or None, saved
    finally:
        if active_tab:
            try:
                _activate_tab(active_tab)
            except Exception:
                pass
    return None


def _collect_visual_board_from_project_tabs(
    packet: dict[str, Any],
    output_dir: Path,
    stage: str,
    *,
    marker: str,
    board_index: int,
    board_count: int,
    allow_legacy_marker: bool = False,
    wait_if_busy: bool = True,
) -> tuple[str, str | None, str] | None:
    """Collect exactly one board from its own browser request turn."""
    execution_id = str(packet.get("execution_id") or "").strip()
    tabs = _list_tabs()
    original_tab = next((str(tab.get("tabId") or "") for tab in tabs if tab.get("active")), "")
    matched_tab = False
    try:
        for tab in tabs:
            tab_id = str(tab.get("tabId") or "").strip()
            if not tab_id or not _activate_tab(tab_id):
                continue
            time.sleep(0.4)
            try:
                state = _page_state(isolated=False)
            except Exception:
                continue
            request_marker = marker
            marker_found = _state_has_execution_request(state, marker)
            if not marker_found and allow_legacy_marker and execution_id:
                has_board_markers = any(
                    "::visual-board:" in str(value or "")
                    for value in list(state.get("userMessageTexts") or [])
                )
                if not has_board_markers and _state_has_execution_request(state, execution_id):
                    request_marker = execution_id
                    marker_found = True
            if not marker_found:
                continue
            matched_tab = True
            _raise_if_chatgpt_login_required(state)
            _raise_if_rate_limited(state)
            saved = _persist_visual_board_from_state(
                output_dir,
                stage,
                state,
                request_marker,
                board_index,
                board_count,
            )
            if saved:
                return _preferred_response_text(state), str(state.get("url") or "") or None, saved
            request_status = _visual_request_turn_status(state, request_marker)
            if request_status == "terminal_no_media":
                raise ChatGPTStageError(
                    (
                        "CHATGPT_VISUAL_MARKER_TERMINAL_NO_MEDIA: "
                        f"visual board {board_index}/{board_count} request already exists and stopped "
                        "without attributable generated media; refusing to submit the same generation twice"
                    ),
                    raw_text=_preferred_response_text(state),
                    chat_url=str(state.get("url") or "") or None,
                )
            if request_status != "busy":
                return None
            if not wait_if_busy:
                # A bounded fresh-regeneration delivery must not attach to the
                # same permanently stalled assistant turn again. The caller
                # will stop that exact marker-scoped turn before resubmitting.
                return None
            request_images = set(_generated_images_for_request(state, request_marker))
            previous_images = {
                str(value)
                for value in list(state.get("generatedImages") or [])
                if str(value).strip() and str(value) not in request_images
            }
            waited = _wait_for_answer(
                int(state.get("count") or 0),
                previous_images,
                timeout_seconds=int(packet.get("visual_inflight_collect_seconds") or 600),
                minimum_images=1,
                packet=packet,
            )
            saved = _persist_visual_board_from_state(
                output_dir,
                stage,
                waited,
                request_marker,
                board_index,
                board_count,
            )
            if not saved:
                new_images = [str(value) for value in list(waited.get("newImages") or []) if str(value).strip()]
                if new_images:
                    persisted = _persist_visuals(
                        output_dir,
                        stage,
                        [new_images[-1]],
                        1,
                        start_index=board_index,
                        total_expected=board_count,
                    )
                    saved = persisted[0] if persisted else None
            if not saved:
                raise ChatGPTStageError(
                    f"CHATGPT_RESPONSE_STILL_RUNNING: visual board {board_index}/{board_count} has no attributable image",
                    raw_text=_preferred_response_text(waited),
                    chat_url=str(waited.get("url") or "") or None,
                )
            return _preferred_response_text(waited), str(waited.get("url") or "") or None, saved
    finally:
        if not matched_tab and original_tab:
            try:
                _activate_tab(original_tab)
            except Exception:
                pass
    return None


def _interrupt_stalled_visual_request(marker: str) -> bool:
    """Stop one busy visual request identified by its exact user-turn marker.

    A ChatGPT image turn can retain the Stop control forever without producing
    media. Idempotent recovery must normally keep waiting, but once the bounded
    self-heal plan explicitly requests a fresh render, continuing to attach to
    that dead turn creates an endless retry loop. Only the tab containing the
    exact marker is eligible; unrelated projects and conversations are ignored.
    """
    request_marker = str(marker or "").strip()
    if not request_marker:
        return False
    tabs = _list_tabs()
    original_tab = next((str(tab.get("tabId") or "") for tab in tabs if tab.get("active")), "")
    interrupted = False
    try:
        for tab in tabs:
            tab_id = str(tab.get("tabId") or "").strip()
            if not tab_id or not _activate_tab(tab_id):
                continue
            time.sleep(0.4)
            try:
                state = _page_state(isolated=False)
            except Exception:
                continue
            if not _state_has_execution_request(state, request_marker) or not state.get("busy"):
                continue
            clicked = bool(_eval(r'''(() => {
              const visible = element => {
                if (!element) return false;
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                  && Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
              };
              const controls = [...document.querySelectorAll(
                '[data-testid="stop-button"],button[aria-label*="Stop"],button[aria-label*="\u505c\u6b62"],button'
              )];
              const target = controls.find(item => {
                if (!visible(item)) return false;
                if (item.matches('[data-testid="stop-button"],button[aria-label*="Stop"],button[aria-label*="\u505c\u6b62"]')) return true;
                const label = ((item.innerText || item.textContent || '') + ' ' + (item.getAttribute('aria-label') || '')).trim();
                return /^(stop generating|stop responding|\u505c\u6b62\u751f\u6210|\u505c\u6b62\u56de\u7b54)$/i.test(label);
              });
              if (!target) return false;
              target.click();
              return true;
            })()''', isolated=False))
            if not clicked:
                return False
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                time.sleep(1)
                current = _page_state(isolated=False)
                if not current.get("busy"):
                    interrupted = True
                    break
            return interrupted
    finally:
        if not interrupted and original_tab:
            try:
                _activate_tab(original_tab)
            except Exception:
                pass
    return False


def _activate_project_visual_tab(packet: dict[str, Any]) -> bool:
    execution_id = str(packet.get("execution_id") or "").strip()
    if not execution_id:
        return False
    try:
        tabs = _list_tabs()
    except Exception:
        return False
    for tab in tabs:
        tab_id = str(tab.get("tabId") or "").strip()
        if not tab_id or not _activate_tab(tab_id):
            continue
        time.sleep(0.3)
        try:
            if _state_has_execution_request(_page_state(isolated=False), execution_id):
                return True
        except Exception:
            continue
    return False


def _execute_visual_boards_sequentially(
    packet: dict[str, Any],
    output_dir: Path,
    stage: str,
    files: list[str],
    prompt_template: str,
    boards: list[tuple[str, dict[str, Any]]],
) -> tuple[str, str | None, list[str]]:
    """Generate, persist, and resume multi-board visual work one board at a time."""
    board_count = len(boards)
    saved_paths: list[str] = []
    last_text = ""
    last_url: str | None = None
    opened_composer = False
    force_fresh = bool(packet.get("force_fresh_response", False))
    for position, (board_prompt, board_spec) in enumerate(boards, 1):
        board_index = int(board_spec.get("board_index") or position)
        existing = _existing_visual_board_path(output_dir, stage, board_index, board_count)
        if existing:
            saved_paths.append(existing)
            continue
        marker = _visual_board_execution_marker(packet, board_index, board_count)
        recovered = _collect_visual_board_from_project_tabs(
            packet,
            output_dir,
            stage,
            marker=marker,
            board_index=board_index,
            board_count=board_count,
            allow_legacy_marker=board_index == 1,
            wait_if_busy=not force_fresh,
        )
        if recovered is not None:
            last_text, last_url, saved = recovered
            saved_paths.append(saved)
            opened_composer = True
            continue
        if force_fresh:
            _interrupt_stalled_visual_request(marker)
            # The old turn can finish between the initial capture probe and
            # the Stop click (or the Stop control can disappear because it just
            # completed). Probe once more before submitting a duplicate request.
            recovered = _collect_visual_board_from_project_tabs(
                packet,
                output_dir,
                stage,
                marker=marker,
                board_index=board_index,
                board_count=board_count,
                allow_legacy_marker=board_index == 1,
                wait_if_busy=False,
            )
            if recovered is not None:
                last_text, last_url, saved = recovered
                saved_paths.append(saved)
                opened_composer = True
                continue

        stage_url = "https://chatgpt.com/?temporary-chat=false"
        if not opened_composer:
            if not _activate_project_visual_tab(packet):
                _run("open", stage_url, timeout=_navigation_timeout())
            _ensure_normal_chat_for_visual_stage()
            _enter_chat_composer(stage_url)
            _dismiss_nonblocking_chatgpt_overlays()
            _wait_selector("#upload-files", timeout_seconds=30)
            opened_composer = True
        else:
            _dismiss_nonblocking_chatgpt_overlays()
            _enter_chat_composer(stage_url)

        instruction = _visual_browser_single_board_instruction(board_prompt, board_spec)
        prompt_text = prompt_template.replace(_SEQUENTIAL_BOARD_INSTRUCTION_TOKEN, instruction)
        prompt_text += f"\n\nBROWSER REQUEST MARKER (idempotency only; do not reproduce): {marker}"
        _raise_if_rate_limited(_page_state())
        _clear_composer_strict()
        _fill_prompt_text(prompt_text, timeout=180)
        for file_index, path in enumerate(files, 1):
            _upload_file(path, expected_count=file_index)
        _wake_composer_for_send(prompt_text)
        _wait_until_sendable(expected_attachments=len(files), prompt_text=prompt_text)
        before = _page_state()
        previous_images = {str(value) for value in list(before.get("generatedImages") or []) if str(value).strip()}
        _pace_before_send(len(prompt_text), len(files), packet=packet)
        _send_prompt(prompt_text)
        state = _wait_for_answer(
            int(before.get("count") or 0),
            previous_images,
            timeout_seconds=300,
            minimum_images=1,
            packet=packet,
        )
        saved = _persist_visual_board_from_state(
            output_dir,
            stage,
            state,
            marker,
            board_index,
            board_count,
        )
        if not saved:
            new_images = [str(value) for value in list(state.get("newImages") or []) if str(value).strip()]
            if new_images:
                persisted = _persist_visuals(
                    output_dir,
                    stage,
                    [new_images[-1]],
                    1,
                    start_index=board_index,
                    total_expected=board_count,
                )
                saved = persisted[0] if persisted else None
        if not saved:
            raise RuntimeError(f"ChatGPT completed without persistable visual board {board_index}/{board_count}")
        saved_paths.append(saved)
        last_text = _preferred_response_text(state)
        last_url = str(state.get("url") or "") or None
        _record_chatgpt_success()

    ordered = [
        _existing_visual_board_path(output_dir, stage, index, board_count)
        for index in range(1, board_count + 1)
    ]
    if any(not value for value in ordered):
        raise RuntimeError(
            f"Sequential visual generation persisted {sum(bool(value) for value in ordered)}/{board_count} boards"
        )
    return last_text, last_url, [str(value) for value in ordered if value]


def _execute_chatgpt_stage(packet: dict[str, Any]) -> tuple[str, str | None, list[str]]:
    global CDP_URL, PACING_SCOPE
    requested_cdp_url = str(packet.get("browser_cdp_url") or "").strip()
    if not requested_cdp_url:
        if os.getenv("HERMES_ALLOW_DEFAULT_CDP", "").strip().lower() in {"1", "true", "yes"}:
            requested_cdp_url = str(os.getenv("HERMES_CDP_URL") or "http://127.0.0.1:9222").strip()
        else:
            raise RuntimeError(
                "BROWSER_BRIDGE_ROUTE_MISSING: content-factory stage has no user-owned CDP route; "
                "refusing to fall back to a shared browser"
            )
    CDP_URL = requested_cdp_url
    PACING_SCOPE = str(packet.get("project_id") or "default-project").strip() or "default-project"
    stage = str(packet["current_stage"])
    output_dir = Path(str(packet["browser_output_path"]))
    visual_sequence_count = _expected_visual_count(packet, stage) if stage in VISUAL_STAGES else 0
    # Quota/rate-limit notices can render after the previous broker delivery
    # has already timed out. Inspect the project-owned slot before opening a
    # new page so a late terminal notice becomes a cooldown instead of a
    # duplicate ChatGPT submission.
    _raise_if_live_capacity_limited()
    force_fresh = bool(packet.get("force_fresh_response", False))
    visual_repair = bool(str(packet.get("visual_repair_instruction") or "").strip())
    if stage in VISUAL_STAGES and visual_sequence_count <= 1 and (not force_fresh or not visual_repair):
        inflight_visual = _collect_inflight_visual_from_project_tabs(packet, output_dir, stage)
        if inflight_visual is not None:
            _clear_composer_best_effort()
            return inflight_visual
    if not force_fresh:
        # Never accept visual media that was already visible before this stage
        # sends its own prompt. ChatGPT conversations are sticky; reusing a
        # stale image is how unrelated charts/product crops can leak into a
        # new project and then get split downstream. Visual recovery is still
        # allowed after a send attempt, where attempt_start_images excludes
        # pre-existing media.
        if stage in VISUAL_STAGES and visual_sequence_count <= 1:
            recovered_visuals = _recover_visible_visuals_from_project_tabs(packet, output_dir, stage)
            if recovered_visuals is not None:
                _clear_composer_best_effort()
                return recovered_visuals
        recovered = _existing_stage_response(packet)
        if recovered is not None:
            text, chat_url = recovered
            _clear_composer_best_effort()
            return text, chat_url, []
    files = [str(path) for path in packet.get("browser_asset_paths") or [] if str(path).strip()]
    role_prompt = STAGE_ROLE_PROMPTS.get(stage, "Execute the requested content-factory stage precisely.") + "\n\n"
    expected_visuals = 0
    visual_boards: list[tuple[str, dict[str, Any]]] = []

    if stage in VISUAL_STAGES:
        if stage == "VISUAL_PREVIEW":
            visual_boards = _visual_browser_boards(packet)
        if len(visual_boards) > 1:
            count_instruction = _SEQUENTIAL_BOARD_INSTRUCTION_TOKEN
            expected_visuals = len(visual_boards)
        else:
            count_instruction, expected_visuals = _visual_browser_board_instruction(packet, stage)
        policy = dict(packet.get("video_model_policy") or {})
        face_reference_mode = str(
            policy.get("human_face_reference_mode") or "allowed"
        ).strip().lower()
        face_rule = (
            "Because Seedance is selected, every panel must avoid visible human faces. "
            "Use product packaging, room layout, props, hands-only action, back-of-head, cropped-below-chin, or face-hidden body anchors. "
            "Do not generate portraits, selfies, face-locked characters, or recognizable human face references. "
            if face_reference_mode == "forbidden"
            else (
                "Because Seedance is selected, visible adult faces are allowed only as unmistakably fictional stylized animation. "
                "Use clearly drawn or rendered 2D/2.5D/3D facial planes, skin, eyes, hair, expressions, light, and surfaces. "
                "Do not create photorealistic, hyperreal, live-action, photographic, synthetic-photo, or real-person-looking faces; "
                "avoid realistic pores, skin texture, eye reflections, individual hair strands, photographic bokeh, and photographic lighting. "
                "Preserve the approved adult character, emotion, hook, scene, action, and continuity in this animation medium. "
                if face_reference_mode == "stylized_animation_only"
                else ""
            )
        )
        learned = dict(packet.get("learned_constraints") or {})
        omni_learning = dict(learned.get("omni_flash") or {})
        provider_reference_rule = (
            "The previous reference board was permanently blocked by the video provider. Preserve high dramatic intensity through "
            "shouting, door slamming, forceful gestures, thrown pillows, object impact, abrupt movement, and strong facial emotion. "
            "Do not soften the conflict. Keep clear spatial separation and avoid hands on neck/chest, choking-like poses, restraining contact, "
            "direct assault, blood, or visible injury in every panel so each split reference remains provider-acceptable. "
            if bool(omni_learning.get("avoid_provider_flagged_reference_composition"))
            else ""
        )
        project_requirement = _humanize_project_text(
            packet.get("project_requirements") or packet.get("user_instruction") or packet.get("brief") or ""
        )
        product_visual_lock = _product_visual_lock(packet)
        visual_repair_instruction = str(packet.get("visual_repair_instruction") or "").strip()
        repair_rule = (
            "SELF-HEAL VISUAL REPAIR - highest priority for this attempt: "
            + visual_repair_instruction
            + " Ignore any previously visible wrong canvas in the chat. Generate a brand-new image that satisfies this repair. "
            if visual_repair_instruction
            else ""
        )
        requirement_rule = (
            "PROJECT REQUIREMENT CARRYOVER: Apply this user-approved requirement to the visual board and every later downstream asset: "
            + project_requirement
            + ". Do not soften, dilute, or replace it with generic wellness imagery. "
            if project_requirement
            else ""
        )
        visual_source_rule = _visual_source_instruction(files)
        prompt = (
            role_prompt
            + "PROJECT-GOAL-FIRST VISUAL AUTHORITY: The user has explicitly approved this project objective and creative direction. "
            "Treat the selected creative plan as the highest creative authority after platform hard-safety rules. "
            "All depicted people are fictional AI-generated adults, not real persons. Preserve the requested dramatic intensity: "
            "heated arguments, shouting, door slamming, forceful gestures, objects striking furniture, and non-graphic physical confrontation are allowed when requested. "
            "Do not sanitize an explosive scene into calm conversation or unrelated wellness imagery. "
            "Only exclude gore, visible serious injury, harm to minors, and sexual violence. If a detail crosses one of those hard limits, "
            "replace only that detail with an equally intense non-graphic action while preserving the hook, conflict, pacing, and project goal. "
            "Do not refuse merely because the script contains relationship conflict, anger, family tension, or non-graphic fictional violence. "
            + repair_rule
            + requirement_rule
            + visual_source_rule
            + f"{count_instruction} Call the native ChatGPT image-generation tool now. "
            "Do not squeeze multiple requested boards into one image. Do not create horizontal strips, tiny thumbnails, infographic charts, or product-only crop sheets. "
            "Each panel must remain large and clean enough to be split later into a standalone vertical video reference image. "
            "Do not use Python, code interpreter, canvas code, or create a /mnt/data file. "
            "Display every generated board image directly and visibly inline in this chat response, in board-number order. "
            "In this message, output only the generated image media: do not write JSON, explanations, "
            "summaries, or placeholder asset IDs. "
            + product_visual_lock
            + "Preserve character continuity, room continuity, wardrobe, lighting, and action continuity. "
            + face_rule
            + provider_reference_rule
            + "Do not invent promotions, medical outcomes, unsupported packaging views, or readable overlay text. "
            "Do not publish, purchase, or advance another stage. "
            "The local board specification below is the complete visual-stage payload; do not infer or reproduce unrelated project history."
        )
    else:
        stage_instruction = ""
        if stage == "FACTS":
            stage_instruction = (
                "Use the attached product facts PDF and product images first. Do not browse unless a critical product fact is impossible to decide from uploads. "
                "If browsing/search is unavailable, slow, or causes tool instability, skip browsing and record the gap in source_map and product_truth_handoff. "
                "Keep every fact, citation, upload, and conclusion inside this project's knowledge_namespace; never reuse another project. "
                "Return only one strict JSON object. Do not put inline citations, source chips, markdown links, or bibliography text inside JSON string values; "
                "place source titles and URLs only in result.source_map. "
            )
        elif stage == "CREATIVE_REVIEW":
            stage_instruction = (
                "Review the generated visual preview against the selected creative plan and video model constraints. "
                "This is a creative-control gate, not a strict compliance audit. Do not fail merely because you cannot inspect every pixel, "
                "cannot confirm an absence, or recommend later QA. If the canvas is broadly usable for producing controlled reference images, "
                "set approved_for_split true, choose the exact reference_image_count needed for video control, and set next_stage to FINAL_ASSETS. "
                "The visual preview is an ordered sequence of one or more multi-panel boards that will be split into separate references next. "
                "Inspect every attached board in order and verify that their panels continue the creative reference_plan without omissions or duplicates. "
                "Do not reject the boards merely because they are grids, storyboards, or contain multiple sequential panels. "
                "Only set approved_for_split false for a blocking defect inside an individual panel, such as nested picture-in-picture, "
                "wrong product, obvious packaging deformation, broken character continuity, or readable unauthorized promotion text. "
                "Keep the result compact: creative_review, approved_for_split, reference_image_count, repair_brief. "
            )
        elif stage == "VIDEO_PROMPTS":
            marketing = dict(packet.get("marketing_authorization") or {})
            learned = dict(packet.get("learned_constraints") or {})
            quality_contract = dict(learned.get("quality_contract") or {})
            promo = "；".join(dict.fromkeys(
                part for part in (
                    str(marketing.get("confirmed_promotions") or "").strip(),
                    str(marketing.get("promotion_cta") or "").strip(),
                )
                if part
            ))
            language = str(packet.get("video_language") or "en-US")
            language_label = str(packet.get("video_language_label") or "English (US)")
            resolution = str(packet.get("video_resolution") or "720p")
            project_requirement = _humanize_project_text(
                packet.get("project_requirements") or packet.get("user_instruction") or packet.get("brief") or ""
            )
            requirement_instruction = (
                "Treat the user-approved project requirement as source constraints, not final prompt copy. Extract its intent into structured prompt controls: "
                "pacing, conflict_intensity, speech_speed, camera_style, CTA, forbidden_visuals, product_visibility, and segment_goal. "
                "Apply those controls to every complete video, every segment prompt, the action rhythm, camera movement, dialogue speed, CTA, and edit handoff. Source requirement: "
                + project_requirement
                + ". Do not paste this source requirement verbatim into segment prompts. Do not dilute it or apply it only to the creative plan. "
                if project_requirement
                else ""
            )
            fast_instruction = (
                "Because the project asks for fast rhythm, explosive impact, or fast speech, every segment must explicitly request fast-paced vertical short-form rhythm, rapid cuts, quick gestures, high-energy delivery, and fast but intelligible spoken cadence. "
                if any(marker in project_requirement.lower() for marker in ("快节奏", "语速快", "快速", "爆发", "激烈", "fast", "rapid", "quick", "high-energy", "explosive"))
                else ""
            )
            promo_instruction = (
                f"Use this confirmed CTA as natural on-camera speech in the video prompts and edit handoff: {promo}. "
                "This exact price/offer was explicitly approved by the project owner: do not reject, omit, soften, or classify it as an unconfirmed promotion. "
                "Do not invent other discounts. Do not show a landing page, checkout page, shopping UI, QR code, hard-sell banner, or TV-shopping style sales pitch. "
                if bool(marketing.get("allow_promotional_cta")) and promo
                else "Use only non-promotional CTAs because no confirmed promotion is authorized. "
            )
            segment_durations = list(packet.get("video_segment_durations_seconds") or [10])
            total_duration = int(sum(int(value) for value in segment_durations) or packet.get("recommended_video_duration_seconds") or 10)
            reference_limit = int(packet.get("video_reference_limit") or 5)
            stage_instruction = (
                f"Return result.videos as a JSON array containing exactly {int(packet.get('video_count') or 10)} complete videos, not loose clips. "
                f"Each complete video should be about {total_duration} seconds within the selected duration range and must contain exactly "
                f"{int(packet.get('video_segment_count') or len(segment_durations))} ordered segments with durations {segment_durations}. "
                "Every video object must include version_name, story_arc, aspect_ratio, and segments. "
                "Every complete video must use a full conversion structure: opening retention hook, middle visual selling-point proof, and final promotional CTA conversion. "
                "For two-segment videos, segment 1 is the hook and segment 2 must contain both selling-point proof and final CTA; never leave the second half as only a mood shot or simple product hero. "
                "The product half must communicate only project-confirmed value points through the authoritative creative dialogue and scene-appropriate visual proof. Do not invent ingredients, usage, dosage, consumption, or a generic product demonstration. "
                "CTA must be spoken naturally by the person in-scene; overlays may only be small subtitles that mirror the spoken line. Never use shopping-button wording, landing-page display, checkout UI, fake shopping page, QR code, countdown, TV-shopping layout, or presenter-style hard selling. "
                "Every segment must include prompt, integer duration, segment_index, reference_indices, continuity_note, segment_goal, timeline, pacing, camera_direction, dialogue_lines, and negative_prompt. "
                "Keep reference_indices as a separate control field. Never write reference-image bindings, @image aliases, product authority rules, the full-video story arc, or another segment's events inside segment.prompt. "
                "timeline must be an ordered list of this segment's local time beats, each with start_second, end_second, action, camera, and optional dialogue_key. "
                "segment.prompt must contain only a concise description of this segment's rhythm, local action, camera behavior, and performance intent; the server will bind reference images separately. "
                f"reference_indices must point to the attached final reference images by visual order; use no more than {reference_limit} references per segment. "
                "Do not give every segment the same reference_indices. Select only the references that the segment actually needs. "
                "Include a product/package reference only when that segment visibly shows the product; otherwise avoid product-package references because they can contaminate people-only or scene-only clips. "
                "For multi-segment videos, reuse one shared continuity reference across the ordered segments, then vary the action/product references per segment. "
                "For a 20-second video with five references, prefer continuity patterns like segment 1 using [1,2,4] and segment 2 using [1,3,5]. "
                "The segments inside one video must form one continuous story with the same person, room, product, wardrobe, lighting, and camera logic. "
                "Across the requested videos, vary hooks, scenes, actions, camera movement, body language, and speaking lines so they are not homogeneous. "
                "Do not treat video_count as segment count; it is the number of complete final videos. "
                "The visual source is approved automatically for split. Do not require a male anchor unless an approved reference contains one. "
                "Use the attached product/package authority image only for package identity and label consistency. "
                + _product_visual_lock(packet)
                + "Product handling must follow the project creative and uploaded authority image. Do not invent opening, loose contents, use, consumption, dosage, or serving instructions. "
                f"Strictly use video language {language_label} ({language}) for all spoken lines, captions, subtitles, CTA, and narration. Do not mix languages. "
                f"Write prompts for final output resolution {resolution}; do not request any other resolution. "
                + requirement_instruction
                + fast_instruction
                + promo_instruction
                + "Resolve ordinary creative ambiguity in favor of the user-approved project requirement while preserving product identity and provider safety constraints. "
                + "Use a strict segment-only format: one short action paragraph in segment.prompt, structured local beats in timeline, exact spoken copy in dialogue_lines, and constraints in negative_prompt. "
                "Never copy the project goal verbatim into prompt strings; rewrite it as concise video-model instructions. "
                "Return strictly valid JSON. Inside every prompt string, write spoken dialogue with single quotes; never place unescaped double quotes inside a JSON string. "
                "LEARNED QUALITY CONTRACT: reference images and text prompts have separate jobs. Reference images lock character, scene, current action, product package, or the preceding continuity frame. "
                "segment.prompt must not restate reference bindings or whole-video background; it contains only this segment's pacing, local timeline/action, camera behavior, performance, and exact spoken lines. "
                "Only an original uploaded product visual is package authority. AI-generated panels showing the product remain action/composition references and never override label or package geometry. "
                "Put the confirmed CTA in the final segment exactly once and never serialize fields such as duration, characters, reference_indices, schema_version, or continuity_note into dialogue. "
                + (
                    "These rules are active because the product/model experience ledger has confirmed them. "
                    if quality_contract
                    else ""
                )
            )
        elif stage == "EDIT_PACKAGE":
            marketing = dict(packet.get("marketing_authorization") or {})
            promo = "；".join(dict.fromkeys(
                part for part in (
                    str(marketing.get("confirmed_promotions") or "").strip(),
                    str(marketing.get("promotion_cta") or "").strip(),
                )
                if part
            ))
            language = str(packet.get("video_language") or "en-US")
            language_label = str(packet.get("video_language_label") or "English (US)")
            resolution = str(packet.get("video_resolution") or "720p")
            stage_instruction = (
                "You are the creative director. Create concise editor-facing publishing guidance, not an ad-buying or campaign optimization plan. "
                "This handoff is for a human video editor who needs timeline overlay instructions and final posting copy. "
                f"Use only {language_label} ({language}) for all on-screen text, publish title, caption, CTA, and hashtags; do not mix languages. "
                f"Mention final output resolution {resolution} only as an editor delivery note. "
                "Return natural, readable language inside result.edit_guidance. "
                "For each complete video or version, include time ranges such as 0-2s, 2-6s, 6s-end; specify overlay wording, font style, position, and whether it should appear as title, small caption, or CTA. "
                "Keep overlays short, culturally natural for the target country, and suitable for local TikTok/Reels style. CTA must be spoken by the character; overlay text may only be an optional small subtitle, not a shopping button or hard-sell card. "
                "Do not include media buying, audience targeting, budget, CTR, conversion testing, A/B testing, bidding, pixel, ad-set, or ROAS advice. "
                + (
                    f"Use this confirmed CTA as the spoken final line and in caption where natural: {promo}. Do not turn it into shopping-button wording, a landing page, checkout UI, QR code, or TV-shopping sales card. "
                    f"Translate its wording into {language_label} while preserving the exact offer amount and conditions. "
                    if bool(marketing.get("allow_promotional_cta")) and promo
                    else "Use only non-promotional CTAs because no confirmed promotion is authorized. "
                )
                + "Do not invent any other discount, countdown, or scarcity language. "
                "Required result fields: edit_guidance, publish_title, publish_caption, hashtags. "
            )
        readable_requirement = _humanize_project_text(
            packet.get("project_requirements") or packet.get("user_instruction") or packet.get("brief") or ""
        )
        prompt = (
            role_prompt + "Execute exactly this content-factory stage. Use the attached approved assets. "
            + stage_instruction
            + "Do not publish, purchase, log in, use unconfirmed promotions, or advance another stage. "
            "End with one complete JSON object and no substitute summary. The JSON must contain "
            "schema_version, execution_id, project_id, stage, status, result, evidence, issues, repair_brief, and next_stage. "
            f"execution_id must be exactly {json.dumps(str(packet.get('execution_id') or ''), ensure_ascii=False)}. "
            f"result must contain these fields: {', '.join(packet['required_result_fields'])}. "
            f"next_stage must be {packet['required_next_stage']}.\n\n"
            + ("USER-APPROVED PROJECT REQUIREMENT (plain text):\n" + readable_requirement + "\n\n" if readable_requirement else "")
            + "STAGE PACKET (structured controls):\n"
            + json.dumps(_packet_for_prompt(packet, stage), ensure_ascii=False, separators=(",", ":"))
        )
    if stage == "VISUAL_PREVIEW" and len(visual_boards) > 1:
        return _execute_visual_boards_sequentially(
            packet,
            output_dir,
            stage,
            files,
            prompt,
            visual_boards,
        )

    errors: list[str] = []
    previous_error = ""
    # One broker delivery may submit at most once. Recovery scans this
    # project's own slot for a late response before the project-level retry
    # scheduler is allowed to submit again. Keeping retries outside this loop
    # prevents duplicate conversations and keeps the Celery execution lease
    # longer than every internal browser wait.
    max_attempts = 1
    last_stage_error: ChatGPTStageError | None = None
    for attempt in range(1, max_attempts + 1):
        attempt_start_images: set[str] = set()
        try:
            if attempt > 1 and stage not in VISUAL_STAGES:
                recovered = _existing_stage_response(packet)
                if recovered is not None:
                    text, chat_url = recovered
                    _clear_composer_best_effort()
                    return text, chat_url, []
            stage_url = (
                "https://chatgpt.com/?temporary-chat=false"
                if stage in NORMAL_CHAT_STAGES
                else DEFAULT_CHATGPT_URL
            )
            attempt_url = stage_url
            if attempt == 1:
                _run("open", attempt_url, timeout=_navigation_timeout())
            else:
                _run("reload", timeout=_navigation_timeout())
                time.sleep(3)
                _run("open", attempt_url, timeout=_navigation_timeout())
            if stage in NORMAL_CHAT_STAGES:
                _ensure_normal_chat_for_visual_stage()
            try:
                _enter_chat_composer(stage_url)
                _dismiss_nonblocking_chatgpt_overlays()
                _wait_selector("#upload-files", timeout_seconds=30)
            except TimeoutError:
                _run("open", stage_url, timeout=_navigation_timeout())
                _enter_chat_composer(stage_url)
                _wait_selector("#upload-files")
            # Temporary Chat cannot invoke image generation on some accounts
            # and can abort long structured CREATIVE responses after only a few
            # JSON characters. Those stages use an exact normal conversation
            # URL which the server deletes after persisting the result.
            if stage not in NORMAL_CHAT_STAGES:
                _ensure_temporary_chat_best_effort()

            retry_instruction = ""
            if attempt > 1:
                if stage in VISUAL_STAGES:
                    visual_retry_reason = (
                        "The previous assistant analyzed the uploaded image or returned ordinary text instead of generating image media. "
                        "Do not describe, critique, summarize, or ask questions. Use ChatGPT image generation and return visible generated image media only. "
                        if "text without a generated image" in previous_error.lower()
                        else ""
                    )
                    retry_instruction = (
                        (
                            "The previous assistant refused because it applied stale visual guidelines. Reinterpret only any truly prohibited detail, "
                            "but preserve the approved project's full emotional intensity, explosive conflict, fictional adult characters, hook, and pacing. "
                            if "stale visual guidelines" in previous_error.lower()
                            else "The previous image-generation attempt failed or timed out. This is a complete retry. "
                        )
                        + visual_retry_reason
                        + "Call the native ChatGPT image-generation tool now, not Python or code interpreter. "
                        "Display the image visibly inline in chat and return only the actual generated image media. "
                        "Do not return text or JSON.\n\n"
                    )
                else:
                    retry_instruction = "The previous attempt failed or timed out. Execute the complete stage again.\n\n"
            _raise_if_rate_limited(_page_state())
            _dismiss_nonblocking_chatgpt_overlays()
            _clear_composer_strict()
            prompt_text = retry_instruction + prompt
            _fill_prompt_text(prompt_text, timeout=180)
            for file_index, path in enumerate(files, 1):
                _upload_file(path, expected_count=file_index)
            if _composer_text_length() < min(80, max(10, len(prompt_text) // 20)):
                _fill_prompt_text(prompt_text, timeout=180)
            _wake_composer_for_send(prompt_text)
            _wait_until_sendable(expected_attachments=len(files), prompt_text=prompt_text)
            before = _page_state()
            attempt_start_images = set(str(value) for value in (before.get("generatedImages") or []))
            _pace_before_send(len(prompt_text), len(files), packet=packet)
            _send_prompt(prompt_text)
            state = _wait_for_answer(
                int(before.get("count") or 0),
                set(str(value) for value in (before.get("generatedImages") or [])),
                timeout_seconds=300 if stage in VISUAL_STAGES else 600,
                minimum_images=(expected_visuals if stage in VISUAL_STAGES else 0),
                packet=packet,
            )
            if stage in VISUAL_STAGES:
                generated_images = [
                    str(value)
                    for value in (state.get("generatedImages") or [])
                    if str(value).strip() and str(value) not in attempt_start_images
                ]
                if len(generated_images) >= expected_visuals and len(state.get("newImages") or []) < expected_visuals:
                    state["newImages"] = generated_images[-expected_visuals:]
            saved = (
                _persist_visuals(output_dir, stage, list(state.get("newImages") or []), expected_visuals)
                if stage in VISUAL_STAGES
                else []
            )
            if stage in VISUAL_STAGES and len(saved) != expected_visuals:
                raise RuntimeError(
                    f"ChatGPT completed with {len(saved)} persistable visuals; expected {expected_visuals}"
                )
            _record_chatgpt_success()
            return _preferred_response_text(state), str(state.get("url") or "") or None, saved
        except SoftTimeLimitExceeded:
            # Do not spend the final hard-limit grace period probing tabs or
            # preparing another composer. The outer task persists a retry that
            # first attempts to collect the existing assistant response.
            raise
        except Exception as exc:
            # The agent-browser response pipe can close after ChatGPT has
            # already rendered a terminal quota response. Re-read the live
            # page before classifying the transport exception; otherwise the
            # stage is resubmitted every few seconds despite a multi-hour
            # account cooldown being visible in the completed assistant turn.
            capacity_error: ChatGPTStageError | None = None
            try:
                _raise_if_live_capacity_limited()
            except ChatGPTStageError as detected:
                capacity_error = detected
            except Exception:
                pass
            if capacity_error is not None:
                previous_error = str(capacity_error)
                last_stage_error = capacity_error
                errors.append(f"attempt {attempt}: {capacity_error}")
                break
            previous_error = str(exc)
            if isinstance(exc, ChatGPTStageError):
                last_stage_error = exc
            recovered_visuals = (
                _recover_visible_visuals(packet, output_dir, stage, exclude_signatures=attempt_start_images)
                if stage in VISUAL_STAGES
                else None
            )
            if recovered_visuals is not None:
                _clear_composer_best_effort()
                return recovered_visuals
            recovered = _existing_stage_response(packet)
            if recovered is not None:
                text, chat_url = recovered
                _clear_composer_best_effort()
                return text, chat_url, []
            if attempt < max_attempts and stage not in VISUAL_STAGES:
                recovered = _wait_for_late_stage_response(
                    packet,
                    timeout_seconds=int(packet.get("late_response_probe_seconds") or 90),
                )
                if recovered is not None:
                    text, chat_url = recovered
                    _clear_composer_best_effort()
                    return text, chat_url, []
            errors.append(f"attempt {attempt}: {exc}")
            if _rate_limit_marker(str(exc)) or "CHATGPT_RATE_LIMIT_COOLDOWN_ACTIVE" in str(exc):
                break
            if attempt < max_attempts:
                continue
    if last_stage_error is not None:
        raise ChatGPTStageError(
            "; ".join(errors) or str(last_stage_error),
            raw_text=last_stage_error.raw_text,
            chat_url=last_stage_error.chat_url,
        )
    raise RuntimeError("; ".join(errors))


def execute_chatgpt_stage(packet: dict[str, Any]) -> tuple[str, str | None, list[str]]:
    """Execute one browser stage and always retire its local CDP daemon.

    The daemon is only a Linux-side CDP client.  Closing it does not close the
    authoritative Windows Chrome slot; a later browser stage recreates the
    client against the same user-owned CDP route.
    """
    session = _session_name()
    stage_lease = _acquire_browser_stage_lease(session)
    try:
        return _execute_chatgpt_stage(packet)
    finally:
        try:
            if str(os.getenv("HERMES_AGENT_BROWSER_CLOSE_AFTER_STAGE", "true")).strip().lower() not in {
                "0", "false", "no", "off",
            }:
                close_agent_browser_session_best_effort()
        finally:
            _release_browser_lock(stage_lease)
