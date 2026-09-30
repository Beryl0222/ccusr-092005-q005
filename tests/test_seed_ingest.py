"""现有项目与里程碑资料（seed.json）录入后端。"""

from __future__ import annotations

import pytest

from cultural_fund.eventstore import Actor
from cultural_fund.ingest import build_stage_payloads, ingest_seed
from cultural_fund.services import CulturalFundService

pytestmark = pytest.mark.custom_rules


def test_seed_ingests_rules_projects_and_milestones(svc, office, seed_data):
    result = ingest_seed(svc, office, seed_data)
    assert set(result["projects"]) == {"program-digital-stage", "program-grassroots-tour"}

    rule = svc.repo.rule("rule-cultural-2026")
    assert rule is not None
    versions = {v["version"]: v for v in rule.versions}
    assert versions[1]["effective_date"] == "2026-01-01"
    assert versions[2]["effective_date"] == "2026-07-01"

    project = svc.project if hasattr(svc, "project") else None
    p = svc.repo.project("program-digital-stage")
    assert p.region == "城区"
    assert len(p.milestones) == 3
    assert p.milestones[0]["milestone_id"] == "milestone-stage-01"
    assert p.milestones[0]["commitment"]["metrics"]["service_count"] == 1000


def test_seed_milestones_build_stage_payloads(seed_data):
    payloads = build_stage_payloads(seed_data, "program-digital-stage")
    assert [s["stage_id"] for s in payloads] == [
        "milestone-stage-01", "milestone-stage-02", "milestone-stage-03",
    ]
    assert payloads[0]["planned_amount"] == "120000.00"


def test_seeded_project_can_be_claimed_and_paid(svc, office, seed_data, biz, fin, clock):
    """录入后的真实资料可以走完整申报-审核-支付流程。"""
    ingest_seed(svc, office, seed_data)
    org = Actor("u-org", "applicant", org_id="org-digital-center", display_name="数字中心")
    clock.set("2026-02-01T09:00:00+00:00")
    stages = build_stage_payloads(seed_data, "program-digital-stage")
    svc.submit_application(
        org, application_id="app-seed", project_id="program-digital-stage",
        fiscal_year=2026, stages=stages[:1],
    )
    svc.assign_reviewers(office, application_id="app-seed",
                         stage_id="milestone-stage-01",
                         business_reviewer=biz.user_id, finance_reviewer=fin.user_id)
    from tests.conftest import make_evidence_set
    svc.submit_stage_evidence(
        org, application_id="app-seed", stage_id="milestone-stage-01",
        evidence=make_evidence_set("seed", "org-digital-center", service_count=1200),
    )
    svc.business_review(biz, application_id="app-seed",
                        stage_id="milestone-stage-01", decision="approved")
    svc.finance_review(fin, application_id="app-seed",
                       stage_id="milestone-stage-01", decision="approved")
    svc.lock_stage(fin, application_id="app-seed",
                   stage_id="milestone-stage-01", payment_id="pay-seed")
    stage = svc.repo.application("app-seed").stages["milestone-stage-01"]
    # 120000 * 0.8 = 96000，且被 v1 上限 150000 放行
    assert stage["payable_amount"] == "96000.00"
    assert stage["locked"]["rule_version"] == 1
