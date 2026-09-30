"""联合申报重复成果：指纹去重 -> 归属协商 -> 裁决 -> 分成计入成效与额度。"""

from __future__ import annotations

import pytest

from cultural_fund import domain as dm
from cultural_fund.errors import IllegalStateError, ValidationError
from tests.conftest import make_evidence_set
from tests.flow import assign, register_project_and_app


def _setup_two_apps(svc, office, org_a, org_b, biz, fin):
    register_project_and_app(svc, office, org_a, project_id="P1", application_id="appA")
    register_project_and_app(svc, office, org_b, project_id="P2", application_id="appB",
                             region="县域")
    assign(svc, office, "appA", biz=biz, fin=fin)
    assign(svc, office, "appB", biz=biz, fin=fin)


def _submit_both(svc, org_a, org_b, *, service_count=2000):
    svc.submit_stage_evidence(
        org_a, application_id="appA", stage_id="s1",
        evidence=make_evidence_set("a", "org-a", service_count=service_count, case="joint"),
    )
    svc.submit_stage_evidence(
        org_b, application_id="appB", stage_id="s1",
        evidence=make_evidence_set("b", "org-b", service_count=service_count, case="joint"),
    )


def test_duplicate_achievement_opens_dispute_and_blocks_review(
        svc, office, org_a, org_b, biz, fin):
    _setup_two_apps(svc, office, org_a, org_b, biz, fin)
    _submit_both(svc, org_a, org_b)

    disputes = svc.repo.list_disputes()
    assert len(disputes) == 1
    dispute = disputes[0]
    assert dispute.status == dm.DISPUTE_OPEN
    assert len(dispute.candidates) == 2
    candidate_orgs = {c["org_id"] for c in dispute.candidates}
    assert candidate_orgs == {"org-a", "org-b"}

    # 双方阶段都进入争议态，业务审核被阻止
    assert svc.repo.application("appA").stages["s1"]["status"] == dm.STAGE_DISPUTED
    assert svc.repo.application("appB").stages["s1"]["status"] == dm.STAGE_DISPUTED
    with pytest.raises(IllegalStateError):
        svc.business_review(biz, application_id="appA", stage_id="s1", decision="approved")


def test_same_org_cannot_claim_same_achievement_twice(svc, office, org_a, org_b, biz, fin):
    _setup_two_apps(svc, office, org_a, org_b, biz, fin)
    _submit_both(svc, org_a, org_b)
    # org-a 在另一个项目/阶段再次报同一成果：拒绝
    register_project_and_app(svc, office, org_a, project_id="P3", application_id="appC")
    assign(svc, office, "appC", biz=biz, fin=fin)
    dup = make_evidence_set("c", "org-a", case="joint")
    with pytest.raises(ValidationError, match="同一单位"):
        svc.submit_stage_evidence(org_a, application_id="appC", stage_id="s1", evidence=dup)


def test_resolve_dispute_shared_then_review_and_payable(
        svc, office, org_a, org_b, biz, fin):
    _setup_two_apps(svc, office, org_a, org_b, biz, fin)
    _submit_both(svc, org_a, org_b, service_count=2000)
    dispute = svc.repo.list_disputes()[0]

    svc.record_negotiation(org_a, dispute_id=dispute.dispute_id, proposal="五五分成")
    svc.record_negotiation(org_b, dispute_id=dispute.dispute_id, proposal="同意五五分成")

    allocations = [
        {"application_id": "appA", "stage_id": "s1", "evidence_id": "a-service",
         "org_id": "org-a", "adopted": True, "credited_share": "0.5"},
        {"application_id": "appB", "stage_id": "s1", "evidence_id": "b-service",
         "org_id": "org-b", "adopted": True, "credited_share": "0.5"},
    ]
    svc.resolve_dispute(office, dispute_id=dispute.dispute_id, decision="shared",
                        rationale="联合共创，五五分成", allocations=allocations)

    # 争议解决后进入待评审，证据被采纳且分成 0.5
    assert svc.repo.dispute(dispute.dispute_id).status == dm.DISPUTE_RESOLVED_STATUS
    for app_id in ("appA", "appB"):
        stage = svc.repo.application(app_id).stages["s1"]
        assert stage["status"] == dm.STAGE_PENDING_REVIEW
        svc_ev = [e for e in stage["evidence"].values() if e["kind"] == "service_record"][0]
        assert svc_ev["adopted"] is True
        assert svc_ev["credited_share"] == "0.5"
        assert svc_ev["dedup_group_id"] == f"dedup-{dispute.dispute_id}"

    # 成效按分成加权：service_count 2000*0.5=1000 恰好达标；不重复计算
    svc.business_review(biz, application_id="appA", stage_id="s1", decision="approved")
    svc.finance_review(fin, application_id="appA", stage_id="s1", decision="approved")
    stage_a = svc.repo.application("appA").stages["s1"]
    # 120000*0.8=96000，去重折算仅针对服务记录（分成0.5）
    assert stage_a["payable_amount"] == "48000.00"
    calc = stage_a["finance_review"]["calc"]
    assert calc["dedup_factor"] == "0.5000"
    assert calc["deduped_evidence_ids"] == ["a-service"]


