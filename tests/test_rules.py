"""规则发布、生效日期与换版：只影响尚未锁定的阶段。"""

from __future__ import annotations

import pytest

from cultural_fund.errors import IllegalStateError, ValidationError
from cultural_fund.repository import Repository
from tests.conftest import METRIC_DIMS
from tests.flow import assign, drive_to_paid, register_project_and_app
from cultural_fund.payments import SimulatedPaymentGateway


def test_rule_version_must_increase(svc, office, rule_v1):
    with pytest.raises(ValidationError):
        svc.publish_rule(office, code="R", name="x", version=1, effective_date="2026-03-01",
                         support_categories=[], stage_cap="1", subsidy_rate="0.5",
                         required_metrics=[], metric_dims=METRIC_DIMS)
    with pytest.raises(ValidationError):
        svc.publish_rule(office, code="R", name="x", version=0, effective_date="2026-03-01",
                         support_categories=[], stage_cap="1", subsidy_rate="0.5",
                         required_metrics=[], metric_dims=METRIC_DIMS)


def test_effective_rule_respects_date(svc, office, rule_v2, clock):
    rule = svc.repo.rule("R")
    assert rule.effective_at("2026-06-30")["version"] == 1
    # 7 月 1 日当天即适用新版
    assert rule.effective_at("2026-07-01")["version"] == 2
    assert rule.effective_at("2026-12-31")["version"] == 2


@pytest.mark.custom_rules
def test_cannot_apply_before_first_effective_date(store, office, clock, vault, org_a):
    # 只有 3 月才生效的规则，1 月不能申报
    from cultural_fund.services import CulturalFundService
    clock.set("2026-01-05T09:00:00+00:00")
    svc_late = CulturalFundService(store, clock, Repository(store), vault)
    svc_late.publish_rule(office, code="R", name="r", version=1, effective_date="2026-03-01",
                          support_categories=["x"], stage_cap="1", subsidy_rate="0.5",
                          required_metrics=[], metric_dims=METRIC_DIMS)
    svc_late.register_project(office, project_id="P1", name="p", region="城区",
                              applicant_org_id="org-a")
    with pytest.raises(IllegalStateError):
        svc_late.submit_application(
            org_a, application_id="app1", project_id="P1", fiscal_year=2026,
            stages=[{"stage_id": "s1", "name": "n", "planned_amount": "1", "commitment": {}}],
        )


from cultural_fund.eventstore import Actor  # noqa: E402
from cultural_fund.services import CulturalFundService  # noqa: E402


def test_revision_only_affects_unlocked_stages(svc, office, org_a, biz, fin, rule_v2, clock):
    """阶段 A 在 7 月前锁定（钉 v1）；阶段 B 在换版后审核，适用 v2。"""
    register_project_and_app(
        svc, office, org_a,
        project_id="P1", application_id="appA",
        planned_amount="120000.00",
        commitment={"metrics": {"service_count": 1000, "satisfaction": 4.5}},
    )
    assign(svc, office, "appA", biz=biz, fin=fin)
    from tests.conftest import make_evidence_set
    svc.submit_stage_evidence(
        org_a, application_id="appA", stage_id="s1",
        evidence=make_evidence_set("a", "org-a", service_count=1200),
    )
    # 6 月完成双审与锁定：适用 v1（0.8、上限 150000）
    clock.set("2026-06-15T09:00:00+00:00")
    svc.business_review(biz, application_id="appA", stage_id="s1", decision="approved")
    svc.finance_review(fin, application_id="appA", stage_id="s1", decision="approved")
    svc.lock_stage(fin, application_id="appA", stage_id="s1", payment_id="payA")
    locked_a = svc.repo.application("appA").stages["s1"]["locked"]
    assert locked_a["rule_version"] == 1
    assert locked_a["payable_amount"] == "96000.00"  # 120000 * 0.8

    # 第二个申报/阶段在 8 月审核：v2（0.9、上限 200000）
    clock.set("2026-08-01T09:00:00+00:00")
    register_project_and_app(
        svc, office, org_a,
        project_id="P2", application_id="appB",
        planned_amount="120000.00",
        commitment={"metrics": {"service_count": 1000, "satisfaction": 4.5}},
    )
    assign(svc, office, "appB", biz=biz, fin=fin)
    svc.submit_stage_evidence(
        org_a, application_id="appB", stage_id="s1",
        evidence=make_evidence_set("b", "org-a", service_count=1200),
    )
    svc.business_review(biz, application_id="appB", stage_id="s1", decision="approved")
    svc.finance_review(fin, application_id="appB", stage_id="s1", decision="approved")
    locked_b = svc.repo.application("appB").stages["s1"]
    assert locked_b["finance_review"]["rule_version"] == 2
    assert locked_b["payable_amount"] == "108000.00"  # 120000 * 0.9

    # 已锁定阶段 A 的口径不被换版影响
    assert svc.repo.application("appA").stages["s1"]["locked"]["rule_version"] == 1
    assert svc.repo.application("appA").stages["s1"]["payable_amount"] == "96000.00"


def test_cap_is_applied_under_v2(svc, office, org_a, biz, fin, rule_v2, clock):
    """v2 上限 200000：120000*0.9=108000 未触顶；超大额被截断到上限。"""
    register_project_and_app(svc, office, org_a, project_id="P1", application_id="app1",
                             planned_amount="500000.00")
    assign(svc, office, "app1", biz=biz, fin=fin)
    from tests.conftest import make_evidence_set
    clock.set("2026-08-01T09:00:00+00:00")
    svc.submit_stage_evidence(org_a, application_id="app1", stage_id="s1",
                              evidence=make_evidence_set("a", "org-a"))
    svc.business_review(biz, application_id="app1", stage_id="s1", decision="approved")
    svc.finance_review(fin, application_id="app1", stage_id="s1", decision="approved")
    assert svc.repo.application("app1").stages["s1"]["payable_amount"] == "200000.00"
