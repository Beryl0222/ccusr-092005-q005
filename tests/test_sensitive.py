"""敏感合同访问控制：仅实际审核者可读，访问（含拒绝）全部留痕。"""

from __future__ import annotations

import pytest

from cultural_fund.errors import PermissionDeniedError, ValidationError
from tests.conftest import make_evidence_set
from tests.flow import assign, register_project_and_app


def _submit_sensitive(svc, office, org_a, biz, fin):
    register_project_and_app(svc, office, org_a)
    assign(svc, office, "app1", biz=biz, fin=fin)
    evidence = make_evidence_set(
        "a", "org-a", sensitive=True,
        secret={"title": "敏感采购合同", "clauses": "合同正文：单价、账号等敏感条款"},
    )
    svc.submit_stage_evidence(org_a, application_id="app1", stage_id="s1", evidence=evidence)


def test_only_assigned_reviewers_can_read(svc, office, org_a, biz, biz2, fin, auditor):
    _submit_sensitive(svc, office, org_a, biz, fin)

    # 申报主体本人不能读合同正文
    with pytest.raises(PermissionDeniedError):
        svc.read_sensitive_contract(org_a, application_id="app1", evidence_id="a-contract")
    # 未被指派的业务审核者不能读
    with pytest.raises(PermissionDeniedError):
        svc.read_sensitive_contract(biz2, application_id="app1", evidence_id="a-contract")
    # 审计人员不是实际审核者：不能读正文，但可查访问日志
    with pytest.raises(PermissionDeniedError):
        svc.read_sensitive_contract(auditor, application_id="app1", evidence_id="a-contract")

    # 被指派的业务/财务审核者可读
    secret_biz = svc.read_sensitive_contract(biz, application_id="app1", evidence_id="a-contract")
    assert "合同正文" in secret_biz["clauses"]
    secret_fin = svc.read_sensitive_contract(fin, application_id="app1", evidence_id="a-contract")
    assert secret_fin["title"] == "敏感采购合同"

    log = svc.repo.application("app1").access_log
    assert len(log) == 5
    assert [entry["allowed"] for entry in log] == [False, False, False, True, True]
    assert all(entry["user_id"] for entry in log)


def test_sensitive_flag_only_for_contracts(svc, office, org_a, biz, fin):
    register_project_and_app(svc, office, org_a)
    assign(svc, office, "app1", biz=biz, fin=fin)
    bad = make_evidence_set("a", "org-a")
    bad[1]["sensitive"] = True  # public_feedback 标敏感
    with pytest.raises(ValidationError, match="仅合同"):
        svc.submit_stage_evidence(org_a, application_id="app1", stage_id="s1", evidence=bad)


def test_audit_view_never_exposes_contract_body(svc, office, org_a, biz, fin):
    _submit_sensitive(svc, office, org_a, biz, fin)
    svc.read_sensitive_contract(biz, application_id="app1", evidence_id="a-contract")
    svc.business_review(biz, application_id="app1", stage_id="s1", decision="approved")
    svc.finance_review(fin, application_id="app1", stage_id="s1", decision="approved")
    svc.lock_stage(fin, application_id="app1", stage_id="s1", payment_id="pay1")
    drill = svc.audit_drilldown("pay1")
    contract = [e for e in drill["adopted_evidence"] if e["evidence_id"] == "a-contract"][0]
    assert contract["sensitive"] is True
    assert contract["secret_revealed"] is False
    assert "secret_id" not in contract
    serialized = str(drill)
    assert "合同正文：单价" not in serialized