def test_resolve_dispute_single_owner_rejects_loser(svc, office, org_a, org_b, biz, fin):
    _setup_two_apps(svc, office, org_a, org_b, biz, fin)
    _submit_both(svc, org_a, org_b)
    dispute = svc.repo.list_disputes()[0]
    svc.resolve_dispute(
        office, dispute_id=dispute.dispute_id, decision="single_owner", rationale="合同与原始记录归A",
        allocations=[
            {"application_id": "appA", "stage_id": "s1", "evidence_id": "a-service",
             "org_id": "org-a", "adopted": True, "credited_share": "1.0"},
            {"application_id": "appB", "stage_id": "s1", "evidence_id": "b-service",
             "org_id": "org-b", "adopted": False, "credited_share": "0"},
        ],
    )
    stage_b = svc.repo.application("appB").stages["s1"]
    loser = [e for e in stage_b["evidence"].values() if e["evidence_id"] == "b-service"][0]
    assert loser["adopted"] is False
    assert loser["credited_share"] == "0"
    # B 缺少被采纳的服务记录，三类证据不齐，业务审核不能通过
    with pytest.raises(IllegalStateError):
        svc.business_review(biz, application_id="appB", stage_id="s1", decision="approved")


def test_resolution_must_cover_all_candidates_and_share_sum_one(
        svc, office, org_a, org_b, biz, fin):
    _setup_two_apps(svc, office, org_a, org_b, biz, fin)
    _submit_both(svc, org_a, org_b)
    dispute = svc.repo.list_disputes()[0]
    # 缺少一个候选
    with pytest.raises(ValidationError, match="全部候选证据"):
        svc.resolve_dispute(
            office, dispute_id=dispute.dispute_id, decision="shared", rationale="缺一方",
            allocations=[
                {"application_id": "appA", "stage_id": "s1", "evidence_id": "a-service",
                 "org_id": "org-a", "adopted": True, "credited_share": "1.0"},
            ],
        )
    # 分成之和不等于 1
    with pytest.raises(ValidationError, match="之和必须为 1"):
        svc.resolve_dispute(
            office, dispute_id=dispute.dispute_id, decision="shared", rationale="和不为一",
            allocations=[
                {"application_id": "appA", "stage_id": "s1", "evidence_id": "a-service",
                 "org_id": "org-a", "adopted": True, "credited_share": "0.6"},
                {"application_id": "appB", "stage_id": "s1", "evidence_id": "b-service",
                 "org_id": "org-b", "adopted": True, "credited_share": "0.6"},
            ],
        )


def test_locked_achievement_cannot_be_claimed_again(svc, office, org_a, org_b, biz, fin):
    register_project_and_app(svc, office, org_a, project_id="P1", application_id="appA")
    assign(svc, office, "appA", biz=biz, fin=fin)
    svc.submit_stage_evidence(
        org_a, application_id="appA", stage_id="s1",
        evidence=make_evidence_set("a", "org-a", service_count=1200),
    )
    svc.business_review(biz, application_id="appA", stage_id="s1", decision="approved")
    svc.finance_review(fin, application_id="appA", stage_id="s1", decision="approved")
    svc.lock_stage(fin, application_id="appA", stage_id="s1", payment_id="payA")

    # 阶段锁定后，另一主体就同一成果申报 -> 拒绝（保护已公示口径）
    register_project_and_app(svc, office, org_b, project_id="P2", application_id="appB",
                             region="县域")
    assign(svc, office, "appB", biz=biz, fin=fin)
    with pytest.raises(IllegalStateError, match="已随阶段锁定"):
        svc.submit_stage_evidence(
            org_b, application_id="appB", stage_id="s1",
            evidence=make_evidence_set("b", "org-b", service_count=1200, case="a"),
        )


def test_third_party_joins_open_dispute(svc, office, org_a, org_b, biz, fin):
    _setup_two_apps(svc, office, org_a, org_b, biz, fin)
    _submit_both(svc, org_a, org_b)
    # 第三个主体就同一成果申报：并入既有未决争议，而不是新开
    org_c = Actor("u-org-c", "applicant", org_id="org-c", display_name="C单位")
    register_project_and_app(svc, office, org_c, project_id="P3", application_id="appC",
                             region="滨海")
    assign(svc, office, "appC", biz=biz, fin=fin)
    svc.submit_stage_evidence(
        org_c, application_id="appC", stage_id="s1",
        evidence=make_evidence_set("c", "org-c", service_count=2000, case="joint"),
    )
    disputes = svc.repo.list_disputes()
    assert len(disputes) == 1
    assert len(disputes[0].candidates) == 3
    assert svc.repo.application("appC").stages["s1"]["status"] == dm.STAGE_DISPUTED


from cultural_fund.eventstore import Actor  # noqa: E402
