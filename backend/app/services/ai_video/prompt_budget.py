from __future__ import annotations

import re


_STRUCTURED_PREFIXES = (
    "Visual style (signed whole-video contract):",
    "Reference bindings:",
    "Timeline (this segment only):",
    "Motion and effects:",
    "Dialogue:",
    "Voice lock for this segment:",
    "Voice:",
    "Audio:",
    "Continuity:",
    "Product presentation policy:",
    "Segment scope:",
    # Small-budget providers receive the same compiler packet after its first
    # semantic compaction.  These are not free-form user prose: ``Refs`` keeps
    # the ordered multimodal bindings, ``Beats`` keeps the segment-local motion
    # lane, and ``Repair`` is a bounded pixel-QA correction for that lane.
    "Refs:",
    "Beats:",
    "Repair:",
    "Must:",
    "Product:",
    "Direction:",
)


_TIMED_ROW_RE = re.compile(
    r"^(?P<label>\d+(?:\.\d+)?-\d+(?:\.\d+)?s):\s*(?P<body>.*)$",
    flags=re.IGNORECASE,
)
_READABLE_VALUE_RE = re.compile(
    r"\b(?P<object>(?:physical\s+)?(?:analog\s+)?clock|"
    r"(?:mechanical\s+)?(?:tally\s+)?counter|tally|timer)\b"
    r"[^.;|]{0,72}?\b(?:reads?|shows?|displays?|indicates?)\s+"
    r"(?P<value>\d{1,4}(?::\d{2})?(?:\s*(?:a\.?m\.?|p\.?m\.?))?)",
    flags=re.IGNORECASE,
)
_EXACT_QUANTITY_RE = re.compile(
    r"\bexactly\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"[A-Za-z][A-Za-z-]*",
    flags=re.IGNORECASE,
)
_EXACT_QUANTITY_ACTION_RE = re.compile(
    r"\b(?:(?:she|he|they|the\s+(?:woman|man|character|protagonist))\s+)?"
    r"(?:takes?|presents?|holds?|places?|shows?|removes?|dispenses?)\s+"
    r"exactly\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"[A-Za-z][A-Za-z-]*",
    flags=re.IGNORECASE,
)
_READABLE_PRODUCT_MARKING_RE = re.compile(
    r"\b(?:front\s+)?(?P<value>[A-Za-z][A-Za-z0-9-]{2,40})\s+"
    r"(?:marking|badge|label\s+text)\b",
    flags=re.IGNORECASE,
)
_PHONE_STATE_RE = re.compile(
    r"\bphone(?:\s+remains?|\s+remaining|\s+stays?|\s+is|\s+must\s+be)?\s+"
    r"(?:visibly\s+)?(?:face[- ]down|face[- ]up|screen[- ]up|screen[- ]down)\b",
    flags=re.IGNORECASE,
)
_OBJECT_STATE_RE = re.compile(
    r"\b(?P<object>dish|tray|hands?|screen|door|lid|cap)\s+"
    r"(?P<state>empty|full|open|closed|locked|unlocked)\b",
    flags=re.IGNORECASE,
)
_DOWNWARD_ACTION_RE = re.compile(
    r"\b(?:one\s+)?(?:hand\s+)?(?:held\s+in\s+)?(?:a\s+)?(?:simple\s+)?"
    r"downward[- ]pointing\s+(?:hand\s+)?pose\b|"
    r"\b(?:hand|finger)\b[^.;|]{0,48}\bpoint(?:s|ing)?\s+downward\b",
    flags=re.IGNORECASE,
)
_VISUAL_EFFECT_RE = re.compile(
    r"\b(?P<effect>(?:(?:fading|descending|rising|contracting|expanding|"
    r"luminous|glowing|warm|cool|blue|purple|amber)\s+){0,2}"
    r"(?:pulse|glow|portal|shards?|flash|sparkles?|particles?))\b",
    flags=re.IGNORECASE,
)
_NEGATIVE_PRODUCT_RE = re.compile(
    r"\b(?:no\s+(?:product|package|packaging|bottle|jar|tube|box|carton|container)|"
    r"(?:product|package|packaging|bottle|jar|tube|box|carton|container)\s+(?:remains?\s+)?"
    r"(?:out\s+of\s+frame|hidden|not\s+visible))\b",
    flags=re.IGNORECASE,
)
_PRODUCT_PACKAGE_VISIBLE_RE = re.compile(
    r"\b(?:show(?:s|ing|ed)?|reveal(?:s|ing|ed)?|display(?:s|ing|ed)?|"
    r"feature(?:s|ing|d)?|present(?:s|ing|ed)?|hold(?:s|ing)?|center(?:ed)?|"
    r"appear(?:s|ing)?)\b[^.;|]{0,72}\b(?:product\s+)?"
    r"(?:package|packaging|bottle|jar|tube|box|carton|container)\b|"
    r"\b(?:product\s+)?(?:package|packaging|bottle|jar|tube|box|carton|container)"
    r"\b[^.;|]{0,72}\b(?:visible|shown|"
    r"centered|alone|facing\s+camera)\b",
    flags=re.IGNORECASE,
)
_DANGLING_ACTION_TAIL_RE = re.compile(
    r"\b(?:a|an|the|at|to|toward|towards|into|from|with|while|despite|"
    r"nearly|near|over|under|and|or|as|its|her|his|their)$",
    flags=re.IGNORECASE,
)
_CJK_CHARACTER_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _normalized_semantic(value: str) -> str:
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(value or "").casefold().replace("a.m.", "am").replace("p.m.", "pm"),
    ).strip()


def _short_object_name(value: str) -> str:
    lowered = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if "clock" in lowered:
        return "clock"
    if "tally" in lowered:
        return "tally"
    if "counter" in lowered:
        return "counter"
    return "timer"


def structured_video_prompt_semantic_invariants(value: str) -> list[str]:
    """Extract provider-visible facts that transport compaction may not lose.

    Hermes owns the creative prose, while this function protects only exact
    observable states: timed readable values, exact quantities, phone
    orientation and time-scoped product absence.  It deliberately does not
    invent creative actions or product claims.
    """

    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    explicit = next((line for line in lines if line.startswith("Must:")), "")
    if explicit:
        expanded: list[str] = []
        for group in explicit.partition(":")[2].split(";"):
            group = group.strip(" .")
            if not group:
                continue
            timed = re.match(
                r"^(?P<label>\d+(?:\.\d+)?-\d+(?:\.\d+)?s)\s+"
                r"(?P<body>.+)$",
                group,
                flags=re.IGNORECASE,
            )
            label = timed.group("label") if timed else ""
            body = timed.group("body") if timed else group
            parts = [part.strip(" .") for part in body.split(",")]
            expanded.extend(
                f"{label} {part}".strip()
                for part in parts
                if part
            )
        return list(dict.fromkeys(expanded))

    timeline = next((
        line
        for line in lines
        if line.startswith(("Timeline (this segment only):", "Beats:"))
    ), "")
    if not timeline:
        return []
    body = timeline.partition(":")[2].strip()
    invariants: list[str] = []
    for raw_row in [row.strip() for row in body.split(" | ") if row.strip()]:
        match = _TIMED_ROW_RE.match(raw_row)
        label = match.group("label") if match else ""
        row = match.group("body") if match else raw_row
        prefix = f"{label} " if label else ""
        for readable in _READABLE_VALUE_RE.finditer(row):
            invariants.append(
                f"{prefix}{_short_object_name(readable.group('object'))} "
                f"reads {readable.group('value').strip()}"
            )
        action_quantities = list(_EXACT_QUANTITY_ACTION_RE.finditer(row))
        if action_quantities:
            invariants.extend(
                prefix + quantity.group(0).strip()
                for quantity in action_quantities
            )
        else:
            for quantity in _EXACT_QUANTITY_RE.finditer(row):
                invariants.append(prefix + quantity.group(0).strip())
        for marking in _READABLE_PRODUCT_MARKING_RE.finditer(row):
            invariants.append(
                prefix + marking.group("value").strip() + " marking visible"
            )
        for state in _PHONE_STATE_RE.finditer(row):
            canonical = re.sub(r"\s+", " ", state.group(0).strip())
            canonical = re.sub(
                r"phone(?:\s+remains?|\s+remaining|\s+stays?|\s+is|\s+must\s+be)?\s+"
                r"(?:visibly\s+)?",
                "phone ",
                canonical,
                flags=re.IGNORECASE,
            )
            invariants.append(prefix + canonical)
        for state in _OBJECT_STATE_RE.finditer(row):
            invariants.append(
                prefix
                + state.group("object").strip()
                + " "
                + state.group("state").strip()
            )
        if _DOWNWARD_ACTION_RE.search(row):
            invariants.append(prefix + "downward-pointing hand pose")
        effect_tail = row.rsplit(";", 1)[-1] if ";" in row else ""
        for effect in _VISUAL_EFFECT_RE.finditer(effect_tail):
            invariants.append(prefix + effect.group("effect").strip())
        if _NEGATIVE_PRODUCT_RE.search(row):
            invariants.append(prefix + "no product visible")
        elif _PRODUCT_PACKAGE_VISIBLE_RE.search(row):
            # The package geometry belongs to the authoritative product
            # reference.  A generic transport invariant keeps the visible
            # product beat without rewriting a balm jar into a gummy bottle.
            invariants.append(prefix + "product package visible")
    return list(dict.fromkeys(invariants))


