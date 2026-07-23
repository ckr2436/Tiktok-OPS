from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SERVICE_SOURCE = (
    BACKEND_ROOT / "app/services/hermes_agent/content_factory.py"
).read_text(encoding="utf-8")
TASK_SOURCE = (
    BACKEND_ROOT / "app/tasks/hermes_agent/content_factory_tasks.py"
).read_text(encoding="utf-8")
ROUTER_SOURCE = (
    BACKEND_ROOT / "app/features/tenants/hermes_agent/router.py"
).read_text(encoding="utf-8")


def test_queue_stage_persists_waiting_bridge_state():
    assert 'wait_state["browser_slot_wait_error_code"] = str(exc.code)' in SERVICE_SOURCE
    assert 'project.status = "waiting_bridge"' in SERVICE_SOURCE
    assert '"CONTENT_BROWSER_CAPACITY_FULL"' in SERVICE_SOURCE


def test_retired_slot_indices_are_reusable():
    used_slot_section = SERVICE_SOURCE[
        SERVICE_SOURCE.index("def _agent_used_slot_indices(") :
        SERVICE_SOURCE.index("def _agent_target_slot_count(")
    ]
    assert 'if str(row.status or "").lower() == "retired":' in used_slot_section
    assert "continue" in used_slot_section


def test_self_heal_consumes_durable_draft_slot_requests():
    assert 'status.in_(("draft", "queued", "running"' in TASK_SOURCE
    assert 'if project.status == "draft":' in TASK_SOURCE
    assert 'request_state.get("browser_slot_requested_at")' in TASK_SOURCE
    assert 'wait_state["browser_slot_requested_at"] = _stage_now().isoformat()' in TASK_SOURCE
    assert 'stats["waiting_slot_started"] += 1' in TASK_SOURCE


def test_create_project_returns_while_local_agent_builds_slot():
    create_section = ROUTER_SOURCE[
        ROUTER_SOURCE.index("def create_content_factory_project(") :
        ROUTER_SOURCE.index("def update_content_factory_project(")
    ]
    assert "except APIError as exc:" in create_section
    assert '"CONTENT_BROWSER_BRIDGE_REQUIRED"' in create_section
    assert "db.expire_all()" in create_section
