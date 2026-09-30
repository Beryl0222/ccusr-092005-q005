"""应用服务层：命令处理 + 只读投影。

所有写操作都表现为“校验 -> 追加事件”，不更新任何可被覆盖的计划表；
资金退回、项目合并、地区变更、跨年结转只产生连续事件，旧数据原样保留。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from . import domain as dm
from .clock import Clock
from .errors import (
    IllegalStateError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from .eventstore import (
    Actor,
    Event,
    EventStore,
    PendingEvent,
    canonical_dumps,
)
from .repository import FingerprintHit, Repository
from .vault import SecretVault

ROLE_AUTHORITY = "authority"            # 省文化产业推进办公室（主管部门）
ROLE_APPLICANT = "applicant"            # 申报主体 / 联合单位经办人
ROLE_BUSINESS = "business_reviewer"     # 业务审核者
ROLE_FINANCE = "finance_reviewer"       # 财务审核者
ROLE_AUDITOR = "auditor"                # 审计人员（只读）

EVIDENCE_KINDS = {"contract", "service_record", "public_feedback"}


# ---------------------------------------------------------------------------
# 端口
# ---------------------------------------------------------------------------


class PaymentGateway(Protocol):
    def charge(self, payment_id: str, amount: str, attempt_no: int) -> "GatewayResult": ...


@dataclass(frozen=True)
class GatewayResult:
    ok: bool
    gateway_ref: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# 主服务
# ---------------------------------------------------------------------------


def achievement_fingerprint(achievement: dict[str, Any]) -> str:
    """成果指纹：对成果身份要素（不含口径各异的传播量指标）做规范哈希。"""
    identity = achievement.get("identity", achievement)
    return "fp:" + hashlib.sha256(canonical_dumps(identity).encode("utf-8")).hexdigest()[:32]


def _require(payload: dict[str, Any], key: str) -> Any:
    if key not in payload or payload[key] in (None, ""):
        raise ValidationError(f"缺少必填字段：{key}")
    return payload[key]


def _dec(value: Any, field_name: str = "金额") -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ValidationError(f"{field_name} 不是合法十进制数：{value!r}") from exc
    if result < 0:
        raise ValidationError(f"{field_name} 不能为负数")
    return result


def _require_role(actor: Actor, *roles: str) -> None:
    if actor.role not in roles:
        raise PermissionDeniedError(f"需要角色 {roles} 之一，当前为 {actor.role}")


class CulturalFundService:
    def __init__(
        self,
        store: EventStore,
        clock: Clock,
        repo: Repository | None = None,
        vault: SecretVault | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.repo = repo or Repository(store)
        self.vault = vault

    # ===============================================================
    # 规则发布与换版
    # ===============================================================

    def publish_rule(
        self,
        actor: Actor,
        *,
        code: str,
        name: str,
        version: int,
        effective_date: str,
        support_categories: list[str],
        stage_cap: str,
        subsidy_rate: str,
        required_metrics: list[str],
        metric_dims: list[dict[str, Any]],
        traffic_metric_key: str = "views",
        notes: str = "",
    ) -> list[Event]:
        """发布规则版本。版本号必须递增；生效日期之后才适用，旧版永不修改。"""
        _require_role(actor, ROLE_AUTHORITY)
        cap = _dec(stage_cap, "stage_cap")
        rate = _dec(subsidy_rate, "subsidy_rate")
        if not (Decimal("0") <= rate <= Decimal("1")):
            raise ValidationError("subsidy_rate 必须在 0 与 1 之间")
        if not isinstance(version, int) or version < 1:
            raise ValidationError("规则版本必须为正整数")
        _date(effective_date)

        rule = self.repo.rule(code)
        if rule is not None:
            if any(v["version"] == version for v in rule.versions):
                raise ValidationError(f"规则 {code} 版本 {version} 已存在；换版请使用新版本号")
            max_version = max(v["version"] for v in rule.versions)
            if version <= max_version:
                raise ValidationError(f"新版本号必须大于 {max_version}")

        data = {
            "code": code,
            "name": name,
            "version": version,
            "effective_date": effective_date,
            "support_categories": support_categories,
            "stage_cap": str(cap),
            "subsidy_rate": str(rate),
            "required_metrics": required_metrics,
            "metric_dims": metric_dims,
            "traffic_metric_key": traffic_metric_key,
            "notes": notes,
        }
        return self.store.append(
            dm.AGG_RULE,
            code,
            [PendingEvent(dm.RULE_PUBLISHED, data)],
            actor,
            self.clock.now().isoformat(),
            expected_version=self.store.stream_version(dm.AGG_RULE, code),
        )

    def effective_rule(self, code: str, on_date: str | None = None) -> dict[str, Any]:
        rule = self.repo.rule(code)
        if rule is None:
            raise NotFoundError(f"规则 {code} 不存在")
        snap = rule.effective_at(on_date or self.clock.today())
        if snap is None:
            raise IllegalStateError(f"规则 {code} 在 {on_date or self.clock.today()} 尚未生效")
        return snap

    # ===============================================================
    # 项目登记与连续事件
    # ===============================================================

    def register_project(
        self,
        actor: Actor,
        *,
        project_id: str,
        name: str,
        region: str,
        applicant_org_id: str,
        targets: list[str] | None = None,
        funding_tranches: int = 1,
        registered_date: str | None = None,
        milestones: list[dict[str, Any]] | None = None,
    ) -> list[Event]:
        _require_role(actor, ROLE_AUTHORITY)
        if self.repo.project(project_id) is not None:
            raise ValidationError(f"项目 {project_id} 已登记")
        data = {
            "project_id": project_id,
            "name": name,
            "region": region,
            "applicant_org_id": applicant_org_id,
            "targets": targets or [],
            "funding_tranches": funding_tranches,
            "registered_date": registered_date or self.clock.today(),
            "milestones": milestones or [],
        }
        return self.store.append(
            dm.AGG_PROJECT,
            project_id,
            [PendingEvent(dm.PROJECT_REGISTERED, data)],
            actor,
            self.clock.now().isoformat(),
            expected_version=0,
        )

    def change_project_region(
        self, actor: Actor, *, project_id: str, to_region: str, reason: str, effective_date: str
    ) -> list[Event]:
        """地区变更：只追加事件，原公示地区与口径在 region_history 中保留。"""
        _require_role(actor, ROLE_AUTHORITY)
        project = self._project(project_id)
        _date(effective_date)
        if to_region == project.region:
            raise ValidationError("新地区与当前地区相同")
        data = {
            "from_region": project.region,
            "to_region": to_region,
            "reason": reason,
            "effective_date": effective_date,
        }
        return self._append_project(project_id, dm.PROJECT_REGION_CHANGED, data, actor)

    def merge_projects(
        self,
        actor: Actor,
        *,
        surviving_project_id: str,
        merged_project_ids: list[str],
        reason: str,
        effective_date: str,
    ) -> list[Event]:
        """项目合并：被合并项目标记 merged_into，事件链保留双方历史。"""
        _require_role(actor, ROLE_AUTHORITY)
        surviving = self._project(surviving_project_id)
        if not merged_project_ids:
            raise ValidationError("merged_project_ids 不能为空")
        for pid in merged_project_ids:
            if pid == surviving_project_id:
                raise ValidationError("存续项目不能同时是被合并项目")
            self._project(pid)
        _date(effective_date)
        atoms = [
            EventStore.WriteAtom(
                dm.AGG_PROJECT,
                surviving_project_id,
                [
                    PendingEvent(
                        dm.PROJECT_MERGED,
                        {
                            "surviving_project_id": surviving_project_id,
                            "merged_project_ids": merged_project_ids,
                            "reason": reason,
                            "effective_date": effective_date,
                            "dissolved": False,
                        },
                    )
                ],
                expected_version=self.store.stream_version(dm.AGG_PROJECT, surviving_project_id),
            )
        ]
        for pid in merged_project_ids:
            atoms.append(
                EventStore.WriteAtom(
                    dm.AGG_PROJECT,
                    pid,
                    [
                        PendingEvent(
                            dm.PROJECT_MERGED,
                            {
                                "surviving_project_id": surviving_project_id,
                                "merged_project_ids": merged_project_ids,
                                "reason": reason,
                                "effective_date": effective_date,
                                "dissolved": True,
                            },
                        )
                    ],
                    expected_version=self.store.stream_version(dm.AGG_PROJECT, pid),
                )
            )
        return self.store.commit(atoms, actor, self.clock.now().isoformat())

    def carry_over(
        self,
        actor: Actor,
        *,
        project_id: str,
        from_fiscal_year: int,
        to_fiscal_year: int,
        amount: str,
        reason: str,
    ) -> list[Event]:
        """跨年结转：新增连续事件，不动旧年度拨付记录。"""
        _require_role(actor, ROLE_AUTHORITY)
        self._project(project_id)
        amt = _dec(amount, "结转金额")
        if to_fiscal_year <= from_fiscal_year:
            raise ValidationError("结转目标年度必须晚于来源年度")
        data = {
            "from_fiscal_year": from_fiscal_year,
            "to_fiscal_year": to_fiscal_year,
            "amount": str(amt),
            "reason": reason,
        }
        return self._append_project(project_id, dm.PROJECT_CARRIED_OVER, data, actor)

    def return_funds(
        self,
        actor: Actor,
        *,
        project_id: str,
        amount: str,
        reason: str,
        fiscal_year: int,
        payment_id: str | None = None,
    ) -> list[Event]:
        """资金退回：项目流记连续事件；若能定位支付，支付流同时记退款。"""
        _require_role(actor, ROLE_AUTHORITY, ROLE_FINANCE)
        project = self._project(project_id)
        amt = _dec(amount, "退回金额")
        if amt <= 0:
            raise ValidationError("退回金额必须大于 0")
        project_atom = EventStore.WriteAtom(
            dm.AGG_PROJECT,
            project_id,
            [
                PendingEvent(
                    dm.PROJECT_FUNDS_RETURNED,
                    {
                        "amount": str(amt),
                        "reason": reason,
                        "fiscal_year": fiscal_year,
                        "payment_id": payment_id,
                    },
                )
            ],
            expected_version=self.store.stream_version(dm.AGG_PROJECT, project_id),
        )
        atoms = [project_atom]
        if payment_id:
            located = self.repo.payment(payment_id)
            if located is None:
                raise NotFoundError(f"支付单 {payment_id} 不存在")
            app, pay = located
            if Decimal(str(pay["amount"])) < amt:
                raise ValidationError("退款金额不能超过支付金额")
            atoms.append(
                EventStore.WriteAtom(
                    dm.AGG_APPLICATION,
                    app.application_id,
                    [
                        PendingEvent(
                            dm.PAYMENT_REFUNDED,
                            {
                                "payment_id": payment_id,
                                "amount": str(amt),
                                "reason": reason,
                            },
                        )
                    ],
                    expected_version=self.store.stream_version(dm.AGG_APPLICATION, app.application_id),
                )
            )
        return self.store.commit(atoms, actor, self.clock.now().isoformat())

    def _append_project(self, project_id: str, event_type: str, data: dict[str, Any], actor: Actor):
        return self.store.append(
            dm.AGG_PROJECT,
            project_id,
            [PendingEvent(event_type, data)],
            actor,
            self.clock.now().isoformat(),
            expected_version=self.store.stream_version(dm.AGG_PROJECT, project_id),
        )

    def _project(self, project_id: str) -> dm.ProjectState:
        project = self.repo.project(project_id)
        if project is None:
            raise NotFoundError(f"项目 {project_id} 不存在")
        return project

    # ===============================================================
    # 申报与阶段承诺
    # ===============================================================

    def submit_application(
        self,
        actor: Actor,
        *,
        application_id: str,
        project_id: str,
        fiscal_year: int,
        applicant_org_id: str | None = None,
        partner_org_ids: list[str] | None = None,
        regions: list[str] | None = None,
        stages: list[dict[str, Any]],
    ) -> list[Event]:
        """申报主体围绕阶段承诺提交申报；提交时钉住当时有效规则版本。"""
        _require_role(actor, ROLE_APPLICANT, ROLE_AUTHORITY)
        project = self._project(project_id)
        if actor.role == ROLE_APPLICANT:
            org_id = actor.org_id
            if not org_id:
                raise ValidationError("申报经办人缺少所属单位 org_id")
        else:
            org_id = applicant_org_id or project.applicant_org_id
        partners = partner_org_ids or []
        if org_id in partners:
            raise ValidationError("联合单位不能与申报主体重复")
        if self.repo.application(application_id) is not None:
            raise ValidationError(f"申报 {application_id} 已存在")

        rule = self._rule_for_project(project)
        # 提交钉版：取“今天”有效版本
        pinned = rule.effective_at(self.clock.today())
        if pinned is None:
            raise IllegalStateError("申报当日没有已生效的扶持规则")

        normalized_stages: list[dict[str, Any]] = []
        seen: set[str] = set()
        total = Decimal("0")
        for m in stages:
            sid = _require(m, "stage_id")
            if sid in seen:
                raise ValidationError(f"阶段标识重复：{sid}")
            seen.add(sid)
            planned = _dec(_require(m, "planned_amount"), "阶段拟拨金额")
            total += planned
            normalized_stages.append(
                {
                    "stage_id": sid,
                    "name": _require(m, "name"),
                    "planned_amount": str(planned),
                    "due_date": m.get("due_date", ""),
                    "commitment": _require(m, "commitment"),
                }
            )

        data = {
            "application_id": application_id,
            "project_id": project_id,
            "rule_code": rule.code,
            "rule_version_at_submission": pinned["version"],
            "fiscal_year": fiscal_year,
            "applicant_org_id": org_id,
            "partner_org_ids": partners,
            "regions": regions or [project.region],
            "stages": normalized_stages,
        }
        return self.store.append(
            dm.AGG_APPLICATION,
            application_id,
            [PendingEvent(dm.APPLICATION_SUBMITTED, data)],
            actor,
            self.clock.now().isoformat(),
            expected_version=0,
        )

    def _rule_for_project(self, project: dm.ProjectState) -> dm.RuleState:
        rules = self.repo.list_rules()
        if not rules:
            raise IllegalStateError("尚未发布任何扶持规则")
        # 当前系统内单一规则线时直接取之；多规则线可在项目上记录 code 后扩展
        return rules[0]

    def assign_reviewers(
        self,
        actor: Actor,
        *,
        application_id: str,
        stage_id: str,
        business_reviewer: str,
        finance_reviewer: str,
    ) -> list[Event]:
        _require_role(actor, ROLE_AUTHORITY)
        app, stage = self._stage(application_id, stage_id)
        if business_reviewer == finance_reviewer:
            raise ValidationError("业务审核者与财务审核者不能为同一人（职责分离）")
        data = {
            "stage_id": stage_id,
            "business_reviewer": business_reviewer,
            "finance_reviewer": finance_reviewer,
        }
        return self._append_app(application_id, dm.REVIEWERS_ASSIGNED, actor, data)

    # ===============================================================
    # 证据提交 + 成果指纹去重 + 归属协商
    # ===============================================================

    def submit_stage_evidence(
        self,
        actor: Actor,
        *,
        application_id: str,
        stage_id: str,
        evidence: list[dict[str, Any]],
    ) -> list[Event]:
        """提交合同/服务记录/公众反馈；跨主体同成果指纹自动转归属协商。"""
        _require_role(actor, ROLE_APPLICANT, ROLE_AUTHORITY)
        app, stage = self._stage(application_id, stage_id)
        if actor.role == ROLE_APPLICANT and actor.org_id not in app.all_orgs():
            raise PermissionDeniedError("只有申报主体或联合单位可提交本申报证据")
        if stage["status"] == dm.STAGE_LOCKED:
            raise IllegalStateError("阶段已锁定，证据不可变更")

        normalized: list[dict[str, Any]] = []
        for item in evidence:
            kind = _require(item, "kind")
            if kind not in EVIDENCE_KINDS:
                raise ValidationError(f"证据类型必须是 {sorted(EVIDENCE_KINDS)}")
            org = item.get("submitted_by_org") or actor.org_id
            if actor.role == ROLE_APPLICANT and org != actor.org_id:
                raise PermissionDeniedError("不能代其他单位提交证据")
            if org not in app.all_orgs():
                raise ValidationError(f"单位 {org} 不属于该申报的主体或联合单位")
            achievement = _require(item, "achievement")
            fp = item.get("fingerprint") or achievement_fingerprint(achievement)
            metrics = {k: str(_dec(v, f"指标 {k}")) for k, v in (item.get("metrics") or {}).items()}
            sensitive = bool(item.get("sensitive", False))
            if sensitive:
                if kind != "contract":
                    raise ValidationError("仅合同证据可标记敏感")
                if self.vault is None:
                    raise IllegalStateError("未配置密件保管库，不能接收敏感合同")
            normalized.append(
                {
                    "evidence_id": _require(item, "evidence_id"),
                    "kind": kind,
                    "title": _require(item, "title"),
                    "submitted_by_org": org,
                    "fingerprint": fp,
                    "achievement": achievement,
                    "metrics": metrics,
                    "sensitive": sensitive,
                    "secret_id": None,  # 查重通过后再入库密文
                    "_secret_plaintext": item.get("secret", {"title": item.get("title", "")}) if sensitive else None,
                    "doc_ref": item.get("doc_ref", ""),
                }
            )

        # 同批次内部重复指纹
        fps = [n["fingerprint"] for n in normalized]
        if len(fps) != len(set(fps)):
            raise ValidationError("同一次提交中出现重复成果指纹")

        # 成果指纹查重（跨全部申报、全部阶段）：
        # - 命中已锁定成果：锁定即终审归属，拒绝再次申报（保护已公示口径）；
        # - 同一主体重复申报同一成果：直接拒绝；
        # - 不同主体（含联合单位之间）：进入归属协商。
        prior_by_fp: dict[str, list[FingerprintHit]] = {}
        for n in normalized:
            hits = self.repo.fingerprint_hits(n["fingerprint"])
            if any(h.locked for h in hits):
                raise IllegalStateError(
                    f"成果 {n['fingerprint']} 已随阶段锁定并拨付，不能再次申报；如有异议须走申诉程序"
                )
            same_org = [h for h in hits if h.submitted_by_org == n["submitted_by_org"]]
            if same_org:
                h = same_org[0]
                raise ValidationError(
                    f"成果 {n['fingerprint']} 已由同一单位在申报 {h.application_id}/{h.stage_id} 申报，"
                    "不得重复申报同一成果"
                )
            prior_by_fp[n["fingerprint"]] = hits

        # 所有校验与查重已通过，敏感密文此时入库（事件只引用 secret_id）
        for n in normalized:
            if n["sensitive"]:
                n["secret_id"] = self.vault.store(
                    n.pop("_secret_plaintext"),
                    title=n["title"],
                    application_id=application_id,
                    evidence_id=n["evidence_id"],
                    created_by=actor.user_id,
                )
            else:
                n.pop("_secret_plaintext", None)

        evidence_payload = [
            {k: v for k, v in n.items() if not k.startswith("_")} for n in normalized
        ]
        pending_events: list[PendingEvent] = [
            PendingEvent(
                dm.STAGE_EVIDENCE_SUBMITTED,
                {"stage_id": stage_id, "evidence": evidence_payload},
            )
        ]
        atoms = [
            EventStore.WriteAtom(
                dm.AGG_APPLICATION,
                application_id,
                pending_events,
                expected_version=self.store.stream_version(dm.AGG_APPLICATION, application_id),
            )
        ]

        # 每个冲突指纹：并入既有未决争议，或新开争议
        new_disputes: list[tuple[str, str, dict[str, Any]]] = []
        joined_disputes: dict[str, list[dict[str, Any]]] = {}
        for n in normalized:
            hits = prior_by_fp[n["fingerprint"]]
            conflicting = [h for h in hits if h.submitted_by_org != n["submitted_by_org"]]
            if not conflicting:
                # 无冲突：自动采纳，参与后续审核
                pending_events.append(
                    PendingEvent(
                        dm.EVIDENCE_ADOPTED,
                        {
                            "stage_id": stage_id,
                            "evidence_id": n["evidence_id"],
                            "adopted": True,
                            "credited_share": "1.0",
                            "dedup_group_id": None,
                            "rationale": "未发现跨主体重复，自动采纳",
                        },
                    )
                )
                continue

            new_candidate = {
                "application_id": application_id,
                "stage_id": stage_id,
                "evidence_id": n["evidence_id"],
                "org_id": n["submitted_by_org"],
                "locked": False,
            }
            existing_candidates = [
                {
                    "application_id": h.application_id,
                    "stage_id": h.stage_id,
                    "evidence_id": h.evidence_id,
                    "org_id": h.submitted_by_org,
                    "locked": h.locked,
                }
                for h in conflicting
            ]

            # 查找该指纹是否已有未决争议（含本次批次刚规划的新争议）
            open_dispute_id = self._find_open_dispute(n["fingerprint"])
            if open_dispute_id is None:
                for d_id, fp, _data in new_disputes:
                    if fp == n["fingerprint"]:
                        open_dispute_id = d_id
                        break

            if open_dispute_id is not None:
                # 新提交侧挂到既有争议
                pending_events.append(
                    PendingEvent(
                        dm.STAGE_DISPUTE_FLAGGED,
                        {
                            "stage_id": stage_id,
                            "evidence_id": n["evidence_id"],
                            "dispute_id": open_dispute_id,
                        },
                    )
                )
                joined_disputes.setdefault(open_dispute_id, []).append(new_candidate)
                # 既有持证侧若尚未挂标记（例如争议由更早批次产生时已挂，跳过）
                for h in conflicting:
                    atom = self._atom_for_stage_flag(
                        h.application_id, h.stage_id, h.evidence_id, open_dispute_id
                    )
                    if atom is not None:
                        atoms.append(atom)
                continue

            dispute_id = f"disp-{n['fingerprint'][3:13]}-{_short_uid()}"
            new_disputes.append(
                (
                    dispute_id,
                    n["fingerprint"],
                    {
                        "dispute_id": dispute_id,
                        "fingerprint": n["fingerprint"],
                        "achievement_ref": n["achievement"].get("identity", n["achievement"]),
                        "candidates": existing_candidates + [new_candidate],
                    },
                )
            )
            pending_events.append(
                PendingEvent(
                    dm.STAGE_DISPUTE_FLAGGED,
                    {"stage_id": stage_id, "evidence_id": n["evidence_id"], "dispute_id": dispute_id},
                )
            )
            for h in conflicting:
                atom = self._atom_for_stage_flag(
                    h.application_id, h.stage_id, h.evidence_id, dispute_id
                )
                if atom is not None:
                    atoms.append(atom)

        for dispute_id, _fp, data in new_disputes:
            atoms.append(
                EventStore.WriteAtom(
                    dm.AGG_DISPUTE,
                    dispute_id,
                    [PendingEvent(dm.DISPUTE_OPENED, data)],
                    expected_version=0,
                )
            )
        for dispute_id, new_candidates in joined_disputes.items():
            atoms.append(
                EventStore.WriteAtom(
                    dm.AGG_DISPUTE,
                    dispute_id,
                    [
                        PendingEvent(
                            dm.DISPUTE_PARTY_JOINED,
                            {"candidates": new_candidates},
                        )
                    ],
                    expected_version=self.store.stream_version(dm.AGG_DISPUTE, dispute_id),
                )
            )

        return self.store.commit(atoms, actor, self.clock.now().isoformat())

    def _find_open_dispute(self, fingerprint: str) -> str | None:
        for dispute in self.repo.list_disputes():
            if dispute.fingerprint == fingerprint and dispute.status != dm.DISPUTE_RESOLVED_STATUS:
                return dispute.dispute_id
        return None

    def _atom_for_stage_flag(
        self, application_id: str, stage_id: str, evidence_id: str, dispute_id: str
    ):
        app = self.repo.application(application_id)
        stage = app.stages.get(stage_id)
        if stage is None or evidence_id not in stage["evidence"]:
            return None
        ev = stage["evidence"][evidence_id]
        # 已挂同一争议标记则跳过；旧争议已决后产生新争议时允许重挂
        if ev.get("dispute_id") == dispute_id:
            return None
        return EventStore.WriteAtom(
            dm.AGG_APPLICATION,
            application_id,
            [
                PendingEvent(
                    dm.STAGE_DISPUTE_FLAGGED,
                    {"stage_id": stage_id, "evidence_id": evidence_id, "dispute_id": dispute_id},
                )
            ],
            expected_version=self.store.stream_version(dm.AGG_APPLICATION, application_id),
        )

    def record_negotiation(
        self, actor: Actor, *, dispute_id: str, proposal: str
    ) -> list[Event]:
        _require_role(actor, ROLE_APPLICANT, ROLE_AUTHORITY)
        dispute = self._dispute(dispute_id)
        if dispute.status == dm.DISPUTE_RESOLVED_STATUS:
            raise IllegalStateError("争议已裁决，不能继续协商")
        if actor.role == ROLE_APPLICANT and actor.org_id not in {
            c["org_id"] for c in dispute.candidates
        }:
            raise PermissionDeniedError("只有争议候选单位可记录协商意见")
        data = {"proposer_org": actor.org_id or actor.user_id, "proposal": proposal}
        return self.store.append(
            dm.AGG_DISPUTE,
            dispute_id,
            [PendingEvent(dm.DISPUTE_NEGOTIATION_RECORDED, data)],
            actor,
            self.clock.now().isoformat(),
            expected_version=self.store.stream_version(dm.AGG_DISPUTE, dispute_id),
        )

    def resolve_dispute(
        self,
        actor: Actor,
        *,
        dispute_id: str,
        decision: str,  # single_owner / shared
        allocations: list[dict[str, Any]],
        rationale: str,
    ) -> list[Event]:
        """归属裁决：对每条证据落 EvidenceAdopted；同成果的 credited_share 之和为 1。"""
        _require_role(actor, ROLE_AUTHORITY)
        dispute = self._dispute(dispute_id)
        if dispute.status == dm.DISPUTE_RESOLVED_STATUS:
            raise IllegalStateError("争议已裁决")
        if decision not in {"single_owner", "shared"}:
            raise ValidationError("decision 必须为 single_owner 或 shared")
        if not allocations:
            raise ValidationError("allocations 不能为空")

        candidate_keys = {
            (c["application_id"], c["stage_id"], c["evidence_id"], c["org_id"])
            for c in dispute.candidates
        }
        total_share = Decimal("0")
        normalized: list[dict[str, Any]] = []
        for a in allocations:
            key = (
                _require(a, "application_id"),
                _require(a, "stage_id"),
                _require(a, "evidence_id"),
                _require(a, "org_id"),
            )
            if key not in candidate_keys:
                raise ValidationError(f"裁决对象不在候选清单：{key}")
            adopted = bool(a["adopted"])
            share = _dec(a.get("credited_share", "1" if adopted else "0"), "credited_share")
            if adopted and not (Decimal("0") < share <= Decimal("1")):
                raise ValidationError("采纳证据的分成必须在 (0,1] 之间")
            if not adopted and share != 0:
                raise ValidationError("不予采纳的证据分成必须为 0")
            total_share += share
            normalized.append(
                {
                    "application_id": key[0],
                    "stage_id": key[1],
                    "evidence_id": key[2],
                    "org_id": key[3],
                    "adopted": adopted,
                    "credited_share": str(share),
                }
            )
        if total_share != Decimal("1.0"):
            raise ValidationError(f"同一成果采纳分成之和必须为 1，当前为 {total_share}")
        alloc_keys = {(n["application_id"], n["stage_id"], n["evidence_id"]) for n in normalized}
        missing = [
            {
                "application_id": c["application_id"],
                "stage_id": c["stage_id"],
                "evidence_id": c["evidence_id"],
            }
            for c in dispute.candidates
            if (c["application_id"], c["stage_id"], c["evidence_id"]) not in alloc_keys
        ]
        if missing:
            raise ValidationError("裁决必须覆盖争议的全部候选证据，缺少：" + str(missing))

        dedup_group_id = f"dedup-{dispute_id}"
        atoms: list[EventStore.WriteAtom] = []
        per_app: dict[str, list[PendingEvent]] = {}
        for n in normalized:
            per_app.setdefault(n["application_id"], []).append(
                PendingEvent(
                    dm.EVIDENCE_ADOPTED,
                    {
                        "stage_id": n["stage_id"],
                        "evidence_id": n["evidence_id"],
                        "adopted": n["adopted"],
                        "credited_share": n["credited_share"],
                        "dedup_group_id": dedup_group_id,
                        "rationale": rationale,
                    },
                )
            )
        for application_id, events in per_app.items():
            atoms.append(
                EventStore.WriteAtom(
                    dm.AGG_APPLICATION,
                    application_id,
                    events,
                    expected_version=self.store.stream_version(dm.AGG_APPLICATION, application_id),
                )
            )
        atoms.append(
            EventStore.WriteAtom(
                dm.AGG_DISPUTE,
                dispute_id,
                [
                    PendingEvent(
                        dm.DISPUTE_RESOLVED,
                        {
                            "decision": decision,
                            "owner_org": next(
                                (n["org_id"] for n in normalized if n["adopted"]), None
                            ),
                            "allocations": normalized,
                            "rationale": rationale,
                            "resolved_by": actor.user_id,
                        },
                    )
                ],
                expected_version=self.store.stream_version(dm.AGG_DISPUTE, dispute_id),
            )
        )
        return self.store.commit(atoms, actor, self.clock.now().isoformat())

    # ===============================================================
    # 业务 / 财务双审 -> 锁定 -> 可支付额度
    # ===============================================================

    def business_review(
        self,
        actor: Actor,
        *,
        application_id: str,
        stage_id: str,
        decision: str,
        comments: str = "",
    ) -> list[Event]:
        """业务审核：核验三类证据齐备、成果指标达到阶段承诺，且无未决归属争议。"""
        _require_role(actor, ROLE_BUSINESS)
        app, stage = self._stage(application_id, stage_id)
        self._assert_assigned_reviewer(actor, stage, "business_reviewer")
        if decision not in {"approved", "rejected"}:
            raise ValidationError("decision 必须为 approved/rejected")
        if stage["status"] == dm.STAGE_LOCKED:
            raise IllegalStateError("阶段已锁定")
        if decision == "approved":
            if self._has_open_dispute(stage):
                raise IllegalStateError("尚有成果归属争议未决，不能进入业务审核")
            if not dm.stage_required_kinds_satisfied(stage):
                raise IllegalStateError("合同、服务记录、公众反馈三类采纳证据不齐备")

        metric_checks = self._check_commitment_metrics(app, stage)
        if decision == "approved" and any(c["status"] == "short" for c in metric_checks):
            raise IllegalStateError("采纳证据的成效指标未达到阶段承诺：" + ", ".join(
                c["key"] for c in metric_checks if c["status"] == "short"
            ))

        data = {
            "stage_id": stage_id,
            "reviewer": actor.user_id,
            "decision": decision,
            "comments": comments,
            "metric_checks": metric_checks,
        }
        return self._append_app(application_id, dm.STAGE_BUSINESS_REVIEWED, actor, data)

    def finance_review(
        self,
        actor: Actor,
        *,
        application_id: str,
        stage_id: str,
        decision: str,
        comments: str = "",
    ) -> list[Event]:
        """财务审核：业务审过之后，按审核当日有效规则测算可支付额度。"""
        _require_role(actor, ROLE_FINANCE)
        app, stage = self._stage(application_id, stage_id)
        self._assert_assigned_reviewer(actor, stage, "finance_reviewer")
        if stage["status"] == dm.STAGE_LOCKED:
            raise IllegalStateError("阶段已锁定")
        br = stage["business_review"]
        if br is None or br["decision"] != "approved":
            raise IllegalStateError("业务审核未通过，财务审核不能进行")
        if decision not in {"approved", "rejected"}:
            raise ValidationError("decision 必须为 approved/rejected")

        rule = self.repo.rule(app.rule_code)
        snap = rule.effective_at(self.clock.today())  # 换版只影响尚未锁定的阶段
        calc: dict[str, Any] = {}
        payable = Decimal("0")
        if decision == "approved":
            planned = Decimal(stage["planned_amount"])
            adopted = [ev for ev in stage["evidence"].values() if ev["adopted"] is True]
            # 去重折算：仅参与归属去重组（争议裁决）的成果按其平均归属分成调减，
            # 不连累无争议的合同/反馈；无去重成果时系数为 1。
            deduped = [ev for ev in adopted if ev.get("dedup_group_id")]
            if deduped:
                dedup_factor = (
                    sum((Decimal(ev["credited_share"]) for ev in deduped), Decimal("0"))
                    / Decimal(len(deduped))
                ).quantize(Decimal("0.0001"))
            else:
                dedup_factor = Decimal("1")
            base = planned * Decimal(snap["subsidy_rate"])
            payable = (min(base, Decimal(snap["stage_cap"])) * dedup_factor).quantize(Decimal("0.01"))
            calc = {
                "planned_amount": str(planned),
                "subsidy_rate": snap["subsidy_rate"],
                "after_rate": str(base.quantize(Decimal("0.01"))),
                "stage_cap": snap["stage_cap"],
                "dedup_factor": str(dedup_factor),
                "dedup_rule": "参与归属去重组的采纳证据 credited_share 平均值；无去重成果时为 1",
                "deduped_evidence_ids": [ev["evidence_id"] for ev in deduped],
                "payable_amount": str(payable),
            }
        data = {
            "stage_id": stage_id,
            "reviewer": actor.user_id,
            "decision": decision,
            "payable_amount": str(payable),
            "comments": comments,
            "calc": calc,
            "rule_version": snap["version"],
        }
        return self._append_app(application_id, dm.STAGE_FINANCE_REVIEWED, actor, data)

    def lock_stage(
        self, actor: Actor, *, application_id: str, stage_id: str, payment_id: str
    ) -> list[Event]:
        """双审均通过后锁定阶段并生成支付单；锁定快照钉住当时有效规则全文。"""
        _require_role(actor, ROLE_FINANCE, ROLE_AUTHORITY)
        app, stage = self._stage(application_id, stage_id)
        br, fr = stage["business_review"], stage["finance_review"]
        if not br or br["decision"] != "approved":
            raise IllegalStateError("业务审核未通过")
        if not fr or fr["decision"] != "approved":
            raise IllegalStateError("财务审核未通过")
        if stage["status"] == dm.STAGE_LOCKED:
            raise IllegalStateError("阶段已锁定")
        if self.repo.payment(payment_id) is not None:
            raise ValidationError(f"支付单 {payment_id} 已存在")

        rule = self.repo.rule(app.rule_code)
        snap = rule.effective_at(self.clock.today())
        adopted_ids = [
            eid for eid, ev in stage["evidence"].items() if ev["adopted"] is True
        ]
        amount = stage["payable_amount"]
        events = [
            PendingEvent(
                dm.STAGE_LOCKED,
                {
                    "stage_id": stage_id,
                    "rule_code": rule.code,
                    "rule_version": snap["version"],
                    "rule_effective_date": snap["effective_date"],
                    "rule_snapshot": {
                        k: snap[k]
                        for k in (
                            "support_categories",
                            "stage_cap",
                            "subsidy_rate",
                            "required_metrics",
                            "metric_dims",
                            "traffic_metric_key",
                        )
                    },
                    "payable_amount": amount,
                    "adopted_evidence_ids": adopted_ids,
                    "payment_id": payment_id,
                },
            ),
            PendingEvent(
                dm.PAYMENT_CREATED,
                {"payment_id": payment_id, "stage_id": stage_id, "amount": amount},
            ),
        ]
        return self._append_app(application_id, None, actor, events=events)

    def attempt_payment(
        self,
        actor: Actor,
        *,
        payment_id: str,
        gateway: PaymentGateway,
        idempotency_key: str,
    ) -> list[Event]:
        """发起支付；失败可重试。同一 idempotency_key 重放不重复扣款。

        幂等检查先于终态判断：即使支付已成功，客户端因超时用同一键重放，
        也只回放原事件，绝不再次调用网关。
        """
        _require_role(actor, ROLE_FINANCE, ROLE_AUTHORITY)
        located = self.repo.payment(payment_id)
        if located is None:
            raise NotFoundError(f"支付单 {payment_id} 不存在")
        app, pay = located

        existing = self._find_idempotent(idempotency_key, payment_id)
        if existing is not None:
            return [existing]

        if pay["status"] in {dm.PAYMENT_PAID, dm.PAYMENT_REFUNDED_STATUS}:
            raise IllegalStateError(
                f"支付单状态为 {pay['status']}，不能再次发起（重试须复用原幂等键）"
            )

        attempt_no = len(pay["attempts"]) + 1
        result = gateway.charge(payment_id, pay["amount"], attempt_no)

        data = {
            "payment_id": payment_id,
            "attempt_no": attempt_no,
            "result": "succeeded" if result.ok else "failed",
            "gateway_ref": result.gateway_ref,
            "error": result.error,
        }
        return self.store.append(
            dm.AGG_APPLICATION,
            app.application_id,
            [PendingEvent(dm.PAYMENT_ATTEMPTED, data, idempotency_key=idempotency_key)],
            actor,
            self.clock.now().isoformat(),
            expected_version=self.store.stream_version(dm.AGG_APPLICATION, app.application_id),
        )

    def _find_idempotent(self, key: str, payment_id: str | None = None) -> Event | None:
        for e in self.store.all_events():
            if e.idempotency_key == key:
                if payment_id is not None and e.data.get("payment_id") != payment_id:
                    raise ValidationError(
                        f"幂等键 {key} 已用于支付单 {e.data.get('payment_id')}，不能跨支付单复用"
                    )
                return e
        return None

    # ===============================================================
    # 敏感合同阅取
    # ===============================================================

    def read_sensitive_contract(
        self, actor: Actor, *, application_id: str, evidence_id: str
    ) -> dict[str, Any]:
        """只有该阶段实际指派的业务/财务审核者可读正文；每次访问（含拒绝）留痕。"""
        app, stage = self._stage(application_id, None, evidence_id=evidence_id)
        ev = stage["evidence"][evidence_id]
        if not ev["sensitive"] or not ev.get("secret_id"):
            raise ValidationError("该证据不是敏感合同")

        allowed = (
            (actor.role == ROLE_BUSINESS and stage["business_reviewer"] == actor.user_id)
            or (actor.role == ROLE_FINANCE and stage["finance_reviewer"] == actor.user_id)
        )
        self.store.append(
            dm.AGG_APPLICATION,
            application_id,
            [
                PendingEvent(
                    dm.SENSITIVE_ACCESS_LOGGED,
                    {
                        "evidence_id": evidence_id,
                        "user_id": actor.user_id,
                        "allowed": allowed,
                        "reason": "实际审核者" if allowed else "非该阶段实际审核者",
                    },
                )
            ],
            actor,
            self.clock.now().isoformat(),
            expected_version=None,
        )
        if not allowed:
            raise PermissionDeniedError("敏感合同仅对该阶段实际审核者开放")
        assert self.vault is not None
        return self.vault.reveal(ev["secret_id"])

    # ===============================================================
    # 投影：地区差距、多维成效、审计下钻
    # ===============================================================

    def region_gap_report(self) -> list[dict[str, Any]]:
        """各地区“受益（已锁定/已支付）与承诺（阶段拟拨+成效承诺）”差距。"""
        rows: dict[str, dict[str, Any]] = {}
        for app in self.repo.list_applications():
            project = self.repo.project(app.project_id)
            region = project.region if project else (app.regions[0] if app.regions else "未知")
            row = rows.setdefault(
                region,
                {
                    "region": region,
                    "project_count": 0,
                    "committed_amount": Decimal("0"),
                    "payable_amount": Decimal("0"),
                    "paid_amount": Decimal("0"),
                    "refunded_amount": Decimal("0"),
                    "locked_stages": 0,
                    "total_stages": 0,
                },
            )
            row["project_count"] += 0  # 项目数在循环后按项目去重统计
            for stage in app.stages.values():
                row["total_stages"] += 1
                row["committed_amount"] += Decimal(stage["planned_amount"])
                if stage["status"] == dm.STAGE_LOCKED:
                    row["locked_stages"] += 1
                    row["payable_amount"] += Decimal(stage["payable_amount"])
                    pay = app.payments.get(stage["payment_id"] or "")
                    if pay and pay["status"] == dm.PAYMENT_PAID:
                        row["paid_amount"] += Decimal(pay["amount"])
            for pay in app.payments.values():
                for r in pay["refunds"]:
                    row["refunded_amount"] += Decimal(r["amount"])

        project_regions = {p.project_id: p.region for p in self.repo.list_projects()}
        region_projects: dict[str, set[str]] = {}
        for app in self.repo.list_applications():
            region = project_regions.get(app.project_id, "未知")
            region_projects.setdefault(region, set()).add(app.project_id)

        result = []
        for region, row in rows.items():
            committed = row["committed_amount"]
            paid = row["paid_amount"]
            result.append(
                {
                    "region": region,
                    "project_count": len(region_projects.get(region, set())),
                    "locked_stages": row["locked_stages"],
                    "total_stages": row["total_stages"],
                    "committed_amount": _q(committed),
                    "payable_amount": _q(row["payable_amount"]),
                    "paid_amount": _q(paid),
                    "refunded_amount": _q(row["refunded_amount"]),
                    "payment_gap_amount": _q(committed - paid),
                    "payment_gap_ratio": _ratio(paid, committed),
                }
            )
        return sorted(result, key=lambda r: r["region"])

    def effectiveness_report(self, rule_code: str | None = None) -> dict[str, Any]:
        """多维公共文化成效：流量只是一维，并对“唯流量”给出警示。"""
        rules = {r.code: r for r in self.repo.list_rules()}
        rule = rules.get(rule_code) if rule_code else (self.repo.list_rules()[0] if rules else None)
        dims: dict[str, dict[str, Any]] = {}
        if rule and rule.current:
            for d in rule.current["metric_dims"]:
                dims[d["key"]] = dict(d)

        applications_out: list[dict[str, Any]] = []
        for app in self.repo.list_applications():
            if rule_code and app.rule_code != rule_code:
                continue
            stages_out = []
            for stage in app.stages.values():
                agg = self._aggregate_metrics(stage)
                traffic_key = (rule.current["traffic_metric_key"] if rule and rule.current else "views")
                warnings = []
                reported = {k: v for k, v in agg.items() if v is not None}
                if traffic_key in reported and len(reported) == 1:
                    warnings.append("仅有流量指标，不能单独代表公共文化成效")
                # 承诺差距
                commitment_gaps = []
                for key, promised in (stage["commitment"].get("metrics") or {}).items():
                    actual = agg.get(key)
                    if actual is None:
                        commitment_gaps.append({"key": key, "promised": str(promised), "actual": None, "status": "missing"})
                    else:
                        ok = actual >= Decimal(str(promised))
                        commitment_gaps.append(
                            {"key": key, "promised": str(promised), "actual": str(actual),
                             "status": "met" if ok else "short"}
                        )
                stages_out.append(
                    {
                        "stage_id": stage["stage_id"],
                        "status": stage["status"],
                        "metrics_actual": {k: _fmt_num(v) for k, v in agg.items()},
                        "commitment_checks": commitment_gaps,
                        "warnings": warnings,
                        "adopted_evidence_count": sum(1 for e in stage["evidence"].values() if e["adopted"] is True),
                        "disputed": stage["status"] == dm.STAGE_DISPUTED or self._has_open_dispute(stage),
                    }
                )
            applications_out.append(
                {
                    "application_id": app.application_id,
                    "project_id": app.project_id,
                    "fiscal_year": app.fiscal_year,
                    "stages": stages_out,
                }
            )

        return {
            "rule_code": rule.code if rule else None,
            "traffic_metric_key": rule.current["traffic_metric_key"] if rule and rule.current else "views",
            "dimensions": list(dims.values()) or [
                {"key": "service_count", "label": "服务人次/场次", "agg": "sum", "traffic": False},
                {"key": "satisfaction", "label": "公众满意度", "agg": "avg", "traffic": False},
                {"key": "repeat_participation", "label": "重复参与率", "agg": "avg", "traffic": False},
                {"key": "views", "label": "传播量", "agg": "sum", "traffic": True},
            ],
            "principle": "流量仅作参考维度之一，成效以服务记录与公众反馈等多维证据综合判断",
            "applications": applications_out,
        }

    def audit_drilldown(self, payment_id: str) -> dict[str, Any]:
        """审计下钻：从任一拨付结果看到规则、证据、去重决定、审批链与支付过程。"""
        located = self.repo.payment(payment_id)
        if located is None:
            raise NotFoundError(f"支付单 {payment_id} 不存在")
        app, pay = located
        stage = next(s for s in app.stages.values() if s["stage_id"] == pay["stage_id"])
        project = self.repo.project(app.project_id)
        lock = stage["locked"] or {}

        evidence_out = []
        for ev in stage["evidence"].values():
            evidence_out.append(
                {
                    "evidence_id": ev["evidence_id"],
                    "kind": ev["kind"],
                    "title": ev["title"],
                    "submitted_by_org": ev["submitted_by_org"],
                    "fingerprint": ev["fingerprint"],
                    "adopted": ev["adopted"],
                    "credited_share": ev["credited_share"],
                    "dedup_group_id": ev["dedup_group_id"],
                    "dedup_rationale": ev["dedup_rationale"],
                    "dispute_id": ev["dispute_id"],
                    "sensitive": ev["sensitive"],
                    "secret_revealed": False,  # 审计视图不含合同正文
                    "metrics": ev["metrics"],
                    "submitted_event_id": ev["submitted_event_id"],
                }
            )

        disputes_out = []
        for d_id in stage["dispute_ids"]:
            d = self.repo.dispute(d_id)
            if d is None:
                continue
            disputes_out.append(
                {
                    "dispute_id": d.dispute_id,
                    "fingerprint": d.fingerprint,
                    "status": d.status,
                    "candidates": d.candidates,
                    "negotiations": d.negotiations,
                    "resolution": d.resolution,
                }
            )

        timeline = []
        if project:
            timeline = [
                {"type": "region_change", **h} for h in project.region_history[1:]
            ]
            timeline += [
                {"type": "merge", **h} for h in project.merge_history
            ] + [
                {"type": "carryover", **h} for h in project.carryover_history
            ] + [
                {"type": "refund", **h} for h in project.refund_history
            ]

        return {
            "payment": {
                "payment_id": pay["payment_id"],
                "amount": pay["amount"],
                "status": pay["status"],
                "gateway_ref": pay["gateway_ref"],
                "attempts": pay["attempts"],
                "refunds": pay["refunds"],
                "created_event_id": pay["created_event_id"],
            },
            "stage": {
                "stage_id": stage["stage_id"],
                "name": stage["name"],
                "planned_amount": stage["planned_amount"],
                "payable_amount": stage["payable_amount"],
                "status": stage["status"],
            },
            "application": {
                "application_id": app.application_id,
                "project_id": app.project_id,
                "applicant_org_id": app.applicant_org_id,
                "partner_org_ids": app.partner_org_ids,
                "rule_code": app.rule_code,
                "rule_version_at_submission": app.rule_version_at_submission,
                "fiscal_year": app.fiscal_year,
            },
            "effective_rule_at_lock": {
                "version": lock.get("rule_version"),
                "effective_date": lock.get("rule_effective_date"),
                "snapshot": lock.get("rule_snapshot"),
            },
            "adopted_evidence": evidence_out,
            "dedup": {
                "disputes": disputes_out,
                "note": "credited_share 为同一成果在各申报间的归属分成，和恒为 1",
            },
            "approval_chain": [
                entry for entry in (
                    {"step": "business", **stage["business_review"]} if stage["business_review"] else None,
                    {"step": "finance", **stage["finance_review"]} if stage["finance_review"] else None,
                    {"step": "lock", "event_id": lock.get("event_id"), "at": lock.get("locked_at")} if lock else None,
                ) if entry is not None
            ],
            "project_continuous_events": timeline,
            "sensitive_access_log": app.access_log,
        }

    # ===============================================================
    # 内部辅助
    # ===============================================================

    def _stage(self, application_id: str, stage_id: str | None, *, evidence_id: str | None = None):
        app = self.repo.application(application_id)
        if app is None:
            raise NotFoundError(f"申报 {application_id} 不存在")
        if evidence_id is not None:
            for st in app.stages.values():
                if evidence_id in st["evidence"]:
                    return app, st
            raise NotFoundError(f"证据 {evidence_id} 不存在于申报 {application_id}")
        stage = app.stages.get(stage_id or "")
        if stage is None:
            raise NotFoundError(f"阶段 {stage_id} 不存在于申报 {application_id}")
        return app, stage

    def _dispute(self, dispute_id: str) -> dm.DisputeState:
        dispute = self.repo.dispute(dispute_id)
        if dispute is None:
            raise NotFoundError(f"争议 {dispute_id} 不存在")
        return dispute

    def _append_app(
        self,
        application_id: str,
        event_type: str | None,
        actor: Actor,
        data: dict[str, Any] | None = None,
        *,
        events: list[PendingEvent] | None = None,
    ):
        pending = events if events is not None else [PendingEvent(event_type, data or {})]
        return self.store.append(
            dm.AGG_APPLICATION,
            application_id,
            pending,
            actor,
            self.clock.now().isoformat(),
            expected_version=self.store.stream_version(dm.AGG_APPLICATION, application_id),
        )

    @staticmethod
    def _assert_assigned_reviewer(actor: Actor, stage: dict[str, Any], key: str) -> None:
        assigned = stage.get(f"{key}")
        if assigned != actor.user_id:
            raise PermissionDeniedError(
                f"该阶段指派的{('业务' if key == 'business_reviewer' else '财务')}审核者为 {assigned}"
            )

    @staticmethod
    def _has_open_dispute(stage: dict[str, Any]) -> bool:
        return any(
            ev.get("adopted") is None and ev.get("dispute_id")
            for ev in stage["evidence"].values()
        )

    def _aggregate_metrics(self, stage: dict[str, Any]) -> dict[str, Decimal | None]:
        """汇总被采纳证据的指标，归属分成 credited_share 参与加权。

        - sum 维度（服务量、传播量等）：每条证据贡献 = 值 × 分成，共享成果不重复计数；
        - avg 维度（满意度等）：按分成加权平均；
        - max 维度：取最高值（容量型指标不按分成缩放）。
        """
        dims_meta: dict[str, dict[str, Any]] = {}
        rule = self.repo.list_rules()[0] if self.repo.list_rules() else None
        if rule and rule.current:
            for d in rule.current["metric_dims"]:
                dims_meta[d["key"]] = d
        adopted = [ev for ev in stage["evidence"].values() if ev["adopted"] is True]
        keys = {k for ev in adopted for k in ev["metrics"].keys()}
        out: dict[str, Decimal | None] = {}
        for key in keys:
            entries = [
                (Decimal(ev["metrics"][key]), Decimal(ev["credited_share"]))
                for ev in adopted
                if key in ev["metrics"]
            ]
            if not entries:
                out[key] = None
                continue
            agg = dims_meta.get(key, {}).get("agg", "sum")
            if agg == "avg":
                weight = sum(share for _, share in entries)
                out[key] = (
                    (sum(val * share for val, share in entries) / weight).quantize(Decimal("0.0001"))
                    if weight > 0
                    else Decimal("0")
                )
            elif agg == "max":
                out[key] = max(val for val, _ in entries)
            else:
                out[key] = sum((val * share for val, share in entries), Decimal("0"))
        return out

    def _check_commitment_metrics(self, app: Any, stage: dict[str, Any]) -> list[dict[str, Any]]:
        promised = stage["commitment"].get("metrics") or {}
        actual = self._aggregate_metrics(stage)
        checks = []
        for key, target in promised.items():
            val = actual.get(key)
            if val is None:
                checks.append({"key": key, "promised": str(target), "actual": None, "status": "missing"})
            else:
                checks.append(
                    {
                        "key": key,
                        "promised": str(target),
                        "actual": str(val),
                        "status": "met" if val >= Decimal(str(target)) else "short",
                    }
                )
        return checks


def _short_uid() -> str:
    import uuid

    return uuid.uuid4().hex[:8]


def _date(value: str) -> None:
    from datetime import date

    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"日期格式应为 YYYY-MM-DD：{value}") from exc


def _q(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _fmt_num(value: Decimal | None) -> str | None:
    """整数指标不显示尾随小数点（999999.0 -> 999999），非整数保留最多 4 位。"""
    if value is None:
        return None
    if value == value.to_integral_value():
        return str(value.quantize(Decimal("1")))
    return str(value.quantize(Decimal("0.0001")).normalize())


def _ratio(part: Decimal, whole: Decimal) -> str:
    if whole == 0:
        return "0.00"
    return str((part / whole).quantize(Decimal("0.01")))
