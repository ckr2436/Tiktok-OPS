from app.services.hermes_agent.content_director import (
    DirectorCapabilityNode,
    ScriptLine,
    ScriptSegmentAllocation,
    VideoProgramSpec,
    build_script_package,
    preflight_script_copy,
)
from app.services.hermes_agent.content_director_profile import (
    compile_universal_director_series_brief,
)


def test_facts_product_identity_supplies_safe_spoken_shorthand_alias():
    series = compile_universal_director_series_brief(
        series_id="facts-alias",
        objective="Test a product conversion video.",
        platform="TikTok Shop US",
        locale="en-US",
        audience="US adults",
        target_count=1,
        minimum_duration_seconds=10,
        maximum_duration_seconds=10,
        product_required=True,
        brand_name="MYUPONA",
        product_name="MYUPONA SLEEP EASY GUMMIES",
        market="US",
        project_brief=None,
        confirmed_claims=["Melatonin-free"],
        product_truth={
            "facts_envelope": {
                "stage": "FACTS",
                "result": {
                    "approved_claims": ["MYUPONA Sleep Ease Gummies"],
                    "product_passport": {
                        "product_name": "Sleep Ease Gummies",
                        "product_form": "Gummies",
                    },
                    "product_truth_handoff": {
                        "PRODUCT": "MYUPONA Sleep Ease Gummies"
                    },
                },
            },
        },
    )

    assert "MYUPONA Sleep Ease Gummies" in series.conversion.product_name_aliases
    assert "MYUPONA Sleep Ease" in series.conversion.product_name_aliases

    program = VideoProgramSpec(
        program_id="program-1",
        objective="Test one conversion video.",
        content_type="product conversion",
        platform="TikTok Shop US",
        locale="en-US",
        audience="US adults",
        target_duration_seconds=10,
        aspect_ratio="9:16",
        conversion=series.conversion,
        execution_graph=[
            DirectorCapabilityNode(
                node_id="copy",
                capability="copy.write",
                input_contract="VideoProgramSpec",
                output_contract="ScriptPackage",
            )
        ],
        copy_review_criteria=series.copy_review_criteria,
    )
    script = build_script_package(
        script_id="script-1",
        program_id=program.program_id,
        locale="en-US",
        target_duration_seconds=10,
        edit_headroom_seconds=0,
        speech_rate_wpm=180,
        display_reading_rate_wpm=120,
        audio_mode="spoken",
        primary_speaker_id="narrator",
        lines=[
            ScriptLine(
                line_id="line_1",
                delivery_mode="spoken",
                speaker_id="narrator",
                text="That’s why I picked MYUPONA Sleep Ease.",
                beat_id="product",
                purpose="Name the confirmed product shorthand.",
            )
        ],
        segments=[
            ScriptSegmentAllocation(
                segment_index=1,
                duration_seconds=10,
                line_ids=["line_1"],
            )
        ],
    )

    report = preflight_script_copy(program, script)
    assert "PRODUCT_NOT_NAMED" not in {
        issue.code for issue in report.issues
    }


def test_generic_brand_only_name_does_not_create_weak_alias():
    series = compile_universal_director_series_brief(
        series_id="generic-alias",
        objective="Test a product conversion video.",
        platform="short-video",
        locale="en-US",
        audience="Adults",
        target_count=1,
        minimum_duration_seconds=10,
        maximum_duration_seconds=10,
        product_required=True,
        brand_name="ACME",
        product_name="ACME GUMMIES",
        market="US",
        project_brief=None,
        product_truth={
            "result": {
                "approved_claims": ["ACME Gummies"],
                "product_passport": {"product_name": "Gummies"},
            }
        },
    )

    assert "ACME" not in series.conversion.product_name_aliases
