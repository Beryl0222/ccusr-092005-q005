"""HTTP/JSON 接口（仅依赖标准库 http.server）。

所有写操作都是“命令 -> 事件”；GET 端点返回只读投影。
鉴权：Authorization: Bearer <token>；敏感合同仅实际审核者可经专用端点阅取。
"""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from . import serializers as sz
from .auth import AuthRegistry, User
from .clock import Clock, SystemClock
from .errors import DomainError, PermissionDeniedError
from .eventstore import EventStore, InMemoryEventStore
from .payments import SimulatedPaymentGateway
from .repository import Repository
from .services import (
    ROLE_AUDITOR,
    ROLE_AUTHORITY,
    ROLE_APPLICANT,
    ROLE_BUSINESS,
    ROLE_FINANCE,
    CulturalFundService,
)
from .vault import SecretVault


class ApiContext:
    def __init__(
        self,
        store: EventStore | None = None,
        vault: SecretVault | None = None,
        auth: AuthRegistry | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.store = store or InMemoryEventStore()
        self.repo = Repository(self.store)
        self.auth = auth or AuthRegistry()
        self.vault = vault or SecretVault()
        self.clock = clock or SystemClock()
        self.gateway = SimulatedPaymentGateway()
        self.service = CulturalFundService(
            self.store, self.clock, self.repo, self.vault
        )


def _events_payload(events: list[Any]) -> dict[str, Any]:
    return {"status": "ok", "events": [e.to_dict() for e in events]}


Route = tuple[str, re.Pattern[str], Callable[..., Any], set[str] | None]


class ApiHandler(BaseHTTPRequestHandler):
    ctx: ApiContext  # 由工厂函数绑定到子类

    server_version = "CulturalFundAPI/1.0"

    # ------------------------------------------------------------------
    # 路由表
    # ------------------------------------------------------------------

    @classmethod
    def routes(cls) -> list[Route]:
        svc = r"/api/v1"
        return [
            ("POST", re.compile(rf"{svc}/rules$"), cls.publish_rule, {ROLE_AUTHORITY}),
            ("GET", re.compile(rf"{svc}/rules$"), cls.list_rules, None),
            ("GET", re.compile(rf"{svc}/rules/(?P<code>[^/]+)$"), cls.get_rule, None),

            ("POST", re.compile(rf"{svc}/projects$"), cls.register_project, {ROLE_AUTHORITY}),
            ("GET", re.compile(rf"{svc}/projects$"), cls.list_projects, None),
            ("GET", re.compile(rf"{svc}/projects/(?P<project_id>[^/]+)$"), cls.get_project, None),
            ("POST", re.compile(rf"{svc}/projects/(?P<project_id>[^/]+)/region-change$"),
             cls.region_change, {ROLE_AUTHORITY}),
            ("POST", re.compile(rf"{svc}/projects/merge$"), cls.merge_projects, {ROLE_AUTHORITY}),
            ("POST", re.compile(rf"{svc}/projects/(?P<project_id>[^/]+)/carryover$"),
             cls.carry_over, {ROLE_AUTHORITY}),
            ("POST", re.compile(rf"{svc}/projects/(?P<project_id>[^/]+)/refund$"),
             cls.refund, {ROLE_AUTHORITY, ROLE_FINANCE}),

            ("POST", re.compile(rf"{svc}/applications$"), cls.submit_application,
             {ROLE_APPLICANT, ROLE_AUTHORITY}),
            ("GET", re.compile(rf"{svc}/applications$"), cls.list_applications, None),
            ("GET", re.compile(rf"{svc}/applications/(?P<application_id>[^/]+)$"),
             cls.get_application, None),

            ("POST", re.compile(rf"{svc}/applications/(?P<application_id>[^/]+)/stages/"
                                r"(?P<stage_id>[^/]+)/reviewers$"),
             cls.assign_reviewers, {ROLE_AUTHORITY}),
            ("POST", re.compile(rf"{svc}/applications/(?P<application_id>[^/]+)/stages/"
                                r"(?P<stage_id>[^/]+)/evidence$"),
             cls.submit_evidence, {ROLE_APPLICANT, ROLE_AUTHORITY}),
            ("POST", re.compile(rf"{svc}/applications/(?P<application_id>[^/]+)/stages/"
                                r"(?P<stage_id>[^/]+)/business-review$"),
             cls.business_review, {ROLE_BUSINESS}),
            ("POST", re.compile(rf"{svc}/applications/(?P<application_id>[^/]+)/stages/"
                                r"(?P<stage_id>[^/]+)/finance-review$"),
             cls.finance_review, {ROLE_FINANCE}),
            ("POST", re.compile(rf"{svc}/applications/(?P<application_id>[^/]+)/stages/"
                                r"(?P<stage_id>[^/]+)/lock$"),
             cls.lock_stage, {ROLE_FINANCE, ROLE_AUTHORITY}),

            ("POST", re.compile(rf"{svc}/applications/(?P<application_id>[^/]+)/evidence/"
                                r"(?P<evidence_id>[^/]+)/reveal$"),
             cls.reveal_contract, {ROLE_BUSINESS, ROLE_FINANCE, ROLE_AUDITOR}),

            ("GET", re.compile(rf"{svc}/disputes$"), cls.list_disputes, None),
            ("GET", re.compile(rf"{svc}/disputes/(?P<dispute_id>[^/]+)$"), cls.get_dispute, None),
            ("POST", re.compile(rf"{svc}/disputes/(?P<dispute_id>[^/]+)/negotiations$"),
             cls.negotiation, {ROLE_APPLICANT, ROLE_AUTHORITY}),
            ("POST", re.compile(rf"{svc}/disputes/(?P<dispute_id>[^/]+)/resolve$"),
             cls.resolve_dispute, {ROLE_AUTHORITY}),

            ("POST", re.compile(rf"{svc}/payments/(?P<payment_id>[^/]+)/simulate-failure$"),
             cls.simulate_failure, {ROLE_AUTHORITY}),
            ("POST", re.compile(rf"{svc}/payments/(?P<payment_id>[^/]+)/attempt$"),
             cls.attempt_payment, {ROLE_FINANCE, ROLE_AUTHORITY}),

            ("GET", re.compile(rf"{svc}/reports/regions$"), cls.region_report, {ROLE_AUDITOR, ROLE_AUTHORITY}),
            ("GET", re.compile(rf"{svc}/reports/effectiveness$"), cls.effectiveness_report,
             {ROLE_AUDITOR, ROLE_AUTHORITY}),
            ("GET", re.compile(rf"{svc}/audit/payments/(?P<payment_id>[^/]+)$"),
             cls.audit_drilldown, {ROLE_AUDITOR, ROLE_AUTHORITY}),
            ("GET", re.compile(rf"{svc}/events$"), cls.list_events, {ROLE_AUDITOR, ROLE_AUTHORITY}),
            ("POST", re.compile(rf"{svc}/events/verify$"), cls.verify_events, {ROLE_AUDITOR, ROLE_AUTHORITY}),
        ]

    # ------------------------------------------------------------------
    # HTTP 框架
    # ------------------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        body = self._read_body()
        try:
            actor = self.ctx.auth.authenticate(self.headers.get("Authorization"))
            for verb, pattern, handler, roles in self.routes():
                if verb != method:
                    continue
                match = pattern.fullmatch(path)
                if not match:
                    continue
                if roles is not None and actor.role not in roles:
                    raise PermissionDeniedError(f"该端点要求角色 {sorted(roles)}")
                result = handler(self, actor, body, **match.groupdict())
                self._write_json(HTTPStatus.OK, result if result is not None else {"status": "ok"})
                return
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": path})
        except DomainError as exc:
            self._write_json(
                exc.http_status,
                {"error": exc.code, "message": str(exc)},
            )
        except PermissionDeniedError as exc:
            self._write_json(HTTPStatus.FORBIDDEN, {"error": "permission_denied", "message": str(exc)})
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "message": str(exc)})
        except NotImplementedError as exc:
            self._write_json(HTTPStatus.NOT_IMPLEMENTED, {"error": "not_implemented", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 兜底，避免把异常栈直接抛给调用方
            self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                             {"error": "internal_error", "message": str(exc)})

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _write_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静：测试不打访问日志
        return

    # ------------------------------------------------------------------
    # 处理器
    # ------------------------------------------------------------------

    @property
    def svc(self) -> CulturalFundService:
        return self.ctx.service

    def publish_rule(self, actor, body, **_):
        events = self.svc.publish_rule(
            actor,
            code=body["code"],
            name=body["name"],
            version=int(body["version"]),
            effective_date=body["effective_date"],
            support_categories=body["support_categories"],
            stage_cap=str(body["stage_cap"]),
            subsidy_rate=str(body["subsidy_rate"]),
            required_metrics=body["required_metrics"],
            metric_dims=body["metric_dims"],
            traffic_metric_key=body.get("traffic_metric_key", "views"),
            notes=body.get("notes", ""),
        )
        return _events_payload(events)

    def list_rules(self, actor, body, **_):
        return {"rules": [sz.rule_view(r) for r in self.svc.repo.list_rules()]}

    def get_rule(self, actor, body, **p):
        rule = self.svc.repo.rule(p["code"])
        if rule is None:
            from .errors import NotFoundError

            raise NotFoundError(f"规则 {p['code']} 不存在")
        return sz.rule_view(rule)

    def register_project(self, actor, body, **_):
        events = self.svc.register_project(
            actor,
            project_id=body["project_id"],
            name=body["name"],
            region=body["region"],
            applicant_org_id=body.get("applicant_org_id", f"org-{body['project_id']}"),
            targets=body.get("targets", []),
            funding_tranches=int(body.get("funding_tranches", 1)),
            registered_date=body.get("registered_date"),
            milestones=body.get("milestones", []),
        )
        return _events_payload(events)

    def list_projects(self, actor, body, **_):
        return {"projects": [sz.project_view(p) for p in self.svc.repo.list_projects()]}

    def get_project(self, actor, body, **p):
        project = self.svc.repo.project(p["project_id"])
        if project is None:
            from .errors import NotFoundError

            raise NotFoundError(f"项目 {p['project_id']} 不存在")
        return sz.project_view(project)

    def region_change(self, actor, body, **p):
        return _events_payload(
            self.svc.change_project_region(
                actor,
                project_id=p["project_id"],
                to_region=body["to_region"],
                reason=body.get("reason", ""),
                effective_date=body["effective_date"],
            )
        )

    def merge_projects(self, actor, body, **_):
        return _events_payload(
            self.svc.merge_projects(
                actor,
                surviving_project_id=body["surviving_project_id"],
                merged_project_ids=body["merged_project_ids"],
                reason=body.get("reason", ""),
                effective_date=body["effective_date"],
            )
        )

    def carry_over(self, actor, body, **p):
        return _events_payload(
            self.svc.carry_over(
                actor,
                project_id=p["project_id"],
                from_fiscal_year=int(body["from_fiscal_year"]),
                to_fiscal_year=int(body["to_fiscal_year"]),
                amount=str(body["amount"]),
                reason=body.get("reason", ""),
            )
        )

    def refund(self, actor, body, **p):
        return _events_payload(
            self.svc.return_funds(
                actor,
                project_id=p["project_id"],
                amount=str(body["amount"]),
                reason=body.get("reason", ""),
                fiscal_year=int(body["fiscal_year"]),
                payment_id=body.get("payment_id"),
            )
        )

    def submit_application(self, actor, body, **_):
        return _events_payload(
            self.svc.submit_application(
                actor,
                application_id=body["application_id"],
                project_id=body["project_id"],
                fiscal_year=int(body["fiscal_year"]),
                applicant_org_id=body.get("applicant_org_id"),
                partner_org_ids=body.get("partner_org_ids", []),
                regions=body.get("regions"),
                stages=body["stages"],
            )
        )

    def list_applications(self, actor, body, **_):
        apps = self.svc.repo.list_applications()
        # 申报单位只看得到自己参与的申报；审核/主管/审计可看全部
        if actor.role == ROLE_APPLICANT:
            apps = self.svc.repo.applications_for_org(actor.org_id or "")
        return {"applications": [sz.application_view(a) for a in apps]}

    def get_application(self, actor, body, **p):
        app = self.svc.repo.application(p["application_id"])
        if app is None:
            from .errors import NotFoundError

            raise NotFoundError(f"申报 {p['application_id']} 不存在")
        if actor.role == ROLE_APPLICANT and actor.org_id not in app.all_orgs():
            from .errors import PermissionDeniedError

            raise PermissionDeniedError("无权查看该申报")
        return sz.application_view(app)

    def assign_reviewers(self, actor, body, **p):
        return _events_payload(
            self.svc.assign_reviewers(
                actor,
                application_id=p["application_id"],
                stage_id=p["stage_id"],
                business_reviewer=body["business_reviewer"],
                finance_reviewer=body["finance_reviewer"],
            )
        )

    def submit_evidence(self, actor, body, **p):
        return _events_payload(
            self.svc.submit_stage_evidence(
                actor,
                application_id=p["application_id"],
                stage_id=p["stage_id"],
                evidence=body["evidence"],
            )
        )

    def business_review(self, actor, body, **p):
        return _events_payload(
            self.svc.business_review(
                actor,
                application_id=p["application_id"],
                stage_id=p["stage_id"],
                decision=body.get("decision", "approved"),
                comments=body.get("comments", ""),
            )
        )

    def finance_review(self, actor, body, **p):
        return _events_payload(
            self.svc.finance_review(
                actor,
                application_id=p["application_id"],
                stage_id=p["stage_id"],
                decision=body.get("decision", "approved"),
                comments=body.get("comments", ""),
            )
        )

    def lock_stage(self, actor, body, **p):
        return _events_payload(
            self.svc.lock_stage(
                actor,
                application_id=p["application_id"],
                stage_id=p["stage_id"],
                payment_id=body["payment_id"],
            )
        )

    def reveal_contract(self, actor, body, **p):
        # 审计员可以看到“谁访问过”，但不是实际审核者，取不到正文
        secret = self.svc.read_sensitive_contract(
            actor,
            application_id=p["application_id"],
            evidence_id=p["evidence_id"],
        )
        return {"status": "ok", "contract": secret}

    def list_disputes(self, actor, body, **_):
        return {"disputes": [sz.dispute_view(d) for d in self.svc.repo.list_disputes()]}

    def get_dispute(self, actor, body, **p):
        dispute = self.svc.repo.dispute(p["dispute_id"])
        if dispute is None:
            from .errors import NotFoundError

            raise NotFoundError(f"争议 {p['dispute_id']} 不存在")
        return sz.dispute_view(dispute)

    def negotiation(self, actor, body, **p):
        return _events_payload(
            self.svc.record_negotiation(
                actor, dispute_id=p["dispute_id"], proposal=body["proposal"]
            )
        )

    def resolve_dispute(self, actor, body, **p):
        return _events_payload(
            self.svc.resolve_dispute(
                actor,
                dispute_id=p["dispute_id"],
                decision=body["decision"],
                allocations=body["allocations"],
                rationale=body.get("rationale", ""),
            )
        )

    def simulate_failure(self, actor, body, **p):
        self.ctx.gateway.set_failure(p["payment_id"], int(body.get("fail_times", 1)))
        return {"status": "ok", "payment_id": p["payment_id"],
                "fail_times": int(body.get("fail_times", 1))}

    def attempt_payment(self, actor, body, **p):
        return _events_payload(
            self.svc.attempt_payment(
                actor,
                payment_id=p["payment_id"],
                gateway=self.ctx.gateway,
                idempotency_key=body["idempotency_key"],
            )
        )

    def region_report(self, actor, body, **_):
        return {"regions": self.svc.region_gap_report()}

    def effectiveness_report(self, actor, body, **_):
        from urllib.parse import urlparse, parse_qs

        rule_code = parse_qs(urlparse(self.path).query).get("rule_code", [None])[0]
        return self.svc.effectiveness_report(rule_code)

    def audit_drilldown(self, actor, body, **p):
        return self.svc.audit_drilldown(p["payment_id"])

    def list_events(self, actor, body, **_):
        return {"events": [e.to_dict() for e in self.ctx.store.all_events()]}

    def verify_events(self, actor, body, **_):
        self.ctx.store.verify_chain()
        return {"status": "ok", "message": "哈希链完整，事件日志未被篡改"}


def create_server(host: str = "127.0.0.1", port: int = 8080, ctx: ApiContext | None = None):
    ctx = ctx or ApiContext()

    class _BoundHandler(ApiHandler):
        pass

    _BoundHandler.ctx = ctx
    server = ThreadingHTTPServer((host, port), _BoundHandler)
    server.ctx = ctx  # type: ignore[attr-defined]
    return server


def bootstrap_demo_users(ctx: ApiContext) -> dict[str, str]:
    """登记演示角色并返回 token 映射（仅用于演示/测试）。"""
    users = [
        User("u-office", ROLE_AUTHORITY, display_name="省文化产业推进办公室"),
        User("u-org-a", ROLE_APPLICANT, org_id="org-digital-center", display_name="数字文化中心"),
        User("u-org-b", ROLE_APPLICANT, org_id="org-grassroots", display_name="基层巡演单位"),
        User("u-org-c", ROLE_APPLICANT, org_id="org-partner-c", display_name="联合单位C"),
        User("u-biz", ROLE_BUSINESS, display_name="业务审核者"),
        User("u-fin", ROLE_FINANCE, display_name="财务审核者"),
        User("u-audit", ROLE_AUDITOR, display_name="年度审计人员"),
    ]
    tokens = {}
    for u in users:
        token = ctx.auth.register(u, token=f"token-{u.user_id}")
        tokens[u.user_id] = token
    return tokens
