"""聚合状态 -> 可对外 JSON 化的只读视图。

敏感字段（secret_id、合同正文）永不通过普通视图外泄。
"""

from __future__ import annotations

from typing import Any

from . import domain as dm

PUBLIC_EVIDENCE_FIELDS = (
    "evidence_id",
    "stage_id",
    "kind",
    "title",
    "submitted_by_org",
    "fingerprint",
    "achievement",
    "metrics",
    "sensitive",
    "adopted",
    "credited_share",
    "dedup_group_id",
    "dedup_rationale",
    "dispute_id",
    "submitted_event_id",
)


def public_evidence(ev: dict[str, Any]) -> dict[str, Any]:
    out = {k: ev.get(k) for k in PUBLIC_EVIDENCE_FIELDS}
    out["has_secret"] = bool(ev.get("secret_id"))
    return out


def stage_view(stage: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage_id": stage["stage_id"],
        "name": stage["name"],
        "commitment": stage["commitment"],
        "planned_amount": stage["planned_amount"],
        "due_date": stage["due_date"],
        "status": stage["status"],
        "business_reviewer": stage["business_reviewer"],
        "finance_reviewer": stage["finance_reviewer"],
        "business_review": stage["business_review"],
        "finance_review": stage["finance_review"],
        "locked": stage["locked"],
        "payable_amount": stage["payable_amount"],
        "payment_id": stage["payment_id"],
        "dispute_ids": stage["dispute_ids"],
        "evidence": [public_evidence(ev) for ev in stage["evidence"].values()],
    }


def application_view(app: dm.ApplicationState) -> dict[str, Any]:
    return {
        "application_id": app.application_id,
        "project_id": app.project_id,
        "rule_code": app.rule_code,
        "rule_version_at_submission": app.rule_version_at_submission,
        "fiscal_year": app.fiscal_year,
        "applicant_org_id": app.applicant_org_id,
        "partner_org_ids": app.partner_org_ids,
        "regions": app.regions,
        "submitted_at": app.submitted_at,
        "stages": [stage_view(s) for s in app.stages.values()],
        "payments": list(app.payments.values()),
    }


def project_view(project: dm.ProjectState) -> dict[str, Any]:
    return {
        "project_id": project.project_id,
        "name": project.name,
        "region": project.region,
        "applicant_org_id": project.applicant_org_id,
        "targets": project.targets,
        "funding_tranches": project.funding_tranches,
        "milestones": project.milestones,
        "merged_into": project.merged_into,
        "is_active": project.is_active,
        "region_history": project.region_history,
        "merge_history": project.merge_history,
        "carryover_history": project.carryover_history,
        "refund_history": project.refund_history,
    }


def rule_view(rule: dm.RuleState) -> dict[str, Any]:
    return {
        "code": rule.code,
        "name": rule.name,
        "current_version": rule.current["version"] if rule.current else None,
        "versions": rule.versions,
    }


def dispute_view(dispute: dm.DisputeState) -> dict[str, Any]:
    return {
        "dispute_id": dispute.dispute_id,
        "fingerprint": dispute.fingerprint,
        "achievement_ref": dispute.achievement_ref,
        "status": dispute.status,
        "candidates": dispute.candidates,
        "negotiations": dispute.negotiations,
        "resolution": dispute.resolution,
    }
