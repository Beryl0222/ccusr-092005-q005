"""双审门槛、支付失败重试与幂等闭环。"""

from __future__ import annotations

import pytest

from cultural_fund import domain as dm
from cultural_fund.errors import IllegalStateError, PermissionDeniedError
from cultural_fund.payments import SimulatedPaymentGateway
from tests.conftest import make_evidence_set
from tests.flow import assign, register_project_and_app


def _ready_stage(svc, office, org_a, biz, fin, application_id="app1"):
    register_project_and_app(svc, office, org_a, application_id=application_id)
    assign(svc, office, application_id, biz=biz, fin=fin)
    svc.submit_stage_evidence(
        org_a, application_id=application_id, stage_id="s1",
        evidence=make_evidence_set("a", "org-a", service_count=1200),
    )


def test_both_reviews_required_before_lock(svc, office, org_a, biz, fin):
    _ready_stage(svc, office, org_a, biz, fin)
    # 未双审不能锁定
    with pytest.raises(IllegalStateError):
        svc.lock_stage(fin, application_id="app1", stage_id="s1", payment_id="pay1")
    # 财务不能先于业务
    with pytest.raises(IllegalStateError):
        svc.finance_review(fin, application_id="app1", stage_id="s1", decision="approved")
    svc.business_review(biz, application_id="app1", stage_id="s1", decision="approved")
    # 只有被指派的财务审核者能审
    fin_other = Actor("u-fin2", "finance_reviewer", display_name="其他财务")
    with pytest.raises(PermissionDeniedError):
        svc.finance_review(fin_other, application_id="app1", stage_id="s1", decision="approved")
    svc.finance_review(fin, application_id="app1", stage_id="s1", decision="approved")
    svc.lock_stage(fin, application_id="app1", stage_id="s1", payment_id="pay1")
    assert svc.repo.application("app1").stages["s1"]["status"] == dm.STAGE_LOCKED


def test_payment_failure_then_retry_succeeds_without_double_charge(
        svc, office, org_a, biz, fin):
    _ready_stage(svc, office, org_a, biz, fin)
    svc.business_review(biz, application_id="app1", stage_id="s1", decision="approved")
    svc.finance_review(fin, application_id="app1", stage_id="s1", decision="approved")
    svc.lock_stage(fin, application_id="app1", stage_id="s1", payment_id="pay1")

    gateway = SimulatedPaymentGateway(fail_times={"pay1": 2})

    # 第 1 次失败
    r1 = svc.attempt_payment(fin, payment_id="pay1", gateway=gateway, idempotency_key="k-1")
    assert r1[0].data["result"] == "failed"
    assert svc.repo.application("app1").payments["pay1"]["status"] == dm.PAYMENT_FAILED

    # 客户端用同一幂等键重放：不再次扣款，回放首次失败事件
    replay = svc.attempt_payment(fin, payment_id="pay1", gateway=gateway, idempotency_key="k-1")
    assert replay[0].event_id == r1[0].event_id
    assert len(gateway.calls) == 1

    # 第 2、3 次尝试（新键）：第二次仍失败（网关配置失败 2 次），第三次成功
    r2 = svc.attempt_payment(fin, payment_id="pay1", gateway=gateway, idempotency_key="k-2")
    assert r2[0].data["result"] == "failed"
    r3 = svc.attempt_payment(fin, payment_id="pay1", gateway=gateway, idempotency_key="k-3")
    assert r3[0].data["result"] == "succeeded"

    pay = svc.repo.application("app1").payments["pay1"]
    assert pay["status"] == dm.PAYMENT_PAID
    assert [a["attempt_no"] for a in pay["attempts"]] == [1, 2, 3]
    assert [a["result"] for a in pay["attempts"]] == ["failed", "failed", "succeeded"]
    # 网关只被真正调用 3 次（幂等重放未扣款）
    assert len(gateway.calls) == 3
    svc.store.verify_chain()


def test_cannot_pay_twice_after_success(svc, office, org_a, biz, fin):
    _ready_stage(svc, office, org_a, biz, fin)
    svc.business_review(biz, application_id="app1", stage_id="s1", decision="approved")
    svc.finance_review(fin, application_id="app1", stage_id="s1", decision="approved")
    svc.lock_stage(fin, application_id="app1", stage_id="s1", payment_id="pay1")
    gateway = SimulatedPaymentGateway()
    svc.attempt_payment(fin, payment_id="pay1", gateway=gateway, idempotency_key="k-1")
    # 成功后用新键再发起：拒绝
    with pytest.raises(IllegalStateError):
        svc.attempt_payment(fin, payment_id="pay1", gateway=gateway, idempotency_key="k-2")
    # 但客户端超时后用同一键重放：回放成功事件，不再调用网关
    calls_before = len(gateway.calls)
    replay = svc.attempt_payment(fin, payment_id="pay1", gateway=gateway, idempotency_key="k-1")
    assert replay[0].data["result"] == "succeeded"
    assert len(gateway.calls) == calls_before


def test_idempotency_key_cannot_be_reused_across_payments(svc, office, org_a, biz, fin):
    # 两张支付单（同一项目主体的两个申报）
    from cultural_fund.errors import ValidationError
    _ready_stage(svc, office, org_a, biz, fin, application_id="app1")
    register_project_and_app(svc, office, org_a, project_id="P2", application_id="app2")
    assign(svc, office, "app2", biz=biz, fin=fin)
    svc.submit_stage_evidence(
        org_a, application_id="app2", stage_id="s1",
        evidence=make_evidence_set("app2", "org-a", service_count=1200, case="app2"),
    )
    for app_id, pay_id in [("app1", "pay1"), ("app2", "pay2")]:
        svc.business_review(biz, application_id=app_id, stage_id="s1", decision="approved")
        svc.finance_review(fin, application_id=app_id, stage_id="s1", decision="approved")
        svc.lock_stage(fin, application_id=app_id, stage_id="s1", payment_id=pay_id)
    gateway = SimulatedPaymentGateway()
    svc.attempt_payment(fin, payment_id="pay1", gateway=gateway, idempotency_key="shared-key")
    # 另一支付单复用同一幂等键：拒绝，避免扣款事实串单
    with pytest.raises(ValidationError, match="不能跨支付单复用"):
        svc.attempt_payment(fin, payment_id="pay2", gateway=gateway, idempotency_key="shared-key")


def test_business_review_rejects_unmet_commitment(svc, office, org_a, biz, fin):
    # 承诺 service_count 1000，但证据只有 800
    register_project_and_app(svc, office, org_a)
    assign(svc, office, "app1", biz=biz, fin=fin)
    svc.submit_stage_evidence(
        org_a, application_id="app1", stage_id="s1",
        evidence=make_evidence_set("a", "org-a", service_count=800),
    )
    with pytest.raises(IllegalStateError, match="service_count"):
        svc.business_review(biz, application_id="app1", stage_id="s1", decision="approved")


def test_reviewer_assignment_requires_separation(svc, office, org_a, biz, fin):
    register_project_and_app(svc, office, org_a)
    with pytest.raises(Exception):
        svc.assign_reviewers(office, application_id="app1", stage_id="s1",
                             business_reviewer="same-user", finance_reviewer="same-user")


from cultural_fund.eventstore import Actor  # noqa: E402
