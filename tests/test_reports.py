"""地区差距、多维成效（不唯流量）与审计下钻投影。"""

from __future__ import annotations

from tests.conftest import make_evidence_set
from tests.flow import assign, register_project_and_app
from cultural_fund.payments import SimulatedPaymentGateway


def _paid(svc, office, applicant, biz, fin, *, project_id, app_id, region,
          pay_id, service_count=1200):
    register_project_and_app(svc, office, applicant, project_id=project_id,
                             application_id=app_id, region=region)
    assign(svc, office, app_id, biz=biz, fin=fin)
    org = applicant.org_id
    svc.submit_stage_evidence(
        applicant, application_id=app_id, stage_id="s1",
        evidence=make_evidence_set(app_id, org, service_count=service_count),
    )
    svc.business_review(biz, application_id=app_id, stage_id="s1", decision="approved")
    svc.finance_review(fin, application_id=app_id, stage_id="s1", decision="approved")
    svc.lock_stage(fin, application_id=app_id, stage_id="s1", payment_id=pay_id)
    svc.attempt_payment(fin, payment_id=pay_id, gateway=SimulatedPaymentGateway(),
                        idempotency_key=f"k-{pay_id}")


def test_region_gap_report_compares_benefit_vs_commitment(
        svc, office, org_a, org_b, biz, fin):
    # 城区已支付；县域只提交未锁定
    _paid(svc, office, org_a, biz, fin, project_id="P1", app_id="appA",
          region="城区", pay_id="payA")
    register_project_and_app(svc, office, org_b, project_id="P2", application_id="appB",
                             region="县域")
    assign(svc, office, "appB", biz=biz, fin=fin)
    svc.submit_stage_evidence(
        org_b, application_id="appB", stage_id="s1",
        evidence=make_evidence_set("appB", "org-b", service_count=1200),
    )

    rows = {r["region"]: r for r in svc.region_gap_report()}
    city = rows["城区"]
    county = rows["县域"]
    assert city["locked_stages"] == 1 and city["paid_amount"] == "96000.00"
    assert city["committed_amount"] == "120000.00"
    assert city["payment_gap_amount"] == "24000.00"
    assert county["locked_stages"] == 0 and county["paid_amount"] == "0.00"
    assert county["payment_gap_amount"] == "120000.00"


def test_effectiveness_report_is_multi_dimensional_and_warns_on_traffic_only(
        svc, office, org_a, biz, fin):
    register_project_and_app(svc, office, org_a)
    assign(svc, office, "app1", biz=biz, fin=fin)
    svc.submit_stage_evidence(
        org_a, application_id="app1", stage_id="s1",
        evidence=make_evidence_set("a", "org-a", service_count=1200, satisfaction=4.8,
                                   views=999999),
    )
    report = svc.effectiveness_report()
    assert report["traffic_metric_key"] == "views"
    dims = {d["key"]: d for d in report["dimensions"]}
    assert dims["views"]["traffic"] is True
    assert dims["service_count"]["traffic"] is False
    stage = report["applications"][0]["stages"][0]
    # 高传播量与服务、满意度并列，不允许流量单独决定成效
    assert stage["metrics_actual"]["views"] == "999999"
    assert stage["metrics_actual"]["service_count"] == "1200"
    assert stage["warnings"] == []  # 多维度齐备，不告警
    checks = {c["key"]: c["status"] for c in stage["commitment_checks"]}
    assert checks == {"service_count": "met", "satisfaction": "met"}


def test_traffic_only_evidence_warns(svc, office, org_a, biz, fin):
    # 构造只提供流量指标的服务记录（仍需三类证据）
    register_project_and_app(svc, office, org_a)
    assign(svc, office, "app1", biz=biz, fin=fin)
    evidence = [
        {"evidence_id": "c", "kind": "contract", "title": "合同", "submitted_by_org": "org-a",
         "achievement": {"identity": {"doc": "c"}}, "metrics": {}},
        {"evidence_id": "f", "kind": "public_feedback", "title": "反馈", "submitted_by_org": "org-a",
         "achievement": {"identity": {"fb": "f"}}, "metrics": {}},
        {"evidence_id": "s", "kind": "service_record", "title": "记录", "submitted_by_org": "org-a",
         "achievement": {"identity": {"act": "only-views"}},
         "metrics": {"views": "888888"}},
    ]
    svc.submit_stage_evidence(org_a, application_id="app1", stage_id="s1", evidence=evidence)
    report = svc.effectiveness_report()
    stage = report["applications"][0]["stages"][0]
    assert any("流量" in w for w in stage["warnings"])


def test_audit_drilldown_shows_rule_evidence_dedup_chain(
        svc, office, org_a, org_b, biz, fin):
    """审计从拨付结果下钻：采用证据、去重决定、审批链、当时有效规则齐备。"""
    from cultural_fund.eventstore import Actor
    register_project_and_app(svc, office, org_a, project_id="P1", application_id="appA")
    register_project_and_app(svc, office, org_b, project_id="P2", application_id="appB",
                             region="县域")
    assign(svc, office, "appA", biz=biz, fin=fin)
    assign(svc, office, "appB", biz=biz, fin=fin)
    svc.submit_stage_evidence(org_a, application_id="appA", stage_id="s1",
                              evidence=make_evidence_set("a", "org-a", service_count=2000, case="joint"))
    svc.submit_stage_evidence(org_b, application_id="appB", stage_id="s1",
                              evidence=make_evidence_set("b", "org-b", service_count=2000, case="joint"))
    dispute_id = svc.repo.list_disputes()[0].dispute_id
    svc.resolve_dispute(
        office, dispute_id=dispute_id, decision="shared", rationale="审计可查的五五分成",
        allocations=[
            {"application_id": "appA", "stage_id": "s1", "evidence_id": "a-service",
             "org_id": "org-a", "adopted": True, "credited_share": "0.5"},
            {"application_id": "appB", "stage_id": "s1", "evidence_id": "b-service",
             "org_id": "org-b", "adopted": True, "credited_share": "0.5"},
        ],
    )
    svc.business_review(biz, application_id="appA", stage_id="s1", decision="approved")
    svc.finance_review(fin, application_id="appA", stage_id="s1", decision="approved")
    svc.lock_stage(fin, application_id="appA", stage_id="s1", payment_id="payA")
    svc.attempt_payment(fin, payment_id="payA", gateway=SimulatedPaymentGateway(),
                        idempotency_key="k-A")

    drill = svc.audit_drilldown("payA")
    # 当时有效规则
    assert drill["effective_rule_at_lock"]["version"] == 1
    assert drill["effective_rule_at_lock"]["snapshot"]["subsidy_rate"] == "0.80"
    # 采用证据
    adopted = {e["evidence_id"]: e for e in drill["adopted_evidence"]}
    assert adopted["a-service"]["adopted"] is True
    assert adopted["a-service"]["credited_share"] == "0.5"
    # 去重决定
    dedup = drill["dedup"]["disputes"][0]
    assert dedup["dispute_id"] == dispute_id
    assert dedup["resolution"]["rationale"] == "审计可查的五五分成"
    assert len(dedup["resolution"]["allocations"]) == 2
    # 审批链
    steps = [c["step"] for c in drill["approval_chain"] if c]
    assert steps == ["business", "finance", "lock"]
    assert drill["approval_chain"][0]["reviewer"] == "u-biz"
    assert drill["approval_chain"][1]["reviewer"] == "u-fin"
    # 支付过程
    assert drill["payment"]["status"] == "paid"
    assert drill["payment"]["attempts"][0]["result"] == "succeeded"
