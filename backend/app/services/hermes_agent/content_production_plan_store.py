from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.data.models.hermes_agent import (
    HermesContentFactoryProject,
    HermesContentFactoryStage,
    HermesContentProductionPlanAudit,
)
from app.services.hermes_agent.content_director import DirectedContentArtifact
from app.services.hermes_agent.content_production_plan_runtime import (
    ProductionPlanLoopResult,
)


def persist_content_production_plan_loop(
    db: Session,
    *,
    project: HermesContentFactoryProject,
    stage: HermesContentFactoryStage,
    variant_index: int,
    artifact: DirectedContentArtifact,
    result: ProductionPlanLoopResult,
) -> HermesContentProductionPlanAudit:
    """Append one immutable audit row for the exact stage delivery."""

    final_plan = result.final_plan
    if final_plan is None:
        raise ValueError(
            "production-plan audit requires a final signed plan"
        )
    existing = (
        db.query(HermesContentProductionPlanAudit)
        .filter(
            or_(
                HermesContentProductionPlanAudit.stage_id == int(stage.id),
                (
                    HermesContentProductionPlanAudit.project_id
                    == int(project.id)
                )
                & (
                    HermesContentProductionPlanAudit.plan_sha256
                    == final_plan.plan_sha256
                ),
            ),
        )
        .one_or_none()
    )
    if existing is not None:
        if (
            existing.plan_sha256 != final_plan.plan_sha256
            or existing.director_artifact_sha256
            != artifact.artifact_sha256
            or existing.variant_index != int(variant_index)
            or existing.project_id != int(project.id)
        ):
            raise ValueError(
                "production-plan stage already owns a different audit row"
            )
        return existing

    audit = HermesContentProductionPlanAudit(
        project_id=int(project.id),
        stage_id=int(stage.id),
        workspace_id=int(project.workspace_id),
        user_id=(
            int(project.user_id or stage.user_id)
            if (project.user_id or stage.user_id)
            else None
        ),
        variant_index=max(1, int(variant_index)),
        plan_id=final_plan.plan_id,
        plan_revision=int(final_plan.revision),
        director_artifact_sha256=artifact.artifact_sha256,
        plan_sha256=final_plan.plan_sha256,
        status=result.status,
        accepted=result.status == "approved",
        plan_json=final_plan.model_dump(mode="json"),
        attempts_json=[
            item.model_dump(mode="json") for item in result.attempts
        ],
        critic_attempts_json=[
            item.model_dump(mode="json")
            for item in result.critic_attempts
        ],
        reviews_json=[
            item.model_dump(mode="json") for item in result.reviews
        ],
        contract_errors_json=list(result.contract_errors),
        reason=result.reason,
    )
    db.add(audit)
    db.flush()
    return audit


__all__ = ["persist_content_production_plan_loop"]