def validate_structured_video_prompt_fidelity(
    source: str,
    actual: str,
    *,
    required_reference_aliases: tuple[str, ...] = (),
    product_required: bool = False,
) -> dict[str, object]:
    """Fail closed when the actual provider packet lost signed semantics."""

    invariants = structured_video_prompt_semantic_invariants(source)
    normalized_actual = _normalized_semantic(actual)
    actual_invariants = {
        _normalized_semantic(item)
        for item in structured_video_prompt_semantic_invariants(actual)
    }
    missing_semantics = [
        item
        for item in invariants
        if (
            _normalized_semantic(item) not in normalized_actual
            and _normalized_semantic(item) not in actual_invariants
        )
    ]
    aliases = list(dict.fromkeys(
        [*required_reference_aliases, *re.findall(r"@image\d+", source)]
    ))
    missing_aliases = [alias for alias in aliases if alias not in actual]
    source_timeline = next((
        line
        for line in str(source or "").splitlines()
        if line.strip().startswith(("Timeline (this segment only):", "Beats:"))
    ), "")
    actual_timeline = next((
        line
        for line in str(actual or "").splitlines()
        if line.strip().startswith(("Timeline (this segment only):", "Beats:"))
    ), "")
    source_direction = next((
        line.partition(":")[2].strip()
        for line in str(source or "").splitlines()
        if line.strip().startswith("Direction:")
    ), "")
    actual_direction = next((
        line.partition(":")[2].strip()
        for line in str(actual or "").splitlines()
        if line.strip().startswith("Direction:")
    ), "")

    def timed_actions(line: str) -> dict[str, str]:
        rows: dict[str, str] = {}
        body = str(line or "").partition(":")[2]
        for raw_row in [value.strip() for value in body.split(" | ") if value.strip()]:
            match = _TIMED_ROW_RE.match(raw_row)
            if not match:
                continue
            action = match.group("body").split("; FX:", 1)[0].strip(" ,.;")
            rows[match.group("label").lower()] = action
        return rows

    source_actions = timed_actions(source_timeline)
    actual_actions = timed_actions(actual_timeline)
    missing_action_beats = [
        label
        for label in source_actions
        if not actual_actions.get(label)
    ]
    dangling_action_beats = [
        label
        for label, action in actual_actions.items()
        if _DANGLING_ACTION_TAIL_RE.search(action)
    ]
    errors: list[str] = []
    if missing_semantics:
        errors.append("missing semantics: " + ", ".join(missing_semantics))
    if missing_aliases:
        errors.append("missing references: " + ", ".join(missing_aliases))
    if missing_action_beats:
        errors.append(
            "missing timed actions: " + ", ".join(missing_action_beats)
        )
    if dangling_action_beats:
        errors.append(
            "incomplete timed actions: " + ", ".join(dangling_action_beats)
        )
    if source_direction and not actual_direction:
        errors.append("missing provider direction lane")
    if product_required and not any(
        marker in actual
        for marker in ("Product:", "Product presentation policy:")
    ):
        errors.append("missing authoritative product presentation lane")
    ellipsis_lines = [
        line
        for line in str(actual or "").splitlines()
        if "..." in line
        and not (
            line.strip().startswith("Dialogue:")
            and line.strip() in {
                source_line.strip()
                for source_line in str(source or "").splitlines()
                if source_line.strip().startswith("Dialogue:")
            }
        )
    ]
    if ellipsis_lines:
        ellipsis_lanes = [
            line.partition(":")[0].strip() or "unprefixed"
            for line in ellipsis_lines
        ]
        errors.append(
            "provider prompt contains truncation ellipsis in "
            + ", ".join(dict.fromkeys(ellipsis_lanes))
        )
    if errors:
        raise ValueError(
            "structured provider prompt is not semantically lossless: "
            + " | ".join(errors)
        )
    return {
        "semantic_invariants": invariants,
        "reference_aliases": aliases,
        "product_required": bool(product_required),
        "actual_characters": len(str(actual or "")),
        "validated": True,
    }


def is_structured_video_prompt(value: str) -> bool:
    """Return whether *value* is the segment compiler's line-based packet.

    Direct AI-video prompts can contain prose such as ``Dialogue: Woman1...``
    inside one long line.  Treating that prose as the compiler packet makes
    the low-budget compactor require missing line-level contract fields and
    reject an otherwise valid provider request.  A structured packet has
    several independently prefixed lines; an embedded word is not enough.
    """

    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    matched = {
        prefix
        for prefix in _STRUCTURED_PREFIXES
        if any(line.startswith(prefix) for line in lines)
    }
    # The small-budget provider view may already have replaced the verbose
    # Timeline line with the more useful Motion line.  It is still a compiler
    # packet and must receive the final transport-budget compaction (Doubao
    # reserves five of its 500 characters for ``生成视频：``).  Requiring an
    # explicit Dialogue line, or the compiler's explicit local-voiceover
    # Audio lane, plus three independent compiler prefixes keeps ordinary
    # one-line user prose out of this path.
    has_execution_timeline = bool(
        {
            "Timeline (this segment only):",
            "Motion and effects:",
            "Beats:",
        }
        & matched
    )
    return (
        has_execution_timeline
        and ("Dialogue:" in matched or "Audio:" in matched)
        and len(matched) >= 3
    )


def _compact_text(value: str, limit: int) -> str:
    """Fit transport prose without inventing a truncation marker.

    Structured provider prompts are executable instructions, not previews.
    Appending ``...`` used to turn a complete AI-authored clause into an
    explicitly incomplete action and was then (correctly) rejected by the
    semantic-fidelity guard.  Keep only complete words, or a bounded CJK
    phrase when the provider-facing instruction has no ASCII word breaks.
    """

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    kept: list[str] = []
    for word in text.split(" "):
        candidate = " ".join([*kept, word])
        if len(candidate) > limit:
            break
        kept.append(word)
    result = " ".join(kept).rstrip(" ,.;:-")
    # A word-boundary cut can still end on a preposition/article (for example
    # ``the phone snaps dark at``).  That is not a complete provider action
    # even though no word was split. Remove only dangling grammar tokens until
    # the retained clause is executable; never invent replacement prose.
    while result and _DANGLING_ACTION_TAIL_RE.search(result):
        result = result.rsplit(" ", 1)[0].rstrip(" ,.;:-")
    if result:
        return result
    if _CJK_CHARACTER_RE.search(text):
        return text[:limit].rstrip(" ，。；、,:;.-")
    return ""


def _compact_text_no_ellipsis(value: str, limit: int) -> str:
    """Fit at a word boundary without emitting a false truncation token."""
    return _compact_text(value, limit)


def _compact_prefixed_line(line: str, limit: int) -> str:
    prefix, separator, body = line.partition(":")
    if not separator:
        return _compact_text(line, limit)
    kept_prefix = prefix.strip() + ": "
    return kept_prefix + _compact_text(body, max(1, limit - len(kept_prefix)))


