"""测试用端到端流程辅助函数。"""

from __future__ import annotations

from typing import Any

from cultural_fund.eventstore import Actor
from cultural_fund.services import CulturalFundService


def register_project_and_app(
    svc: CulturalFundService,
    office: Actor,
    applicant: Actor,
    *,
    project_id: str = "P1",
    application_id: str = "app1",
    region: str = "城区",
    partner_org_ids: list[str] | None = None,
    planned_amount: str = "120000.00",
    commitment: dict[str, Any] | None = None,
    stage_id: str = "s1",
    fiscal_year: int = 2026,
):
    org_id = applicant.org_id or "org-a"
    svc.register_project(
        office,
        project_id=project_id,
        name=f"项目-{project_id}",
        region=region,
        applicant_org_id=org_id,
    )
    stages = [
        {
            "stage_id": stage_id,
            "name": "首期",
            "planned_amount": planned_amount,
            "commitment": commitment
            or {"metrics": {"service_count": 1000, "satisfaction": 4.5}},
        }
    ]
    svc.submit_application(
        applicant,
        application_id=application_id,
        project_id=project_id,
        fiscal_year=fiscal_year,
        partner_org_ids=partner_org_ids or [],
        stages=stages,
    )
    return stages


def assign(svc: CulturalFundService, office: Actor, application_id: str, stage_id: str = "s1",
           biz: Actor | None = None, fin: Actor | None = None):
    svc.assign_reviewers(
        office,
        application_id=application_id,
        stage_id=stage_id,
        business_reviewer=(biz or Actor("u-biz", "business_reviewer")).user_id,
        finance_reviewer=(fin or Actor("u-fin", "finance_reviewer")).user_id,
    )


def drive_to_paid(
    svc: CulturalFundService,
    office: Actor,
    biz: Actor,
    fin: Actor,
    *,
    application_id: str = "app1",
    stage_id: str = "s1",
    payment_id: str = "pay1",
    gateway=None,
    idem_prefix: str = "k",
):
    """业务审 -> 财务审 -> 锁定 -> 支付成功（网关可注入失败脚本）。"""
    svc.business_review(biz, application_id=application_id, stage_id=stage_id, decision="approved")
    svc.finance_review(fin, application_id=application_id, stage_id=stage_id, decision="approved")
    svc.lock_stage(fin, application_id=application_id, stage_id=stage_id, payment_id=payment_id)
    if gateway is not None:
        svc.attempt_payment(
            fin, payment_id=payment_id, gateway=gateway, idempotency_key=f"{idem_prefix}-1"
        )
