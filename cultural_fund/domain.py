"""领域事件类型、聚合状态与状态折叠（fold）规则。

事件是唯一事实来源；状态对象只是对事件流的确定性派生，可随时重建。
金额一律以十进制字符串落盘（如 "100000.00"），日期为 ISO ``YYYY-MM-DD``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from .eventstore import Event

# ---------------------------------------------------------------------------
# 事件类型常量
# ---------------------------------------------------------------------------

# 规则
RULE_PUBLISHED = "RulePublished"

# 项目
PROJECT_REGISTERED = "ProjectRegistered"
PROJECT_REGION_CHANGED = "ProjectRegionChanged"
PROJECT_MERGED = "ProjectMerged"
PROJECT_CARRIED_OVER = "ProjectCarriedOver"
PROJECT_FUNDS_RETURNED = "ProjectFundsReturned"

# 申报 / 阶段
APPLICATION_SUBMITTED = "ApplicationSubmitted"
REVIEWERS_ASSIGNED = "ReviewersAssigned"
STAGE_EVIDENCE_SUBMITTED = "StageEvidenceSubmitted"
STAGE_DISPUTE_FLAGGED = "StageDisputeFlagged"
EVIDENCE_ADOPTED = "EvidenceAdopted"
STAGE_BUSINESS_REVIEWED = "StageBusinessReviewed"
STAGE_FINANCE_REVIEWED = "StageFinanceReviewed"
STAGE_LOCKED = "StageLocked"

# 争议
DISPUTE_OPENED = "DisputeOpened"
DISPUTE_PARTY_JOINED = "DisputePartyJoined"
DISPUTE_NEGOTIATION_RECORDED = "DisputeNegotiationRecorded"
DISPUTE_RESOLVED = "DisputeResolved"

# 支付
PAYMENT_CREATED = "PaymentCreated"
PAYMENT_ATTEMPTED = "PaymentAttempted"
PAYMENT_SUCCEEDED = "PaymentSucceeded"
PAYMENT_REFUNDED = "PaymentRefunded"

# 敏感合同访问留痕
SENSITIVE_ACCESS_LOGGED = "SensitiveAccessLogged"

AGG_RULE = "rule"
AGG_PROJECT = "project"
AGG_APPLICATION = "application"
AGG_DISPUTE = "dispute"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def D(value: str | int | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def rule_effective_at(versions: list[dict[str, Any]], on_date: str) -> dict[str, Any] | None:
    """返回 on_date 当日有效（已生效）的最高版本规则；当日换版即适用新版。"""
    candidates = [v for v in versions if v["effective_date"] <= on_date]
    if not candidates:
        return None
    return max(candidates, key=lambda v: v["version"])


# ---------------------------------------------------------------------------
# 规则聚合
# ---------------------------------------------------------------------------


@dataclass
class RuleState:
    code: str = ""
    name: str = ""
    versions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def current(self) -> dict[str, Any] | None:
        if not self.versions:
            return None
        return max(self.versions, key=lambda v: v["version"])

    def effective_at(self, on_date: str) -> dict[str, Any] | None:
        return rule_effective_at(self.versions, on_date)


def _fold_rule(state: RuleState, e: Event) -> RuleState:
    d = e.data
    if e.event_type == RULE_PUBLISHED:
        if not state.code:
            state.code = d["code"]
            state.name = d["name"]
        version = {
            "version": d["version"],
            "name": d.get("name", state.name),
            "effective_date": d["effective_date"],
            "support_categories": d["support_categories"],
            "stage_cap": d["stage_cap"],
            "subsidy_rate": d["subsidy_rate"],
            "required_metrics": d["required_metrics"],
            "metric_dims": d["metric_dims"],
            "traffic_metric_key": d.get("traffic_metric_key", "views"),
            "notes": d.get("notes", ""),
            "published_event_id": e.event_id,
            "published_at": e.occurred_at,
        }
        state.versions = [v for v in state.versions if v["version"] != d["version"]]
        state.versions.append(version)
    return state


# ---------------------------------------------------------------------------
# 项目聚合（含连续事件时间线）
# ---------------------------------------------------------------------------


@dataclass
class ProjectState:
    project_id: str = ""
    name: str = ""
    region: str = ""
    applicant_org_id: str = ""
    targets: list[str] = field(default_factory=list)
    funding_tranches: int = 0
    milestones: list[dict[str, Any]] = field(default_factory=list)
    registered_at: str = ""
    # 连续事件链：地区变更 / 合并 / 结转 / 退回，全部追加，原值保留
    region_history: list[dict[str, Any]] = field(default_factory=list)
    merge_history: list[dict[str, Any]] = field(default_factory=list)
    carryover_history: list[dict[str, Any]] = field(default_factory=list)
    refund_history: list[dict[str, Any]] = field(default_factory=list)
    merged_into: str | None = None

    @property
    def is_active(self) -> bool:
        return self.merged_into is None


def _fold_project(state: ProjectState, e: Event) -> ProjectState:
    d = e.data
    if e.event_type == PROJECT_REGISTERED:
        state.project_id = d["project_id"]
        state.name = d["name"]
        state.region = d["region"]
        state.applicant_org_id = d["applicant_org_id"]
        state.targets = d.get("targets", [])
        state.funding_tranches = d.get("funding_tranches", 1)
        state.milestones = d.get("milestones", [])
        state.registered_at = e.occurred_at
        state.region_history.append(
            {"region": d["region"], "effective_from": d.get("registered_date"), "event_id": e.event_id}
        )
    elif e.event_type == PROJECT_REGION_CHANGED:
        state.region_history.append(
            {
                "from_region": d["from_region"],
                "to_region": d["to_region"],
                "reason": d["reason"],
                "effective_from": d["effective_date"],
                "event_id": e.event_id,
                "at": e.occurred_at,
            }
        )
        state.region = d["to_region"]
    elif e.event_type == PROJECT_MERGED:
        state.merge_history.append(
            {
                "surviving_project_id": d["surviving_project_id"],
                "merged_project_ids": d["merged_project_ids"],
                "reason": d["reason"],
                "effective_date": d["effective_date"],
                "event_id": e.event_id,
                "at": e.occurred_at,
            }
        )
        if d.get("dissolved") and state.project_id in d["merged_project_ids"]:
            state.merged_into = d["surviving_project_id"]
    elif e.event_type == PROJECT_CARRIED_OVER:
        state.carryover_history.append(
            {
                "from_fiscal_year": d["from_fiscal_year"],
                "to_fiscal_year": d["to_fiscal_year"],
                "amount": d["amount"],
                "reason": d["reason"],
                "event_id": e.event_id,
                "at": e.occurred_at,
            }
        )
    elif e.event_type == PROJECT_FUNDS_RETURNED:
        state.refund_history.append(
            {
                "amount": d["amount"],
                "reason": d["reason"],
                "fiscal_year": d["fiscal_year"],
                "payment_id": d.get("payment_id"),
                "event_id": e.event_id,
                "at": e.occurred_at,
            }
        )
    return state


# ---------------------------------------------------------------------------
# 申报聚合（含阶段、证据、双审、支付、敏感访问）
# ---------------------------------------------------------------------------

STAGE_DRAFT = "draft"
STAGE_EVIDENCE_OPEN = "evidence_open"
STAGE_DISPUTED = "disputed"
STAGE_PENDING_REVIEW = "pending_review"
STAGE_BUSINESS_APPROVED = "business_approved"
STAGE_LOCKED = "locked"

PAYMENT_PENDING = "pending"
PAYMENT_FAILED = "failed"
PAYMENT_PAID = "paid"
PAYMENT_REFUNDED_STATUS = "refunded"


@dataclass
class EvidenceState:
    evidence_id: str
    stage_id: str
    kind: str  # contract / service_record / public_feedback
    title: str
    submitted_by_org: str
    fingerprint: str
    achievement: dict[str, Any]
    metrics: dict[str, str]
    sensitive: bool = False
    secret_id: str | None = None
    doc_ref: str = ""
    adopted: bool | None = None  # None=去重未决；True/False=去重决定
    credited_share: str = "1.0"
    dedup_group_id: str | None = None
    dedup_rationale: str = ""
    dispute_id: str | None = None
    submitted_event_id: str = ""


@dataclass
class StageState:
    stage_id: str
    name: str
    commitment: dict[str, Any]
    planned_amount: str
    due_date: str = ""
    status: str = STAGE_DRAFT
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    business_reviewer: str | None = None
    finance_reviewer: str | None = None
    business_review: dict[str, Any] | None = None
    finance_review: dict[str, Any] | None = None
    locked: dict[str, Any] | None = None  # 锁定时的规则与额度快照
    payable_amount: str = "0.00"
    payment_id: str | None = None
    dispute_ids: list[str] = field(default_factory=list)

    def required_kinds_satisfied(self) -> bool:
        adopted_kinds = {
            ev["kind"]
            for ev in self.evidence.values()
            if ev.get("adopted") is True
        }
        return {"contract", "service_record", "public_feedback"} <= adopted_kinds

    def has_open_dispute(self) -> bool:
        return any(ev.get("adopted") is None and ev.get("dispute_id") for ev in self.evidence.values())


def stage_required_kinds_satisfied(stage: dict[str, Any]) -> bool:
    """折叠后阶段为 dict；判断三类采纳证据是否齐备。"""
    adopted_kinds = {
        ev["kind"]
        for ev in stage["evidence"].values()
        if ev.get("adopted") is True
    }
    return {"contract", "service_record", "public_feedback"} <= adopted_kinds


@dataclass
class PaymentState:
    payment_id: str
    stage_id: str
    amount: str
    status: str = PAYMENT_PENDING
    attempts: list[dict[str, Any]] = field(default_factory=list)
    gateway_ref: str = ""
    created_event_id: str = ""
    refunds: list[dict[str, Any]] = field(default_factory=list)

    @property
    def refunded_total(self) -> Decimal:
        return sum((D(r["amount"]) for r in self.refunds), Decimal("0"))


@dataclass
class ApplicationState:
    application_id: str = ""
    project_id: str = ""
    rule_code: str = ""
    rule_version_at_submission: int = 0
    fiscal_year: int = 0
    applicant_org_id: str = ""
    partner_org_ids: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    submitted_at: str = ""
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    payments: dict[str, dict[str, Any]] = field(default_factory=dict)
    access_log: list[dict[str, Any]] = field(default_factory=list)

    def all_orgs(self) -> list[str]:
        return [self.applicant_org_id, *self.partner_org_ids]


def _fold_application(state: ApplicationState, e: Event) -> ApplicationState:
    d = e.data
    if e.event_type == APPLICATION_SUBMITTED:
        state.application_id = d["application_id"]
        state.project_id = d["project_id"]
        state.rule_code = d["rule_code"]
        state.rule_version_at_submission = d["rule_version_at_submission"]
        state.fiscal_year = d["fiscal_year"]
        state.applicant_org_id = d["applicant_org_id"]
        state.partner_org_ids = d.get("partner_org_ids", [])
        state.regions = d.get("regions", [])
        state.submitted_at = e.occurred_at
        for m in d["stages"]:
            state.stages[m["stage_id"]] = {
                "stage_id": m["stage_id"],
                "name": m["name"],
                "commitment": m["commitment"],
                "planned_amount": m["planned_amount"],
                "due_date": m.get("due_date", ""),
                "status": STAGE_EVIDENCE_OPEN,
                "evidence": {},
                "business_reviewer": None,
                "finance_reviewer": None,
                "business_review": None,
                "finance_review": None,
                "locked": None,
                "payable_amount": "0.00",
                "payment_id": None,
                "dispute_ids": [],
            }

    elif e.event_type == REVIEWERS_ASSIGNED:
        st = state.stages[d["stage_id"]]
        st["business_reviewer"] = d["business_reviewer"]
        st["finance_reviewer"] = d["finance_reviewer"]

    elif e.event_type == STAGE_EVIDENCE_SUBMITTED:
        st = state.stages[d["stage_id"]]
        for ev in d["evidence"]:
            st["evidence"][ev["evidence_id"]] = {
                "evidence_id": ev["evidence_id"],
                "stage_id": d["stage_id"],
                "kind": ev["kind"],
                "title": ev["title"],
                "submitted_by_org": ev["submitted_by_org"],
                "fingerprint": ev["fingerprint"],
                "achievement": ev["achievement"],
                "metrics": ev.get("metrics", {}),
                "sensitive": ev.get("sensitive", False),
                "secret_id": ev.get("secret_id"),
                "doc_ref": ev.get("doc_ref", ""),
                "adopted": None,
                "credited_share": "1.0",
                "dedup_group_id": None,
                "dedup_rationale": "",
                "dispute_id": None,
                "submitted_event_id": e.event_id,
            }

    elif e.event_type == STAGE_DISPUTE_FLAGGED:
        st = state.stages[d["stage_id"]]
        st["status"] = STAGE_DISPUTED
        if d["dispute_id"] not in st["dispute_ids"]:
            st["dispute_ids"].append(d["dispute_id"])
        ev = st["evidence"][d["evidence_id"]]
        ev["dispute_id"] = d["dispute_id"]
        # 已自动采纳的证据一旦被争议，回到归属待定，协商期间双方都不能过审
        ev["adopted"] = None

    elif e.event_type == EVIDENCE_ADOPTED:
        st = state.stages[d["stage_id"]]
        ev = st["evidence"][d["evidence_id"]]
        ev["adopted"] = d["adopted"]
        ev["credited_share"] = d["credited_share"]
        ev["dedup_group_id"] = d["dedup_group_id"]
        ev["dedup_rationale"] = d["rationale"]
        # 该阶段所有争议证据均有决定后，状态进入待评审
        if not any(v.get("adopted") is None and v.get("dispute_id") for v in st["evidence"].values()):
            if st["status"] == STAGE_DISPUTED:
                st["status"] = STAGE_PENDING_REVIEW

    elif e.event_type == STAGE_BUSINESS_REVIEWED:
        st = state.stages[d["stage_id"]]
        st["business_review"] = {
            "reviewer": d["reviewer"],
            "decision": d["decision"],
            "comments": d.get("comments", ""),
            "metric_checks": d.get("metric_checks", []),
            "event_id": e.event_id,
            "at": e.occurred_at,
        }
        if d["decision"] == "approved":
            st["status"] = STAGE_BUSINESS_APPROVED

    elif e.event_type == STAGE_FINANCE_REVIEWED:
        st = state.stages[d["stage_id"]]
        st["finance_review"] = {
            "reviewer": d["reviewer"],
            "decision": d["decision"],
            "payable_amount": d.get("payable_amount", "0.00"),
            "comments": d.get("comments", ""),
            "calc": d.get("calc", {}),
            "rule_version": d.get("rule_version"),
            "event_id": e.event_id,
            "at": e.occurred_at,
        }
        if d["decision"] == "approved":
            st["payable_amount"] = d.get("payable_amount", st["payable_amount"])

    elif e.event_type == STAGE_LOCKED:
        st = state.stages[d["stage_id"]]
        st["status"] = STAGE_LOCKED
        st["locked"] = {
            "locked_at": e.occurred_at,
            "rule_code": d["rule_code"],
            "rule_version": d["rule_version"],
            "rule_effective_date": d["rule_effective_date"],
            "rule_snapshot": d["rule_snapshot"],
            "payable_amount": d["payable_amount"],
            "adopted_evidence_ids": d["adopted_evidence_ids"],
            "event_id": e.event_id,
        }
        st["payment_id"] = d.get("payment_id")

    elif e.event_type == PAYMENT_CREATED:
        state.payments[d["payment_id"]] = {
            "payment_id": d["payment_id"],
            "stage_id": d["stage_id"],
            "amount": d["amount"],
            "status": PAYMENT_PENDING,
            "attempts": [],
            "gateway_ref": "",
            "created_event_id": e.event_id,
            "refunds": [],
        }

    elif e.event_type == PAYMENT_ATTEMPTED:
        pay = state.payments[d["payment_id"]]
        pay["attempts"].append(
            {
                "attempt_no": d["attempt_no"],
                "result": d["result"],
                "gateway_ref": d.get("gateway_ref", ""),
                "error": d.get("error", ""),
                "event_id": e.event_id,
                "at": e.occurred_at,
            }
        )
        if d["result"] == "failed":
            pay["status"] = PAYMENT_FAILED
        elif d["result"] == "succeeded":
            pay["status"] = PAYMENT_PAID
            pay["gateway_ref"] = d.get("gateway_ref", pay["gateway_ref"])

    elif e.event_type == PAYMENT_SUCCEEDED:
        # 兼容以独立成功事件记账的网关
        pay = state.payments[d["payment_id"]]
        pay["status"] = PAYMENT_PAID
        pay["gateway_ref"] = d.get("gateway_ref", pay["gateway_ref"])

    elif e.event_type == PAYMENT_REFUNDED:
        pay = state.payments[d["payment_id"]]
        pay["refunds"].append(
            {
                "amount": d["amount"],
                "reason": d["reason"],
                "event_id": e.event_id,
                "at": e.occurred_at,
            }
        )
        pay["status"] = PAYMENT_REFUNDED_STATUS

    elif e.event_type == SENSITIVE_ACCESS_LOGGED:
        state.access_log.append(
            {
                "evidence_id": d["evidence_id"],
                "user_id": d["user_id"],
                "allowed": d["allowed"],
                "reason": d.get("reason", ""),
                "event_id": e.event_id,
                "at": e.occurred_at,
            }
        )
    return state


# ---------------------------------------------------------------------------
# 争议聚合
# ---------------------------------------------------------------------------

DISPUTE_OPEN = "open"
DISPUTE_NEGOTIATING = "negotiating"
DISPUTE_RESOLVED_STATUS = "resolved"


@dataclass
class DisputeState:
    dispute_id: str = ""
    fingerprint: str = ""
    achievement_ref: str = ""
    status: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    negotiations: list[dict[str, Any]] = field(default_factory=list)
    resolution: dict[str, Any] | None = None


def _fold_dispute(state: DisputeState, e: Event) -> DisputeState:
    d = e.data
    if e.event_type == DISPUTE_OPENED:
        state.dispute_id = d["dispute_id"]
        state.fingerprint = d["fingerprint"]
        state.achievement_ref = d["achievement_ref"]
        state.status = DISPUTE_OPEN
        state.candidates = d["candidates"]
    elif e.event_type == DISPUTE_PARTY_JOINED:
        # 后续主体就同一成果申报：并入既有未决争议
        state.status = DISPUTE_OPEN
        for candidate in d["candidates"]:
            if not any(
                c["evidence_id"] == candidate["evidence_id"] for c in state.candidates
            ):
                state.candidates.append(candidate)
    elif e.event_type == DISPUTE_NEGOTIATION_RECORDED:
        state.status = DISPUTE_NEGOTIATING
        state.negotiations.append(
            {
                "proposer_org": d["proposer_org"],
                "proposal": d["proposal"],
                "event_id": e.event_id,
                "at": e.occurred_at,
            }
        )
    elif e.event_type == DISPUTE_RESOLVED:
        state.status = DISPUTE_RESOLVED_STATUS
        state.resolution = {
            "decision": d["decision"],
            "owner_org": d.get("owner_org"),
            "allocations": d["allocations"],
            "rationale": d["rationale"],
            "resolved_by": d.get("resolved_by", ""),
            "event_id": e.event_id,
            "at": e.occurred_at,
        }
    return state


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

_DISPATCH = {
    AGG_RULE: (_fold_rule, RuleState),
    AGG_PROJECT: (_fold_project, ProjectState),
    AGG_APPLICATION: (_fold_application, ApplicationState),
    AGG_DISPUTE: (_fold_dispute, DisputeState),
}


def fold(aggregate_type: str, events: Iterable[Event]) -> Any:
    reducer, factory = _DISPATCH[aggregate_type]
    state = factory()
    for e in sorted(events, key=lambda x: x.version):
        reducer(state, e)
    return state