def _compact_repair_line(line: str, limit: int) -> str:
    """Keep the opening, middle actions and final cue of a QA repair.

    Head truncation retained only the opening composition and repeatedly lost
    later phone-down/final-motion instructions.  Share the small provider lane
    across every ordered clause instead.
    """

    value = str(line or "").strip()
    if not value:
        return ""
    body = value.partition(":")[2].strip() if ":" in value else value
    body = re.sub(r"^visible\s+final\s*:?[ ]*", "", body, flags=re.IGNORECASE)
    body = re.sub(
        r"^keep\s+(?:the\s+)?final\s+beat\s+on\s+(?:the\s+)?"
        r"(?:bedside\s+)?tray\s*:?[ ]*",
        "",
        body,
        flags=re.IGNORECASE,
    )
    body = re.sub(r"\bre-render\s+in\s+(?:the\s+)?signed\s+order\b", "signed order", body, flags=re.IGNORECASE)
    body = re.sub(r"\bbegin\s+with\s+(?:a\s+)?hand\s+above\b", "start hand over", body, flags=re.IGNORECASE)
    body = re.sub(r"\breveal\s+(?:the\s+)?centered\b", "reveal", body, flags=re.IGNORECASE)
    body = re.sub(r"\btake\s+exactly\s+two\s+gummies\b", "take two gummies", body, flags=re.IGNORECASE)
    body = re.sub(r"\bfinally\s+make\s+(?:a\s+)?distinct\b", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\bwith\s+(?:a\s+)?fading\s+amber\s+pulse\b", "fading amber pulse", body, flags=re.IGNORECASE)
    body = re.sub(
        r"\bhard\s+cut\s+from\s+two\s+gummies\s+present\s+and\s+"
        r"phone\s+screen-up\s+to\s+dish\s+empty\s+and\s+phone\s+face-down\b",
        "two gummies phone screen-up; dish empty phone face-down",
        body,
        flags=re.IGNORECASE,
    )
    body = re.sub(
        r"\b(?:then\s+)?set\s+(?:the\s+)?phone\s+face-down\b",
        "phone face-down",
        body,
        flags=re.IGNORECASE,
    )
    body = re.sub(
        r"\b(?:make\s+)?(?:a\s+)?(?:distinct\s+)?downward\s+hand\s+"
        r"gesture\s+(?:with\s+)?(?:a\s+)?(?:fading\s+)?amber\s+pulse\b",
        "gesture downward; amber pulse",
        body,
        flags=re.IGNORECASE,
    )
    body = re.sub(
        r"\bgesture\s+downward\s+to\s+(?:a\s+)?(?:descending\s+)?"
        r"amber\s+pulse\b",
        "gesture downward; amber pulse",
        body,
        flags=re.IGNORECASE,
    )
    # Long multimodal final-review prose often describes several preserved
    # qualities before naming the one observable defect. Clause-balanced
    # truncation could keep generic verbs such as "Regenerate" while dropping
    # the exact package/speech constraint that justified the paid retry. Build
    # a provider-sized semantic repair for the common evidence types; the full
    # reviewer packet remains immutable in task metadata.
    quoted = list(dict.fromkeys(
        value.strip()
        for value in re.findall(r'["“]([^"”]{1,96})["”]', body)
        if value.strip()
    ))
    lower_body = body.lower()
    critical: list[str] = []
    if (
        any(marker in lower_body for marker in ("package", "packaging", "label"))
        and any(marker in lower_body for marker in ("exact", "correct", "legible"))
    ):
        label_value = quoted[0] if quoted else ""
        critical.append(
            f'exact package label "{label_value}"'
            if label_value
            else "exact legible package label"
        )
    if any(
        marker in lower_body
        for marker in ("spoken", "audio", "dialogue", "pronunciation")
    ):
        critical.append("speak Dialogue exactly")
    if "small amount" in lower_body:
        critical.append("show only a visibly small amount")
    if "intact" in lower_body and "skin" in lower_body:
        critical.append("intact external skin only")
    # Character-continuity repairs often arrive as a long evidence paragraph:
    # face proportions, hair, age, wardrobe, room/set and lighting.  Equal
    # clause compaction gave every comma fragment a few words and produced a
    # non-executable packet such as ``maintain adult woman; face proportions``.
    # The ordered continuity reference already contains the exact pixels, so
    # preserve the observable relation to that anchor instead of attempting
    # to squeeze every prose adjective into the provider's 495-character lane.
    continuity_fields = {
        "face": any(marker in lower_body for marker in ("face", "facial")),
        "hair": "hair" in lower_body,
        "age": "age" in lower_body,
        "wardrobe": any(
            marker in lower_body
            for marker in ("wardrobe", "outfit", "clothing", "sleepwear", "camisole", "pajama")
        ),
        "room": any(
            marker in lower_body
            for marker in ("bedroom", "room", "setting", "set", "headboard", "location")
        ),
        "lighting": any(marker in lower_body for marker in ("lighting", "light")),
        "medium": any(marker in lower_body for marker in ("medium", "2d", "2.5d", "3d", "animation")),
    }
    continuity_request = (
        any(
            marker in lower_body
            for marker in ("preserve", "match", "maintain", "same", "continuity")
        )
        and any(
            marker in lower_body
            for marker in ("protagonist", "recurring", "identity", "cast", "woman", "man", "character")
        )
        and sum(bool(value) for value in continuity_fields.values()) >= 3
    )
    if continuity_request:
        named_fields = [name for name, present in continuity_fields.items() if present]
        critical.insert(
            0,
            "match @image1 exactly: " + ", ".join(named_fields),
        )
    # Final multimodal reviews sometimes preface an actual continuity repair
    # with prose such as "No repair is required" and then describe the one
    # boundary that must change.  Equal-width clause compaction turned that
    # packet into useless fragments ("No repair is; Preserve current; ...")
    # while dropping the body location that the provider needed to preserve.
    # Keep the observable transition, not the reviewer's conversational
    # preamble.  The full report remains available in task metadata.
    if (
        "shoulder" in lower_body
        and "segment 3" in lower_body
        and "segment 2" in lower_body
        and any(marker in lower_body for marker in ("leg", "body-location"))
    ):
        critical.append(
            "keep shoulder application continuous across segments"
        )
        critical.append("no leg change")
    elif (
        "shoulder" in lower_body
        and "application" in lower_body
        and "segment" in lower_body
        and (
            "continuous" in lower_body
            or "prior segment" in lower_body
            or "previous segment" in lower_body
        )
    ):
        critical.append("shoulder application continuous")
        if "leg" in lower_body:
            critical.append("no leg change")
    if any(
        marker in lower_body
        for marker in (
            "no added",
            "do not add",
            "do not add any",
            "without added",
        )
    ):
        critical.insert(0, "do not add any spoken claim")
    if critical:
        deduplicated = list(dict.fromkeys(critical))
        semantic = "Repair: " + "; ".join(deduplicated)
        if len(semantic) <= limit:
            return semantic
        # A single reviewer paragraph can contain both the defect that caused
        # the retry (for example an incorrect package label or spoken copy)
        # and a long list of already-anchored continuity qualities.  When the
        # semantic summary itself exceeds the provider lane, retain the
        # observable defect before reference-owned appearance details.  The
        # previous fallback re-tokenized the original prose and produced a
        # vague instruction such as ``Regenerate all segments`` while losing
        # the exact label and speech correction entirely.
        def repair_priority(item: str) -> tuple[int, int]:
            lowered = item.casefold()
            if lowered.startswith("exact package label"):
                return (0, len(item))
            if lowered == "speak dialogue exactly":
                return (1, len(item))
            if lowered.startswith("do not add any spoken claim"):
                return (2, len(item))
            if "small amount" in lowered or "intact external skin" in lowered:
                return (3, len(item))
            if "application continuous" in lowered or "no leg change" in lowered:
                return (4, len(item))
            if lowered.startswith("match @image1 exactly"):
                return (5, len(item))
            return (6, len(item))

        selected: list[str] = []
        for item in sorted(deduplicated, key=repair_priority):
            candidate = "Repair: " + "; ".join([*selected, item])
            if len(candidate) <= limit:
                selected.append(item)
        if selected:
            return "Repair: " + "; ".join(selected)
    clauses = [
        part.strip(" ,.;")
        for part in re.split(
            r"\s*;\s*|\.\s+|:\s*|,\s*(?=(?:then|and\s+then|finally)\b)|"
            r"\s+(?=(?:then|and\s+then|finally)\b)",
            body,
            flags=re.IGNORECASE,
        )
        if part.strip(" ,.;")
    ]
    if len(clauses) > 1:
        clauses = [
            clause
            for clause in clauses
            if clause.strip().lower() not in {"signed order", "re-render signed order"}
        ]
    if len(clauses) <= 1:
        prefix = "Repair: "
        return prefix + _compact_text_no_ellipsis(
            body,
            max(1, limit - len(prefix)),
        )
    prefix = "Repair: "
    filler_words = {
        "a", "an", "the", "with", "and", "then", "finally", "make",
        "distinct", "centered", "visibly", "clearly", "entire", "same",
    }
    semantic_clauses = []
    for clause in clauses:
        words = re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*", clause)
        kept = [word for word in words if word.lower() not in filler_words]
        semantic_clauses.append(" ".join(kept) or clause)
    semantic_result = prefix + "; ".join(semantic_clauses)
    if len(semantic_result) <= limit:
        return semantic_result
    separators = 2 * (len(clauses) - 1)
    available = max(len(clauses) * 12, limit - len(prefix) - separators)
    each = max(12, available // len(clauses))
    compacted = [
        _compact_text_no_ellipsis(clause, each)
        for clause in semantic_clauses
    ]
    return _compact_text_no_ellipsis(
        prefix + "; ".join(compacted),
        limit,
    )


def compact_repair_instruction(value: str, *, max_characters: int) -> str:
    """Return a balanced repair body without the transport field prefix."""

    line = _compact_repair_line(
        "Repair: " + str(value or "").strip(),
        max(16, int(max_characters)),
    )
    return line.partition(":")[2].strip() if line else ""


def _compact_timed_line(line: str, limit: int) -> str:
    """Compact every timed beat instead of keeping only the opening beat.

    The former head truncation kept the room description and silently dropped
    later motion, effects, product action, and transitions.  Each beat is an
    AI-authored multimodal decision, so retain every time label and give each
    beat an equal text budget.
    """

    prefix, separator, body = str(line or "").partition(":")
    if not separator:
        return _compact_text(line, limit)
    rows = [row.strip() for row in body.split(" | ") if row.strip()]
    if len(rows) <= 1:
        return _compact_prefixed_line(line, limit)
    kept_prefix = prefix.strip() + ": "
    punctuation_budget = 3 * (len(rows) - 1)
    available = max(
        len(rows) * 18,
        limit - len(kept_prefix) - punctuation_budget,
    )
    each = max(18, available // len(rows))
    compacted = [_compact_text(row, each) for row in rows]
    return kept_prefix + " | ".join(compacted)


def _compact_direction_line(line: str, limit: int) -> str:
    """Keep style, pace and camera as independent provider duties.

    The multimodal Director authors this lane.  Compaction distributes the
    small transport budget across its ordered clauses instead of retaining
    only a generic opening adjective and dropping shot rhythm or style.
    """

    value = str(line or "").strip()
    if not value:
        return ""
    body = value.partition(":")[2].strip() if ":" in value else value
    clauses = [
        clause.strip(" .;；")
        for clause in re.split(r"\s*\|\s*", body)
        if clause.strip(" .;；")
    ]
    if not clauses:
        return ""
    prefix = "Direction: "
    if len(prefix + " | ".join(clauses)) <= limit:
        return prefix + " | ".join(clauses)
    separators = 3 * max(0, len(clauses) - 1)
    available = max(18 * len(clauses), limit - len(prefix) - separators)
    each = max(18, available // len(clauses))
    compacted = [
        _compact_text_no_ellipsis(clause, each)
        for clause in clauses
    ]
    compacted = [clause for clause in compacted if clause]
    return _compact_text_no_ellipsis(
        prefix + " | ".join(compacted),
        limit,
    )


def _merge_timeline_and_motion_lines(
    timeline_line: str,
    motion_line: str,
) -> str:
    """Join the AI-authored state lane and motion lane by timestamp.

    Still references can anchor the scene described by ``Timeline``, but they
    cannot communicate the portal pull, impact, reaction or other temporal
    hook authored in ``Motion and effects``.  Selecting the first non-empty
    line silently discarded that dynamic lane at the final provider boundary.
    Merge matching rows so later budget allocation treats both as one signed
    chronological contract.
    """

    timeline = str(timeline_line or "").strip()
    motion = str(motion_line or "").strip()
    if not timeline:
        return motion
    if not motion:
        return timeline

    def rows(value: str) -> list[tuple[str, str]]:
        body = value.partition(":")[2].strip()
        parsed: list[tuple[str, str]] = []
        for raw in [item.strip() for item in body.split(" | ") if item.strip()]:
            match = _TIMED_ROW_RE.match(raw)
            if not match:
                match = re.match(
                    r"^(?P<label>\d+(?:\.\d+)?-\d+(?:\.\d+)?s)\s+"
                    r"(?P<body>.*)$",
                    raw,
                    flags=re.IGNORECASE,
                )
            if match:
                parsed.append((match.group("label"), match.group("body")))
            else:
                parsed.append(("", raw))
        return parsed

    timeline_rows = rows(timeline)
    motion_rows = rows(motion)
    if not timeline_rows or not motion_rows:
        return timeline
    # A legacy untimed camera note is not an alternate execution timeline.
    # Merging it by row position can replace the current multimodal opening
    # action with stale prose (for example an obsolete rack-focus note).
    # Only an explicitly timed motion lane is eligible to augment timed beats.
    if not any(label for label, _body in motion_rows):
        return timeline
    motion_by_label = {
        label.casefold(): body
        for label, body in motion_rows
        if label and body
    }
    merged: list[str] = []
    for index, (label, action) in enumerate(timeline_rows):
        effect = motion_by_label.get(label.casefold()) if label else None
        if (
            not effect
            and index < len(motion_rows)
            and motion_rows[index][0]
        ):
            effect = motion_rows[index][1]
        body = action
        if effect and _normalized_semantic(effect) not in _normalized_semantic(action):
            # A separate Motion lane describes what changes through time and
            # therefore carries the hook.  Put it in the primary action
            # budget; the still-friendly Timeline state becomes supporting
            # context.  The old order spent most of each tiny row on static
            # room description and kept only the first FX adjective.
            body = f"{effect}; FX: {action}"
        merged.append(f"{label}: {body}" if label else body)
    return "Timeline (this segment only): " + " | ".join(merged)


def _maximize_timeline_in_packet(
    packet: list[str],
    *,
    timeline: str,
    timeline_source: str,
    limit: int,
) -> tuple[list[str], str]:
    """Spend otherwise unused provider characters on complete motion clauses.

    The per-row compiler adds clauses atomically, so its output length changes
    in steps.  A one-shot slack calculation can land just below the next step
    and leave dozens of usable characters empty.  Search the tiny bounded UI
    budget and retain the richest whole-clause timeline that still fits.
    """

    if not timeline or timeline not in packet:
        return packet, timeline
    timeline_index = packet.index(timeline)
    best_packet = list(packet)
    best_timeline = timeline
    start_budget = max(len(timeline), 54)
    for candidate_budget in range(start_budget, max(start_budget, limit) + 1):
        candidate_timeline = _compact_local_visual_timeline(
            timeline_source,
            candidate_budget,
        )
        if len(candidate_timeline) <= len(best_timeline):
            continue
        candidate_packet = list(packet)
        candidate_packet[timeline_index] = candidate_timeline
        if len("\n".join(candidate_packet)) <= limit:
            best_packet = candidate_packet
            best_timeline = candidate_timeline
    return best_packet, best_timeline


def _lean_dialogue_line(line: str) -> str:
    """Remove repeated speaker metadata without changing spoken copy."""

    if not str(line or "").startswith("Dialogue:"):
        return str(line or "")
    body = str(line).split(":", 1)[1].strip()
    body = re.sub(
        r"(?:^|\|\s*)[A-Za-z][A-Za-z0-9_ -]{0,40}:\s*(?=['\"])",
        lambda match: " | " if match.group(0).lstrip().startswith("|") else "",
        body,
    )
    body = re.sub(r"\s*\|\s*", " | ", body).strip(" |")
    return "Dialogue: " + body


def _lean_reference_bindings(line: str) -> str:
    value = str(line or "").strip()
    if not value:
        return ""
    value = re.sub(
        r";\s*images lock appearance/state,\s*Motion controls animation\.?",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r";\s*refs lock appearance/state\.?",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return value


def _tiny_reference_bindings(line: str) -> str:
    """Keep every ordered image handle *and its semantic role*.

    Reference order alone is not enough for a multimodal video provider.  A
    generated action/scene anchor and an uploaded package authority have very
    different jobs, and collapsing the non-package inputs to ``story`` loses
    the contract that the prompt compiler just established.  The web composer
    budget is small, so normalize the role vocabulary, but never replace it
    with a generic bucket.
    """

    value = _lean_reference_bindings(line)
    short = re.sub(r"^Reference bindings:\s*", "Refs: ", value)
    if not re.search(r"@image\d+", short):
        return _compact_prefixed_line(value, 85)

    role_aliases = {
        "character_anchor": "character",
        "scene_anchor": "scene",
        "action_anchor": "action",
        "product_anchor": "package",
        "product": "package",
    }
    canonical_parts: list[str] = []
    body = re.sub(r"^(?:Reference bindings:|Refs:)\s*", "", value).strip(" .")
    for raw_part in body.split(";"):
        handles = list(dict.fromkeys(re.findall(r"@image\d+", raw_part)))
        if not handles:
            continue
        role_text = raw_part.partition("=")[2]
        roles: list[str] = []
        for token in re.findall(r"[A-Za-z_]+", role_text):
            normalized = role_aliases.get(token.casefold(), token.casefold())
            if normalized in {"character", "scene", "action", "package", "visual"}:
                roles.append(normalized)
        label = "+".join(dict.fromkeys(roles)) or "visual"
        canonical_parts.append(f"{','.join(handles)}={label}")
    if not canonical_parts:
        return _compact_prefixed_line(short, 85)
    return "Refs: " + "; ".join(canonical_parts)


def _compact_semantic_invariants(invariants: list[str]) -> str:
    """Group exact observables by time without changing their meaning."""

    groups: list[tuple[str, list[str]]] = []
    for invariant in invariants:
        match = re.match(
            r"^(?P<label>\d+(?:\.\d+)?-\d+(?:\.\d+)?s)\s+"
            r"(?P<body>.+)$",
            str(invariant or "").strip(),
            flags=re.IGNORECASE,
        )
        label = match.group("label") if match else ""
        body = match.group("body") if match else str(invariant or "").strip()
        existing = next((items for key, items in groups if key == label), None)
        if existing is None:
            existing = []
            groups.append((label, existing))
        if body and body not in existing:
            existing.append(body)
    return "; ".join(
        ((label + " ") if label else "") + ", ".join(items)
        for label, items in groups
        if items
    )


def _compact_local_visual_timeline(line: str, limit: int) -> str:
    """Preserve action, effect and camera from every AI-authored beat."""

    # Doubao's feasibility planner already writes concise Chinese provider
    # actions under one combined transport budget.  If that complete timeline
    # fits, pass it through byte-for-byte instead of running an English
    # stop-word/three-clause heuristic over it.  The latter is a creative
    # rewrite, not compaction, and previously dropped late clauses such as
    # "no product" and the final downward CTA gesture.
    raw_prefix, raw_separator, raw_body = str(line or "").partition(":")
    raw_rows = [row.strip() for row in raw_body.split(" | ") if row.strip()]
    if raw_separator and raw_rows and _CJK_CHARACTER_RE.search(raw_body):
        complete_cjk_timeline = "Beats: " + " | ".join(raw_rows)
        if len(complete_cjk_timeline) <= limit:
            return complete_cjk_timeline

    def semantic_clauses(value: str, budget: int) -> str:
        text = re.sub(
            r"\s+",
            " ",
            str(value or "").replace("’", "'").replace("‘", "'"),
        ).strip()
        # This is transport compression, not a creative rewrite.  Convert
        # verbose, model-authored staging prose into the same observable state
        # tokens before the provider's small prompt budget is distributed.
        # Still-image references own appearance; these tokens tell Seedance
        # what must change over time (object count, phone state and gestures).
        canonical_states = (
            (
                r"\b(?:the\s+)?(?P<brand>[A-Za-z][A-Za-z0-9-]{2,30})\s+"
                r"(?P<color>(?:blue|purple|white|dark|light)\s+)?bottle\s+with\b"
                r"[^.;|]{0,120}\benters\b",
                r"\g<brand> \g<color>bottle enters",
            ),
            (
                r"\bkeeps?\s+scrolling\s+despite\s+(?:a\s+)?nearly\s+"
                r"empty\s+red\s+battery(?:\s+shape)?\b",
                "scrolls on low battery",
            ),
            (
                r"\bfreezes?\s+mid-scroll,?\s*lowers?\s+(?:the\s+)?phone"
                r"(?:\s+slightly)?,?\s*(?:and\s+)?darts?\s+(?:her\s+)?"
                r"eyes?\s+toward\s+(?:the\s+)?(?:late-night\s+)?clock"
                r"(?:\s+glow)?\b",
                "freezes; lowers phone; eyes clock",
            ),
            (
                r"\bfreezes?\s+mid-scroll,?\s*lowers?\s+(?:the\s+)?phone"
                r"(?:\s+slightly)?,?\s*(?:and\s+)?looks?\s+toward\s+"
                r"(?:the\s+)?clock\b",
                "freezes; lowers phone; eyes clock",
            ),
            (
                r"\blooks?\s+back\s+at\s+(?:the\s+)?phone,?\s*repeats?"
                r"\s+rapid\s+upward\s+swipes?.*?\b(?:thumb\s+)?"
                r"stop(?:s|ping)?(?:\s+above\s+(?:the\s+)?screen)?\b",
                "rapidly swipes; thumb stops",
            ),
            (
                r"\brepeats?\s+rapid\s+upward\s+swipes?\s+and\s+"
                r"(?:suddenly\s+)?stops?\b",
                "rapidly swipes; stops",
            ),
            (
                r"\brepeats?\s+rapid\s+upward\s+swipes?\b",
                "rapidly swipes",
            ),
            (
                r"\b(?:her\s+)?thumb\s+(?:stop(?:s|ping)?|is\s+stopped)"
                r"(?:\s+above|\s+over)?\s+(?:the\s+)?screen\b",
                "thumb stops",
            ),
            (
                r"\b(?:her\s+)?stopped\s+hand\s+hovers?\s+over\s+"
                r"(?:the\s+)?phone\s+as\s+(?:the\s+)?repeated[- ]swipe"
                r"\s+light\s+trail\s+collapses?\s+into\s+darkness\b",
                "hand hovers; swipe trail fades",
            ),
            (
                r"\b(?:her\s+)?stopped\s+hand\s+hovers?\s+as\s+"
                r"(?:the\s+)?light\s+trail\s+collapses?\b",
                "hand hovers; trail fades",
            ),
            (
                r"\bunplugs?\s+(?:the\s+)?cable\s+and\s+(?:reveals?|shows?)"
                r"\s+(?:the\s+)?(?:product\s+)?bottle\b",
                "product-package-visible",
            ),
            (
                r"\bunplugs?\s+(?:the\s+)?cable\s+and\s+follows?\s+its"
                r"\s+movement\s+to\s+(?:the\s+)?nightstand\b",
                "unplugs cable; follows to nightstand",
            ),
            (
                r"\b(?:she\s+)?(?:decisively\s+)?turns?\s+(?:the\s+)?"
                r"phone\s+face-down\s+in\s+(?:her\s+)?palm\s+and\s+"
                r"sits?\s+up\b",
                "phone-face-down; sits up",
            ),
            (
                r"\bher\s+thumb\s+keeps\s+scrolling\s+while\s+an\s+"
                r"oversized\s+phone\s+portal\s+pulls\s+luminous\s+shards\s+"
                r"into\s+the\s+room\b",
                "scrolling phone portal pulls luminous shards",
            ),
            (
                r"\bshow\s+(?:the\s+)?woman\s+awake\s+in\s+bed\s+"
                r"holding\s+(?:a\s+)?phone\b",
                "woman awake in bed with phone",
            ),
            (
                r"\b(?:she|the\s+woman|the\s+protagonist)\s+holds\s+"
                r"(?:the\s+)?uploaded\s+bottle\s+in\s+(?:a\s+)?warm\s+"
                r"setting\s+and\s+presents\s+exactly\s+two\s+gummies\b",
                "holds uploaded bottle; presents exactly two gummies",
            ),
            (
                r"\b(?:in\s+)?(?:the\s+)?cool-blue\s+bedroom,?\s+"
                r"(?:the\s+)?woman\b",
                "woman in cool-blue bedroom",
            ),
            (
                r"\b(?:she|the\s+woman|the\s+protagonist)\s+makes\s+"
                r"one\s+deliberate\s+choice[:,]?\s+place(?:s)?\s+(?:the\s+)?"
                r"phone\s+face-down\s+on\s+(?:the\s+)?bedside\b",
                "phone face-down on bedside",
            ),
            (
                r"\b(?:the\s+)?phone\s+(?:already\s+)?face[- ]down\s+on\s+"
                r"(?:the\s+)?bedside\s+surface\s+with\s+(?:the\s+)?"
                r"woman's\s+hand\s+fully\s+withdrawn\b",
                "phone face-down; hand out of frame",
            ),
            (
                r"\bhold\s+(?:the\s+)?bottle\s+in\s+(?:the\s+)?warm\s+"
                r"setting\b",
                "hold bottle in warm setting",
            ),
            (
                r"^use\s+hard\s+cuts\s+between\s+three\s+completed\s+"
                r"states:\s*",
                "",
            ),
            (
                r"\b(?:the\s+)?room\s+changes\s+from\s+cool-blue\s+"
                r"phone\s+light\s+to\s+warm\s+light\b",
                "light blue-to-warm",
            ),
            (
                r"\bhard\s+cut\s+(?:back\s+)?from\s+(.+?)\s+to\s+"
                r"(.+?)(?=(?:[.;]|$))",
                r"\1 -> \2",
            ),
            (
                r"\b(?:the\s+)?(?:narrator|protagonist)\s+(?:is\s+)?"
                r"(?:seated|sits)\s+(?:beside|on)\s+(?:the\s+)?"
                r"(?:bed|bedside)\b",
                "",
            ),
            (
                r"\b(?:she|narrator|protagonist)\s+turns\s+(?:the\s+)?"
                r"phone\s+face-down\b",
                "phone face-down",
            ),
            (
                r"\b(?:the\s+)?phone\s+is\s+(?:already\s+)?face-down\b",
                "phone face-down",
            ),
            (
                r"\b(?:both\s+)?hands\s+are\s+empty\b",
                "hands empty",
            ),
            (
                r"\b(?:exactly\s+)?two\s+unbranded\s+gummies\s+"
                r"(?:resting\s+)?(?:visibly\s+)?in\s+(?:her\s+)?"
                r"(?:open\s+)?(?:other\s+)?palm\b",
                "two unbranded gummies in palm",
            ),
            (
                r"\b(?:her\s+)?(?:other\s+)?open\s+palm\s+presents\s+"
                r"exactly\s+two\s+gummies\s+(?:separately\s+)?beside\s+"
                r"(?:the\s+)?package\b",
                "two gummies in open palm beside package",
            ),
            (
                r"\b(?:the\s+)?(?:narrator|protagonist)\s+makes\s+"
                r"one\s+single\s+fingertip\s+tap\b",
                "one fingertip tap",
            ),
            (r"\bhard\s+cut\s+(?:back\s+)?to\b", ""),
        )
        for pattern, replacement in canonical_states:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        # Exact readable values live in the lossless Must lane. Remove their
        # verbose source clauses from Beats so the scarce motion lane can keep
        # the human action around them instead of repeating the same digits.
        text = _READABLE_VALUE_RE.sub("", text)
        text = re.sub(r"\b(?:and|with)\s*(?=[;,.]|$)", "", text, flags=re.IGNORECASE)
        # Keep object orientation as one semantic token during fair-share
        # allocation.  Otherwise a tiny lane can retain only ``phone`` and
        # discard the state-changing ``face-down`` suffix.
        text = re.sub(
            r"\bphone\s+face-down\b",
            "phone-face-down",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\b(?:decisively\s+)?turns?\s+(?:the\s+)?phone-face-down\b",
            "phone-face-down",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\bphone-face-down\s+and\s+sits?\s+up\b",
            "phone-face-down; sits up",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\b(?:the\s+)?(?:MYUPONA\s+|product\s+)?bottle\s+"
            r"(?:becomes?|is)\s+(?:clearly\s+)?visible\b",
            "product-bottle-visible",
            text,
            flags=re.IGNORECASE,
        )
        # Put immutable state changes before descriptive motion.  The
        # word-boundary compactor keeps the start of a tiny beat; without this
        # ordering, a required bottle reveal at the end of a natural sentence
        # can disappear even though less important travel motion survives.
        priority_tokens = (
            "product-bottle-visible",
            "phone-face-down",
            "phone-screen-up",
            "exactly-two-gummies",
        )
        prioritized: list[str] = []
        for token in priority_tokens:
            if token.casefold() not in text.casefold():
                continue
            text = re.sub(
                re.escape(token),
                "",
                text,
                count=1,
                flags=re.IGNORECASE,
            ).strip(" ,.;")
            prioritized.append(token)
        if prioritized:
            text = "; ".join([*prioritized, text] if text else prioritized)
        text = re.sub(
            r"\bphone\s+screen-up\b",
            "phone-screen-up",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\bexactly\s+two\s+gummies\b",
            "exactly-two-gummies",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\bamber\s+pulse\b",
            "amber-pulse",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\s+", " ", text).strip(" ,.;")
        if " -> " in text:
            # In a transition the post-cut state is the actual acceptance
            # target.  Put it first before round-robin token allocation so a
            # small provider lane cannot preserve only the pre-cut setup and
            # silently lose the required final object state.
            before, after = text.split(" -> ", 1)
            text = f"{after}; {before}"
        text = re.sub(
            r"^(?:the\s+)?silent\s+visible\s+protagonist\b",
            "She",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"^the\s+protagonist\b",
            "She",
            text,
            flags=re.IGNORECASE,
        )
        # Reference images already identify the cast.  Remove only a leading
        # subject label so a tiny per-beat budget starts on the observable
        # verb instead of returning an empty action because a long subject
        # such as ``female-presenting protagonist`` consumed the whole lane.
        text = re.sub(
            r"^(?:the\s+)?(?:(?:female|male)[- ]presenting\s+)?"
            r"(?:adult\s+)?(?:protagonist|woman|man|character)\s+",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"^(?:she|he|they|her|his|their)\s+",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"^tight\s+on\s+the\s+protagonist's\b",
            "Her",
            text,
            flags=re.IGNORECASE,
        )
        clauses = [
            part.strip(" ,")
            for part in re.split(
                r"\s*;\s*|\s*->\s*|\.\s+|:\s*|\s+while\s+|,\s*then\s+|"
                r"\s+and\s+then\s+|,\s*|\s*；\s*|\s*。\s*|"
                r"\s*：\s*|\s*，\s*",
                text,
                flags=re.IGNORECASE,
            )
            if part.strip(" ,")
        ]
        filler_words = {
            "a", "an", "the", "same", "silent", "visible", "protagonist",
            "authoritative", "oversized",
            "continues", "begins", "starts", "completely", "naturally",
            "softly", "implied", "generic", "containing",
            # The ordered package reference already carries label color and
            # layout.  Spend the scarce text lane on semantic product facts
            # (brand, Melatonin-free, serving action) instead.
            "purple", "label", "front", "marking", "enters",
            "now", "still", "aggressive", "tight", "stable",
            "narrator", "seated", "hard", "quiet", "fully",
            "separately", "already", "both", "bare", "transition",
        }

        clause_phrases = []
        for clause in clauses:
            clause = re.sub(
                r"^(?:she|he|they|her|his|their)\s+",
                "",
                clause,
                flags=re.IGNORECASE,
            )
            if _CJK_CHARACTER_RE.search(clause):
                # The AI has already authored this provider-facing Chinese
                # action under a combined character budget.  Preserve its
                # wording and order; the English stop-word tokenizer below is
                # not a semantic tokenizer for CJK text.
                clause_phrases.append(clause.strip(" ，。；、,:;."))
                continue
            words = [
                word
                for word in re.findall(
                    r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*",
                    clause,
                )
                if word.lower() not in filler_words
            ]
            if words:
                clause_phrases.append(" ".join(words))
        if not clause_phrases:
            return ""
        # Keep word order and grammatical action phrases.  The old
        # round-robin/equal-clause allocator could turn a beat into fragments
        # such as ``cool-blue; physical; 43``.  Exact states are separately
        # protected by Must, so this lane should remain a readable motion
        # sentence rather than scattering isolated tokens from every clause.
        # Add complete clauses only. Partially appending the next clause made
        # valid AI plans reach providers as fragments such as ``; In`` or
        # ``woman s``. If even the first clause is too long, trim only that
        # clause at a word boundary.
        kept_phrases: list[str] = []
        for phrase in clause_phrases:
            candidate = "; ".join([*kept_phrases, phrase])
            if len(candidate) > budget:
                break
            kept_phrases.append(phrase)
        phrases = (
            "; ".join(kept_phrases)
            if kept_phrases
            else _compact_text_no_ellipsis(clause_phrases[0], budget)
        )
        phrases = re.sub(
            r"\s+(?:a|an|the|at|and|or|as|despite|toward|towards|to|with|"
            r"while|into|from|for|of|near|over|under)$",
            "",
            phrases,
            flags=re.IGNORECASE,
        )
        return (
            phrases
            .replace("exactly-two-gummies", "exactly two gummies")
            .replace("amber-pulse", "amber pulse")
            .replace("product-package-visible", "product package visible")
            .replace("phone-face-down", "phone face-down")
        )

    prefix, separator, body = str(line or "").partition(":")
    if not separator:
        return _compact_text(line, limit)
    rows = [row.strip() for row in body.split(" | ") if row.strip()]
    if not rows:
        return ""
    kept_prefix = "Beats: "
    separators = 3 * (len(rows) - 1)
    each = max(18, (limit - len(kept_prefix) - separators) // len(rows))
    compacted: list[str] = []
    for row in rows:
        action_part, fx_separator, effects_part = row.partition("; FX:")
        effects_part, camera_separator, camera_part = effects_part.partition("; Cam:")
        time_match = re.match(r"^([^:]{1,24}:)\s*(.*)$", action_part)
        time_label = time_match.group(1) if time_match else ""
        action_text = time_match.group(2) if time_match else action_part
        label_budget = len(time_label) + (1 if time_label else 0)
        payload_budget = max(18, each - label_budget)
        if payload_budget < 52:
            # Six or more rapid beats can leave fewer than forty characters
            # per row after references, product authority and output controls.
            # In that case preserve a readable action for every timestamp;
            # the ordered reference already anchors appearance and the full
            # effect/camera contract remains in task metadata for QA/retry.
            # Product visibility is an immutable conversion beat.  Generic
            # word-boundary compaction could otherwise keep ``unplugs cable``
            # and cut the package noun from the same final beat.  Preserve the
            # observable package state explicitly while its exact geometry
            # remains owned by the ordered product reference.
            if _PRODUCT_PACKAGE_VISIBLE_RE.search(action_text):
                payload = "product package visible"
            elif _NEGATIVE_PRODUCT_RE.search(action_text):
                payload = "no product visible"
            else:
                payload = semantic_clauses(action_text, payload_budget)
        elif fx_separator and camera_separator:
            # Final animation framing is already visible in the ordered image
            # anchors.  Inside Doubao's 495-character limit, spend the text
            # lane on what a still cannot specify: action and temporal FX.
            action_budget = max(12, int(payload_budget * 0.60))
            effects_budget = max(10, payload_budget - action_budget - 5)
            payload = (
                semantic_clauses(action_text, action_budget)
                + "; FX: "
                + semantic_clauses(effects_part, effects_budget)
            )
        elif fx_separator:
            action_budget = max(12, int(payload_budget * 0.58))
            effects_budget = max(10, payload_budget - action_budget - 5)
            payload = (
                semantic_clauses(action_text, action_budget)
                + "; FX: "
                + semantic_clauses(effects_part, effects_budget)
            )
        else:
            payload = semantic_clauses(action_text, payload_budget)
        compacted.append((time_label + " " + payload).strip())
    return kept_prefix + " | ".join(compacted)


def _tiny_voice_lock(voice_line: str, dialogue_line: str) -> str:
    text = f"{voice_line} {dialogue_line}".lower()
    gender = (
        "female"
        if "female" in text
        else "male"
        if "male" in text
        else "same"
    )
    owner = (
        "visible protagonist"
        if "visible protagonist" in text
        else "off-screen narrator"
        if "off-screen narrator" in text
        else "narrator"
        if "narrator" in text
        else "speaker"
    )
    # Speech pace is part of the signed performance direction.  The former
    # tiny form kept only gender/ownership, so a deliberately brisk TikTok
    # read became an unqualified generic narration after transport-budget
    # compaction.  Preserve the explicit rate without inventing a new one.
    rate_match = re.search(
        r"\b(\d{2,3})\s*(?:words?\s+per\s+minute|wpm)\b",
        text,
        flags=re.IGNORECASE,
    )
    rate = f", {rate_match.group(1)} wpm" if rate_match else ""
    return f"Voice: same {gender} {owner}, US{rate}"


def _tiny_product_authority(reference_line: str) -> str:
    """Bind package authority to its explicit ordered reference handle."""

    package = ""
    for part in str(reference_line or "").split(";"):
        if re.search(r"\b(?:package|product)\b", part, flags=re.IGNORECASE):
            handles = re.findall(r"@image\d+", part)
            if handles:
                package = handles[0]
                break
    return (
        f"Product: {package} sole package authority."
        if package
        else "Product: uploaded package is sole authority."
    )


def _compact_voice_lock(line: str) -> str:
    prefix = "Voice lock for this segment:"
    body = line[len(prefix):].strip() if line.startswith(prefix) else line
    head = re.split(r"\.\s+This speaker is explicitly", body, maxsplit=1)[0]
    head = _compact_text(head, 155)
    gender_match = re.search(
        r"This speaker is explicitly\s+(female|male|androgynous)",
        body,
        flags=re.IGNORECASE,
    )
    gender = gender_match.group(1).lower() if gender_match else ""
    parts = [f"{prefix} {head}"]
    if gender:
        parts.append(
            f"This speaker is explicitly {gender}; do not change the speaker's gender."
        )
    lowered = body.lower()
    if "lip-sync only that same character" in lowered:
        parts.append("Lip-sync only that same character.")
    elif "visible protagonist's own voiceover" in lowered:
        parts.append(
            "This is the visible protagonist's own voiceover, not an independent narrator."
        )
    elif "off-screen narrator" in lowered:
        parts.append("This is an off-screen narrator; visible characters stay silent.")
    parts.append(
        "Keep the same speaker identity, timbre, pitch, accent, and delivery in adjacent clips."
    )
    return " ".join(parts)


def _lean_voice_lock(line: str) -> str:
    body = line.split(":", 1)[-1]
    gender_match = re.search(
        r"explicitly\s+(female|male|androgynous)", body, flags=re.IGNORECASE
    )
    gender = gender_match.group(1).lower() if gender_match else "same"
    accent = "General American" if "general american" in body.lower() else "same accent"
    owner = (
        "visible protagonist"
        if "visible protagonist" in body.lower()
        else "off-screen narrator"
        if "off-screen narrator" in body.lower()
        else "same speaker"
    )
    return (
        f"Voice: same {gender} {owner}, {accent}; preserve identity, timbre, "
        "accent and delivery across clips; no speaker or gender change."
    )


def _ultra_lean_voice_lock(line: str) -> str:
    """Keep the signed speaker identity inside very small web UI budgets."""
    body = line.split(":", 1)[-1]
    gender_match = re.search(
        r"explicitly\s+(female|male|androgynous)", body, flags=re.IGNORECASE
    )
    gender = gender_match.group(1).lower() if gender_match else "same"
    owner = (
        "visible protagonist"
        if "visible protagonist" in body.lower()
        else "off-screen narrator"
        if "off-screen narrator" in body.lower()
        else "speaker"
    )
    return (
        f"Voice: same {gender} {owner}; preserve accent, timbre and delivery; "
        "no speaker/gender change."
    )


def _lean_provider_prompt(lines: list[str], *, limit: int) -> str:
    """Build a short execution packet for web providers with small budgets."""
    by_prefix = {
        prefix: next((line for line in lines if line.startswith(prefix)), "")
        for prefix in (
            "Visual style (signed whole-video contract):",
            "Reference bindings:",
            "Timeline (this segment only):",
            "Beats:",
            "Motion and effects:",
            "Dialogue:",
            "Voice lock for this segment:",
            "Voice:",
            "Audio:",
            "Continuity:",
            "Product presentation policy:",
            "Output:",
            "Segment scope:",
            "Refs:",
            "Repair:",
            "Must:",
            "Product:",
            "Character continuity:",
            "Direction:",
        )
    }
    dialogue = _lean_dialogue_line(by_prefix["Dialogue:"])
    direction = _compact_direction_line(by_prefix["Direction:"], 96)
    audio = by_prefix["Audio:"]
    local_voiceover = bool(
        audio
        and (
            "voiceover" in audio.lower()
            or "no lip-sync" in audio.lower()
            or "no speech" in audio.lower()
            or "silent" in audio.lower()
        )
    )
    if not dialogue and not local_voiceover:
        raise ValueError("structured provider prompt has no approved dialogue line")
    scope_match = re.search(r"(\d+\s*/\s*\d+)", by_prefix["Segment scope:"])
    scope = scope_match.group(1).replace(" ", "") if scope_match else "this segment"
    if local_voiceover:
        # The provider owns only the visual/motion lane.  Spend its scarce
        # 495-character composer budget on ordered reference anchors, concrete
        # actions and effects; deterministic narration is muxed and verified
        # after the generated clip is downloaded.
        style_body = by_prefix[
            "Visual style (signed whole-video contract):"
        ].partition(":")[2].strip()
        stylized_animation = "animation" in style_body.lower()
        references = _tiny_reference_bindings(
            by_prefix["Reference bindings:"] or by_prefix["Refs:"]
        )
        # The AI-authored Beats lane carries the concrete temporal execution.
        # Keep Repair concise so it cannot crowd those observable states out
        # of Doubao's 495-character composer budget.
        repair = _compact_repair_line(by_prefix["Repair:"], 145)
        if repair.casefold().strip(" .") == "repair: obey beats".casefold():
            repair = ""
        invariants = structured_video_prompt_semantic_invariants("\n".join(lines))
        must = (
            "Must: " + _compact_semantic_invariants(invariants)
            if invariants
            else ""
        )
        if len(must) > 190:
            raise ValueError(
                "structured provider semantic contract exceeds the small-model "
                "prompt lane; split the segment into fewer observable beats"
            )
        product = (
            _tiny_product_authority(
                by_prefix["Reference bindings:"] or by_prefix["Refs:"]
            ) + " In-scene; no white packshot."
            if (
                by_prefix["Product presentation policy:"]
                or by_prefix["Product:"]
            )
            else ""
        )
        character_continuity = _compact_prefixed_line(
            by_prefix["Character continuity:"], 105
        )
        # Keep an explicit Audio prefix so this already-compacted packet can
        # be safely compacted again after Seedance reference bindings and the
        # segment scope are appended.  Without the prefix, the second pass
        # mistook a valid local-voiceover packet for missing provider speech.
        audio_output = (
            "Audio: no speech/lip-sync; 9:16"
            + (" stylized animation" if stylized_animation else "")
            + "; no text/UI/watermark."
        )
        timeline_source = (
            _merge_timeline_and_motion_lines(
                by_prefix["Timeline (this segment only):"]
                or by_prefix["Beats:"],
                by_prefix["Motion and effects:"],
            )
        )
        # First ask whether the motion lane itself can carry every immutable
        # state.  Rapid six-beat clips often repeat phone/product facts in a
        # separate Must line, consuming enough of a 495-character request to
        # turn real actions into fragments.  Omit that duplicate lane only
        # when a provisional compacted timeline demonstrably contains every
        # invariant; exact clock values and quantities therefore still keep
        # their Must lane whenever the action text cannot carry them.
        if must and timeline_source:
            provisional_fixed = [
                value
                for value in (references, repair, product, audio_output)
                if value
            ]
            provisional_budget = max(
                54,
                limit
                - sum(len(value) for value in provisional_fixed)
                - len(provisional_fixed)
                - 3,
            )
            provisional_timeline = _compact_local_visual_timeline(
                timeline_source,
                provisional_budget,
            )
            normalized_provisional = _normalized_semantic(
                provisional_timeline
            )
            missing_invariants = [
                invariant
                for invariant in invariants
                if _normalized_semantic(invariant) not in normalized_provisional
            ]
            must = (
                "Must: " + _compact_semantic_invariants(missing_invariants)
                if missing_invariants
                else ""
            )
        fixed = [
            value
            for value in (
                references,
                repair,
                must,
                direction,
                character_continuity,
                product,
                audio_output,
            )
            if value
        ]
        timeline_budget = max(
            54,
            limit - sum(len(value) for value in fixed) - len(fixed) - 3,
        )
        timeline = _compact_local_visual_timeline(
            timeline_source,
            timeline_budget,
        )
        compacted = [
            value
            for value in (
                references,
                repair,
                must,
                direction,
                timeline,
                character_continuity,
                product,
                audio_output,
            )
            if value
        ]
        result = "\n".join(compacted)
        compacted, timeline = _maximize_timeline_in_packet(
            compacted,
            timeline=timeline,
            timeline_source=timeline_source,
            limit=limit,
        )
        result = "\n".join(compacted)
        if len(result) > limit and timeline:
            excess = len(result) - limit
            timeline_index = compacted.index(timeline)
            compacted[timeline_index] = _compact_local_visual_timeline(
                timeline_source,
                max(72, len(timeline) - excess),
            )
            result = "\n".join(compacted)
        if len(result) > limit:
            # Extreme Seedance budget: retain all multimodal lanes but shrink
            # their prose, never their ordered aliases or the QA repair itself.
            # The full signed/replanned contracts remain in task metadata.
            tight_repair = _compact_repair_line(
                by_prefix["Repair:"],
                145,
            )
            tight_timeline = _compact_text_no_ellipsis(
                _compact_local_visual_timeline(timeline_source, 140),
                140,
            )
            tight_audio = (
                "Audio: silent; no lip-sync; 9:16 animation; no text/UI/watermark."
            )
            tight_references = _compact_text_no_ellipsis(references, 100)
            tight_product = (
                _tiny_product_authority(
                    by_prefix["Reference bindings:"] or by_prefix["Refs:"]
                )
                if product
                else ""
            )
            compacted = [
                value
                for value in (
                    tight_references,
                    tight_repair,
                    must,
                    _compact_direction_line(direction, 72),
                    tight_timeline,
                    character_continuity,
                    tight_product,
                    tight_audio,
                )
                if value
            ]
            result = "\n".join(compacted)
        if len(result) > limit:
            # Future provider budgets and ten-image packets may still be
            # tighter. Shrink the redundant timeline before the authoritative
            # repair, while keeping every multimodal lane present.
            overflow = len(result) - limit
            if tight_timeline:
                tight_timeline = _compact_text_no_ellipsis(
                    tight_timeline,
                    max(34, len(tight_timeline) - overflow),
                )
            compacted = [
                value
                for value in (
                    tight_references,
                    tight_repair,
                    must,
                    _compact_direction_line(direction, 72),
                    tight_timeline,
                    character_continuity,
                    tight_product,
                    tight_audio,
                )
                if value
            ]
            result = "\n".join(compacted)
        if len(result) > limit and tight_repair:
            overflow = len(result) - limit
            tight_repair = _compact_repair_line(
                tight_repair,
                max(96, len(tight_repair) - overflow),
            )
            compacted = [
                value
                for value in (
                    tight_references,
                    tight_repair,
                    must,
                    _compact_direction_line(direction, 72),
                    tight_timeline,
                    character_continuity,
                    tight_product,
                    tight_audio,
                )
                if value
            ]
            result = "\n".join(compacted)
        if len(result) > limit:
            raise ValueError(
                "local-voiceover visual execution controls exceed the "
                f"declared {limit}-character prompt limit"
            )
        validate_structured_video_prompt_fidelity(
            "\n".join(lines),
            result,
        )
        return result
    copy_delivery_repair = next(
        (
            line
            for line in lines
            if line.startswith("COPY DELIVERY REPAIR (highest audio priority):")
        ),
        "",
    )
    if copy_delivery_repair:
        invariants = structured_video_prompt_semantic_invariants(
            "\n".join(lines)
        )
        must = "Must: " + "; ".join(invariants) if invariants else ""
        references = _tiny_reference_bindings(
            by_prefix["Reference bindings:"] or by_prefix["Refs:"]
        )
        # The multimodal execution author writes the provider-ready temporal
        # choreography into Timeline/Beats.  ``Motion and effects`` is older
        # signed-plan context and may describe a superseded camera mechanic.
        # Under Doubao's small composer budget, preferring that legacy prose
        # silently replaced every authored beat and then head-truncated it.
        # Keep every timestamp from the newest execution authority instead.
        timeline_source = (
            _merge_timeline_and_motion_lines(
                by_prefix["Timeline (this segment only):"]
                or by_prefix["Beats:"],
                by_prefix["Motion and effects:"],
            )
        )
        motion = _compact_local_visual_timeline(
            timeline_source,
            165,
        )
        product = (
            _tiny_product_authority(
                by_prefix["Reference bindings:"] or by_prefix["Refs:"]
            )
            if (
                by_prefix["Product presentation policy:"]
                or by_prefix["Product:"]
            )
            else ""
        )
        repaired = [
            "Audio: start at 0.0s; speak Dialogue verbatim once; no omissions.",
            references,
            must,
            direction,
            motion,
            _compact_prefixed_line(
                by_prefix["Character continuity:"], 105
            ),
            dialogue,
                _tiny_voice_lock(
                    by_prefix["Voice lock for this segment:"]
                    or by_prefix["Voice:"],
                    by_prefix["Dialogue:"],
                ),
                product,
                "9:16; this segment only; no text/UI/watermark.",
            ]
        repaired = [line for line in repaired if line and not line.endswith(":")]
        result = "\n".join(repaired)
        if len(result) > limit and motion:
            excess = len(result) - limit
            motion_index = repaired.index(motion)
            repaired[motion_index] = _compact_local_visual_timeline(
                timeline_source,
                max(72, len(motion) - excess),
            )
            result = "\n".join(repaired)
        if len(result) > limit:
            raise ValueError(
                "approved dialogue and copy-delivery controls exceed the "
                f"declared {limit}-character prompt limit"
            )
        return result

    invariants = structured_video_prompt_semantic_invariants(
        "\n".join(lines)
    )
    must = "Must: " + "; ".join(invariants) if invariants else ""
    # A model-authored QA repair is the reason this paid retry exists.  The
    # native-speech compact lane previously parsed ``Repair:`` but omitted it
    # from both the normal and ultra-small packets, so retries repeated the
    # same pronunciation or added-copy failure even though the full repair
    # contract remained in task metadata.  Keep a bounded execution repair
    # beside the exact dialogue; references still own appearance and Beats
    # still own chronology.
    repair = _compact_repair_line(by_prefix["Repair:"], 145)
    if len(must) > 220:
        raise ValueError(
            "structured provider semantic contract exceeds the model prompt "
            "lane; split the segment into fewer observable beats"
        )
    # A retry can already be in the compact ``Beats``/``Product`` dialect.
    # Treat those lanes as first-class inputs on every pass.  The previous
    # native-voice path only copied verbose Timeline/Motion and Product-policy
    # fields; an already compacted retry therefore kept its dialogue but
    # silently dropped all timed actions and package authority.
    timeline_source = (
        _merge_timeline_and_motion_lines(
            by_prefix["Timeline (this segment only):"]
            or by_prefix["Beats:"],
            by_prefix["Motion and effects:"],
        )
    )
    product = (
        _tiny_product_authority(
            by_prefix["Reference bindings:"] or by_prefix["Refs:"]
        )
        if (
            by_prefix["Product presentation policy:"]
            or by_prefix["Product:"]
        )
        else ""
    )
    first_line_is_structured = any(
        lines[0].startswith(prefix)
        for prefix in _STRUCTURED_PREFIXES
    )
    compacted = [
        (
            _compact_text(lines[0], 70)
            if not first_line_is_structured
            else ""
        ),
        _compact_prefixed_line(
            by_prefix["Visual style (signed whole-video contract):"], 95
        ),
        _tiny_reference_bindings(
            by_prefix["Reference bindings:"] or by_prefix["Refs:"]
        ),
        repair,
        must,
        direction,
        _compact_prefixed_line(by_prefix["Character continuity:"], 105),
        _compact_local_visual_timeline(timeline_source, 210),
        dialogue,
        _lean_voice_lock(
            by_prefix["Voice lock for this segment:"] or by_prefix["Voice:"]
        ),
        _compact_prefixed_line(by_prefix["Continuity:"], 85),
        product,
        "No captions, overlays, sales UI, QR, collage, inset, playback UI or watermark.",
        f"Output: 9:16 720p, English (US), one full-frame scene; segment {scope} only.",
    ]
    compacted = [line for line in compacted if line and not line.endswith(":")]
    result = "\n".join(compacted)
    if len(result) <= limit:
        return result

    # Preserve exact dialogue and execution locks; progressively shrink only
    # descriptive fields.  This remains a view of the signed AI plan, not a
    # rewritten creative contract.
    for prefix, minimum in (
        ("Beats:", 120),
        ("Motion and effects:", 70),
        ("Timeline (this segment only):", 220),
        ("Product presentation policy:", 70),
        ("Continuity:", 55),
        ("Visual style (signed whole-video contract):", 55),
    ):
        for index, line in enumerate(compacted):
            if not line.startswith(prefix) or len(result) <= limit:
                continue
            target = max(minimum, len(line) - (len(result) - limit))
            compacted[index] = (
                _compact_local_visual_timeline(line, target)
                if prefix == "Beats:"
                else _compact_timed_line(line, target)
                if prefix in {
                    "Timeline (this segment only):",
                    "Motion and effects:",
                }
                else _compact_prefixed_line(line, target)
            )
            result = "\n".join(compacted)
    if len(result) > limit:
        # Doubao's verified composer reads at most 500 characters, including
        # the five-character transport command. Preserve the Director-owned
        # dialogue verbatim and reduce only the execution view around it.
        timeline_source = (
            _merge_timeline_and_motion_lines(
                by_prefix["Timeline (this segment only):"]
                or by_prefix["Beats:"],
                by_prefix["Motion and effects:"],
            )
        )
        motion = _compact_local_visual_timeline(
            timeline_source,
            165,
        )
        product = (
            _tiny_product_authority(
                by_prefix["Reference bindings:"] or by_prefix["Refs:"]
            )
            if (
                by_prefix["Product presentation policy:"]
                or by_prefix["Product:"]
            )
            else ""
        )
        ultra_voice = _tiny_voice_lock(
            by_prefix["Voice lock for this segment:"]
            or by_prefix["Voice:"],
            by_prefix["Dialogue:"],
        ).replace(" female off-screen narrator", " female off-screen")
        ultra = [
            _tiny_reference_bindings(
                by_prefix["Reference bindings:"] or by_prefix["Refs:"]
            ),
            repair,
            must,
            _compact_direction_line(direction, 82),
            motion,
            _compact_prefixed_line(
                by_prefix["Character continuity:"], 90
            ),
            dialogue,
            ultra_voice,
            product,
            "9:16; no text/UI/watermark.",
        ]
        ultra = [line for line in ultra if line and not line.endswith(":")]
        result = "\n".join(ultra)
        if len(result) > limit and motion:
            excess = len(result) - limit
            motion_index = ultra.index(motion)
            ultra[motion_index] = _compact_local_visual_timeline(
                timeline_source,
                max(72, len(motion) - excess),
            )
            motion = ultra[motion_index]
            result = "\n".join(ultra)
        if len(result) > limit and must and must in ultra:
            # Must is deterministically derived from the same Beats line. In
            # the emergency 495-character lane the concrete timed action is
            # the less ambiguous representation, so remove only this duplicate
            # summary before sacrificing Dialogue or Repair.
            ultra.remove(must)
            result = "\n".join(ultra)
        if len(result) > limit:
            character_line = next(
                (
                    line
                    for line in ultra
                    if line.startswith("Character continuity:")
                ),
                "",
            )
            if character_line:
                # Ordered references still carry character identity. This
                # descriptive duplicate is expendable in the verified short
                # composer lane; dialogue, repair, action and product authority
                # are not.
                ultra.remove(character_line)
                result = "\n".join(ultra)
        if len(result) > limit and repair and repair in ultra:
            repair_index = ultra.index(repair)
            ultra[repair_index] = _compact_repair_line(
                repair,
                max(64, len(repair) - (len(result) - limit)),
            )
            result = "\n".join(ultra)
        if len(result) > limit:
            # The final small-composer lane may carry exact dialogue, a signed
            # voice rate and a model-authored Direction simultaneously.  Spend
            # the remaining overflow on duplicate prose summaries in a stable
            # order: first Repair (the timed Beats already enact it), then the
            # wording of Direction (never the lane itself), and only then the
            # beat prose.  Reference aliases, timestamps, exact Dialogue,
            # voice identity/rate and product authority remain untouched.
            repair_line = next(
                (line for line in ultra if line.startswith("Repair:")),
                "",
            )
            if repair_line:
                repair_index = ultra.index(repair_line)
                ultra[repair_index] = _compact_repair_line(
                    repair_line,
                    # Keep the observable repair noun and action (for example
                    # shoulder application).  Direction and beat prose below
                    # are safer overflow lanes than reducing this to a vague
                    # body-part token.
                    max(54, len(repair_line) - (len(result) - limit)),
                )
                result = "\n".join(ultra)
        if len(result) > limit:
            direction_line = next(
                (line for line in ultra if line.startswith("Direction:")),
                "",
            )
            if direction_line:
                direction_index = ultra.index(direction_line)
                ultra[direction_index] = _compact_direction_line(
                    direction_line,
                    max(52, len(direction_line) - (len(result) - limit)),
                )
                result = "\n".join(ultra)
        if len(result) > limit and motion and motion in ultra:
            motion_index = ultra.index(motion)
            ultra[motion_index] = _compact_local_visual_timeline(
                timeline_source,
                max(60, len(motion) - (len(result) - limit)),
            )
            motion = ultra[motion_index]
            result = "\n".join(ultra)
        if len(result) > limit:
            raise ValueError(
                "approved dialogue and essential provider controls exceed the "
            f"declared {limit}-character prompt limit"
        )
    return result


def compact_structured_video_prompt(value: str, *, max_characters: int) -> str:
    """Fit a structured provider prompt without altering approved dialogue.

    Full production contracts remain in task metadata.  This function only
    compacts the provider-facing, segment-local execution view.  Dialogue is
    intentionally never truncated because it is owned by the Director.
    """

    prompt = str(value or "").strip()
    limit = int(max_characters)
    if limit < 256:
        raise ValueError("provider prompt limit must be at least 256 characters")
    if len(prompt) <= limit:
        # A short packet still must respect the reference/prompt authority
        # boundary.  Reference descriptions are internal visual-review notes,
        # not provider story instructions; normalize them to ordered handles
        # even when no character-budget compaction is otherwise necessary.
        if is_structured_video_prompt(prompt):
            normalized_lines = []
            for line in prompt.splitlines():
                stripped = line.strip()
                if stripped.startswith(("Reference bindings:", "Refs:")):
                    normalized_lines.append(_tiny_reference_bindings(stripped))
                else:
                    normalized_lines.append(stripped)
            normalized = "\n".join(line for line in normalized_lines if line)
            if len(normalized) <= limit:
                return normalized
        return prompt

    lines = [line.strip() for line in prompt.splitlines() if line.strip()]
    if limit <= 1400:
        return _lean_provider_prompt(lines, limit=limit)
    compacted: list[str] = []
    for index, line in enumerate(lines):
        if line.startswith("Dialogue:"):
            compacted.append(line)
        elif line.startswith("Beats:"):
            compacted.append(_compact_timed_line(line, 700))
        elif line.startswith("Direction:"):
            compacted.append(_compact_direction_line(line, 260))
        elif line.startswith("Repair:"):
            compacted.append(_compact_repair_line(line, 300))
        elif line.startswith("Refs:"):
            compacted.append(_tiny_reference_bindings(line))
        elif line.startswith("Must:"):
            compacted.append(_compact_prefixed_line(line, 300))
        elif line.startswith("Product:"):
            compacted.append(_compact_prefixed_line(line, 180))
        elif line.startswith("Voice:"):
            compacted.append(_compact_prefixed_line(line, 220))
        elif line.startswith("Audio:"):
            compacted.append(_compact_prefixed_line(line, 180))
        elif line.startswith("Voice lock for this segment:"):
            compacted.append(_compact_voice_lock(line))
        elif line.startswith("Timeline (this segment only):"):
            compacted.append(_compact_timed_line(line, 460))
        elif line.startswith("Motion and effects:"):
            compacted.append(_compact_timed_line(line, 320))
        elif line.startswith("Reference bindings:"):
            compacted.append(_lean_reference_bindings(line))
        elif line.startswith("Visual style (signed whole-video contract):"):
            compacted.append(_compact_prefixed_line(line, 180))
        elif line.startswith("Project visual-medium constraint"):
            compacted.append(_compact_prefixed_line(line, 180))
        elif line.startswith("Continuity:"):
            compacted.append(_compact_prefixed_line(line, 140))
        elif line.startswith("Product presentation policy:"):
            compacted.append(_compact_prefixed_line(line, 150))
        elif line.startswith("Signed intent requirements for this segment:"):
            compacted.append(_compact_prefixed_line(line, 220))
        elif line.startswith("Avoid:"):
            compacted.append(_compact_prefixed_line(line, 130))
        elif line.startswith("Do not model-render"):
            compacted.append(
                "No generated captions, overlays, sales UI, QR codes, collage, "
                "picture-in-picture, or watermark; preserve the exact dialogue."
            )
        elif line.startswith("Segment scope:"):
            match = re.search(r"(\d+\s*/\s*\d+)", line)
            scope = match.group(1).replace(" ", "") if match else "this segment"
            compacted.append(
                f"Segment scope: {scope} only; do not preplay or replay other segments."
            )
        elif line.startswith("One continuous full-frame scene"):
            compacted.append(
                "One continuous full-frame scene; no inset, playback UI, watermark, "
                "or language mixing."
            )
        elif index == 0:
            compacted.append(_compact_text(line, 100))
        else:
            compacted.append(_compact_text(line, 220))

    result = "\n".join(compacted)
    if len(result) <= limit:
        return result

    minimum_by_prefix = {
        "Beats:": 360,
        "Direction:": 120,
        "Repair:": 120,
        "Must:": 140,
        "Product:": 90,
        "Voice:": 120,
        "Audio:": 90,
        "Timeline (this segment only):": 300,
        "Motion and effects:": 140,
        "Visual style (signed whole-video contract):": 90,
        "Project visual-medium constraint": 90,
        "Voice lock for this segment:": 180,
        "Continuity:": 80,
        "Product presentation policy:": 100,
        "Signed intent requirements for this segment:": 100,
        "Avoid:": 70,
    }
    shrink_order = tuple(minimum_by_prefix)
    for prefix in shrink_order:
        if len(result) <= limit:
            break
        for index, line in enumerate(compacted):
            if not line.startswith(prefix):
                continue
            excess = len(result) - limit
            minimum = minimum_by_prefix[prefix]
            target = max(minimum, len(line) - excess)
            if prefix in {"Beats:", "Timeline (this segment only):", "Motion and effects:"}:
                compacted[index] = _compact_timed_line(line, target)
            elif prefix == "Direction:":
                compacted[index] = _compact_direction_line(line, target)
            elif prefix == "Repair:":
                compacted[index] = _compact_repair_line(line, target)
            else:
                compacted[index] = _compact_prefixed_line(line, target)
            result = "\n".join(compacted)
            if len(result) <= limit:
                break

    if len(result) > limit:
        raise ValueError(
            "approved dialogue and mandatory provider controls exceed the "
            f"declared {limit}-character prompt limit"
        )
    return result


_DOUBAO_STRUCTURED_PREFIXES_ZH = {
    "Visual style (signed whole-video contract):": "视觉风格：",
    "Reference bindings:": "参考图绑定：",
    "Timeline (this segment only):": "本片段时间线：",
    "Motion and effects:": "动作与特效：",
    "Dialogue:": "对白（保持原文逐字表达）：",
    "Voice lock for this segment:": "本片段声音锁定：",
    "Voice:": "声音：",
    "Audio:": "音频：",
    "Continuity:": "连续性：",
    "Product presentation policy:": "产品呈现规则：",
    "Segment scope:": "片段范围：",
    "Refs:": "参考图：",
    "Beats:": "本片段时间线：",
    "Repair:": "修复要求：",
    "Must:": "必须保留：",
    "Product:": "产品：",
    "Direction:": "节奏镜头风格：",
}


def localize_structured_video_prompt_for_doubao(value: str) -> str:
    """Use Chinese transport controls for Doubao without rewriting creative copy.

    The Director-owned actions and approved dialogue remain byte-for-byte intact.
    Only stable execution labels and negative/provider controls are localized;
    this gives Doubao native Chinese instructions without introducing a second AI
    rewrite or risking translation drift in spoken English copy.
    """

    localized: list[str] = []
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for source, target in _DOUBAO_STRUCTURED_PREFIXES_ZH.items():
            if line.startswith(source):
                line = target + line[len(source):].lstrip()
                break
        if line == "No captions, overlays, sales UI, QR, collage, inset, playback UI or watermark.":
            line = "不要字幕、叠加文字、销售界面、二维码、拼贴、画中画、播放界面或水印。"
        elif re.fullmatch(
            r"9:16; no captions/UI/watermark; segment \d+/\d+\.",
            line,
            flags=re.IGNORECASE,
        ):
            scope = line.rsplit(" ", 1)[-1].rstrip(".")
            line = f"9:16；不要字幕、界面或水印；只生成片段{scope}。"
        elif line.startswith("Output: 9:16 720p, English (US), one full-frame scene; segment "):
            scope = line.removeprefix(
                "Output: 9:16 720p, English (US), one full-frame scene; segment "
            ).removesuffix(" only.")
            line = f"输出9:16、720p、英文（美国）；单一全画幅场景；仅生成片段{scope}。"
        line = line.replace(
            "uploaded package is sole authority.",
            "上传包装图是唯一产品外观权威。",
        )
        line = line.replace(
            "In-scene; no white background/packshot.",
            "自然融入场景；禁止白底或商品卡式贴图。",
        )
        line = line.replace(
            "No white packshot.",
            "禁止白底商品图式贴图。",
        )
        localized.append(line)
    return "\n".join(localized)


__all__ = ["compact_structured_video_prompt", "is_structured_video_prompt"]
