"""HTTP/JSON 端到端：联合申报冲突、政策换版、失败支付重试一次走通。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from cultural_fund.api import ApiContext, create_server
from cultural_fund.auth import User
from cultural_fund.clock import VirtualClock
from cultural_fund.services import (
    ROLE_AUDITOR,
    ROLE_AUTHORITY,
    ROLE_APPLICANT,
    ROLE_BUSINESS,
    ROLE_FINANCE,
)

DIMS = [
    {"key": "service_count", "label": "人次", "agg": "sum", "traffic": False},
    {"key": "satisfaction", "label": "满意度", "agg": "avg", "traffic": False},
    {"key": "views", "label": "传播量", "agg": "sum", "traffic": True},
]


class ApiClient:
    def __init__(self, base: str, tokens: dict[str, str]):
        self.base = base
        self.tokens = tokens

    def call(self, method: str, path: str, body=None, *, as_user: str = "u-office"):
        url = self.base + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.tokens[as_user]}")
        if data is not None:
            req.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture
def api():
    ctx = ApiContext(clock=VirtualClock("2026-01-10T09:00:00+00:00"))
    users = [
        User("u-office", ROLE_AUTHORITY, display_name="办公室"),
        User("u-a", ROLE_APPLICANT, org_id="org-a", display_name="A"),
        User("u-b", ROLE_APPLICANT, org_id="org-b", display_name="B"),
        User("u-biz", ROLE_BUSINESS, display_name="业务"),
        User("u-fin", ROLE_FINANCE, display_name="财务"),
        User("u-audit", ROLE_AUDITOR, display_name="审计"),
        User("u-biz2", ROLE_BUSINESS, display_name="其他业务"),
    ]
    tokens = {}
    for u in users:
        tokens[u.user_id] = ctx.auth.register(u, token=f"tok-{u.user_id}")

    server = create_server("127.0.0.1", 0, ctx)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/api/v1"
    client = ApiClient(base, tokens)
    client.ctx = ctx
    try:
        yield client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _evidence(prefix, org, *, service_count=2000, sensitive=False, case=None):
    case = case if case is not None else prefix
    ev = [
        {"evidence_id": f"{prefix}-contract", "kind": "contract", "title": f"{prefix}合同",
         "submitted_by_org": org, "sensitive": sensitive,
         "secret": {"title": "敏感合同", "clauses": "正文与账号"},
         "achievement": {"identity": {"doc": f"{prefix}-{org}"}}, "metrics": {}},
        {"evidence_id": f"{prefix}-feedback", "kind": "public_feedback", "title": "反馈",
         "submitted_by_org": org, "achievement": {"identity": {"fb": f"{prefix}-{org}"}},
         "metrics": {"satisfaction": "4.7"}},
        {"evidence_id": f"{prefix}-service", "kind": "service_record", "title": "服务",
         "submitted_by_org": org,
         "achievement": {"identity": {"activity": "联合云剧场", "case": case,
                                      "date": "2026-03-01", "venue": "市馆"}},
         "metrics": {"service_count": str(service_count), "satisfaction": "4.8", "views": "50000"}},
    ]
    if not sensitive:
        ev[0].pop("secret")
    return ev


def _prepare_rule_and_projects(client):
    status, _ = client.call("POST", "/rules", {
        "code": "R", "name": "规则", "version": 1, "effective_date": "2026-01-01",
        "support_categories": ["数字文化"], "stage_cap": "150000.00", "subsidy_rate": "0.80",
        "required_metrics": ["service_count", "satisfaction"], "metric_dims": DIMS,
    })
    assert status == 200
    for pid, name, region, org in [("P1", "项目A", "城区", "org-a"),
                                   ("P2", "项目B", "县域", "org-b")]:
        status, _ = client.call("POST", "/projects", {
            "project_id": pid, "name": name, "region": region, "applicant_org_id": org,
        })
        assert status == 200


def _apply(client, app_id, project_id, user):
    stages = [{"stage_id": "s1", "name": "首期", "planned_amount": "120000.00",
               "commitment": {"metrics": {"service_count": 1000, "satisfaction": 4.5}}}]
    status, _ = client.call("POST", "/applications", {
        "application_id": app_id, "project_id": project_id, "fiscal_year": 2026,
        "stages": stages,
    }, as_user=user)
    assert status == 200
    client.call("POST", f"/applications/{app_id}/stages/s1/reviewers",
                {"business_reviewer": "u-biz", "finance_reviewer": "u-fin"})


def test_full_closed_loop_over_http(api):
    client = api
    _prepare_rule_and_projects(client)
    _apply(client, "appA", "P1", "u-a")
    _apply(client, "appB", "P2", "u-b")

    # 双方提交同一成果 -> 争议
    status, _ = client.call("POST", "/applications/appA/stages/s1/evidence",
                            {"evidence": _evidence("a", "org-a", case="joint")}, as_user="u-a")
    assert status == 200
    status, _ = client.call("POST", "/applications/appB/stages/s1/evidence",
                            {"evidence": _evidence("b", "org-b", case="joint")}, as_user="u-b")
    assert status == 200
    status, disputes = client.call("GET", "/disputes")
    assert status == 200 and len(disputes["disputes"]) == 1
    dispute_id = disputes["disputes"][0]["dispute_id"]

    # 归属协商
    status, _ = client.call("POST", f"/disputes/{dispute_id}/negotiations",
                            {"proposal": "五五分成"}, as_user="u-a")
    assert status == 200
    # 裁决
    allocations = [
        {"application_id": "appA", "stage_id": "s1", "evidence_id": "a-service",
         "org_id": "org-a", "adopted": True, "credited_share": "0.5"},
        {"application_id": "appB", "stage_id": "s1", "evidence_id": "b-service",
         "org_id": "org-b", "adopted": True, "credited_share": "0.5"},
    ]
    status, _ = client.call("POST", f"/disputes/{dispute_id}/resolve",
                            {"decision": "shared", "rationale": "联合共创五五分成",
                             "allocations": allocations})
    assert status == 200

    # 双审 + 锁定
    status, _ = client.call("POST", "/applications/appA/stages/s1/business-review",
                            {"decision": "approved"}, as_user="u-biz")
    assert status == 200
    status, _ = client.call("POST", "/applications/appA/stages/s1/finance-review",
                            {"decision": "approved"}, as_user="u-fin")
    assert status == 200
    status, _ = client.call("POST", "/applications/appA/stages/s1/lock",
                            {"payment_id": "payA"}, as_user="u-fin")
    assert status == 200

    # 支付失败两次后重试成功
    status, _ = client.call("POST", "/payments/payA/simulate-failure", {"fail_times": 2})
    assert status == 200
    status, r1 = client.call("POST", "/payments/payA/attempt",
                             {"idempotency_key": "k1"}, as_user="u-fin")
    assert r1["events"][0]["data"]["result"] == "failed"
    # 同键重放
    status, r1b = client.call("POST", "/payments/payA/attempt",
                              {"idempotency_key": "k1"}, as_user="u-fin")
    assert r1b["events"][0]["event_id"] == r1["events"][0]["event_id"]
    status, r2 = client.call("POST", "/payments/payA/attempt",
                             {"idempotency_key": "k2"}, as_user="u-fin")
    assert r2["events"][0]["data"]["result"] == "failed"
    status, r3 = client.call("POST", "/payments/payA/attempt",
                             {"idempotency_key": "k3"}, as_user="u-fin")
    assert r3["events"][0]["data"]["result"] == "succeeded"

    # 审计下钻
    status, drill = client.call("GET", "/audit/payments/payA", as_user="u-audit")
    assert status == 200
    assert drill["payment"]["status"] == "paid"
    assert [c["step"] for c in drill["approval_chain"] if c] == ["business", "finance", "lock"]
    assert drill["effective_rule_at_lock"]["version"] == 1
    assert drill["dedup"]["disputes"][0]["resolution"]["rationale"] == "联合共创五五分成"

    # 哈希链校验
    status, verified = client.call("POST", "/events/verify", {}, as_user="u-audit")
    assert status == 200 and verified["status"] == "ok"


def test_sensitive_contract_rbac_over_http(api):
    client = api
    _prepare_rule_and_projects(client)
    _apply(client, "appA", "P1", "u-a")
    status, _ = client.call("POST", "/applications/appA/stages/s1/evidence",
                            {"evidence": _evidence("a", "org-a", sensitive=True)},
                            as_user="u-a")
    assert status == 200
    # 其他业务审核者拒绝
    status, err = client.call(
        "POST", "/applications/appA/evidence/a-contract/reveal", {}, as_user="u-biz2")
    assert status == 403 and err["error"] == "permission_denied"
    # 实际指派的业务审核者可读
    status, ok = client.call(
        "POST", "/applications/appA/evidence/a-contract/reveal", {}, as_user="u-biz")
    assert status == 200 and "正文" in ok["contract"]["clauses"]
    # 无令牌拒绝
    req = urllib.request.Request(
        client.base + "/applications/appA/evidence/a-contract/reveal", data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 403


def test_rule_revision_over_http_only_affects_new_review(api):
    client = api
    _prepare_rule_and_projects(client)
    _apply(client, "appA", "P1", "u-a")
    # 6 月前完成锁定：v1
    api.ctx.clock.set("2026-06-20T09:00:00+00:00")
    client.call("POST", "/applications/appA/stages/s1/evidence",
                {"evidence": _evidence("a", "org-a")}, as_user="u-a")
    client.call("POST", "/applications/appA/stages/s1/business-review",
                {"decision": "approved"}, as_user="u-biz")
    client.call("POST", "/applications/appA/stages/s1/finance-review",
                {"decision": "approved"}, as_user="u-fin")
    client.call("POST", "/applications/appA/stages/s1/lock",
                {"payment_id": "payA"}, as_user="u-fin")

    # 发布 v2（7 月生效）
    status, _ = client.call("POST", "/rules", {
        "code": "R", "name": "规则v2", "version": 2, "effective_date": "2026-07-01",
        "support_categories": ["数字文化"], "stage_cap": "200000.00", "subsidy_rate": "0.90",
        "required_metrics": ["service_count", "satisfaction"], "metric_dims": DIMS,
    })
    assert status == 200

    # 新项目 8 月审核：适用 v2
    api.ctx.clock.set("2026-08-01T09:00:00+00:00")
    client.call("POST", "/projects", {"project_id": "P3", "name": "项目C", "region": "滨海",
                                      "applicant_org_id": "org-a"})
    stages = [{"stage_id": "s1", "name": "首期", "planned_amount": "120000.00",
               "commitment": {"metrics": {"service_count": 1000, "satisfaction": 4.5}}}]
    client.call("POST", "/applications", {"application_id": "appC", "project_id": "P3",
                                          "fiscal_year": 2026, "stages": stages}, as_user="u-a")
    client.call("POST", "/applications/appC/stages/s1/reviewers",
                {"business_reviewer": "u-biz", "finance_reviewer": "u-fin"})
    client.call("POST", "/applications/appC/stages/s1/evidence",
                {"evidence": _evidence("c", "org-a")}, as_user="u-a")
    client.call("POST", "/applications/appC/stages/s1/business-review",
                {"decision": "approved"}, as_user="u-biz")
    status, _ = client.call("POST", "/applications/appC/stages/s1/finance-review",
                            {"decision": "approved"}, as_user="u-fin")
    assert status == 200
    _, appC = client.call("GET", "/applications/appC")
    assert appC["stages"][0]["finance_review"]["rule_version"] == 2
    assert appC["stages"][0]["payable_amount"] == "108000.00"
    _, appA = client.call("GET", "/applications/appA")
    assert appA["stages"][0]["locked"]["rule_version"] == 1
    assert appA["stages"][0]["payable_amount"] == "96000.00"


def test_reports_and_visibility_over_http(api):
    client = api
    _prepare_rule_and_projects(client)
    # 未认证 403
    req = urllib.request.Request(client.base + "/reports/regions")
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 403
    # 申报单位不能访问审计级报表
    status, err = client.call("GET", "/reports/regions", as_user="u-a")
    assert status == 403
    # 审计可访问
    status, report = client.call("GET", "/reports/regions", as_user="u-audit")
    assert status == 200 and "regions" in report
    # 申报单位只能看自己的申报
    client.call("POST", "/projects", {"project_id": "P9", "name": "P9", "region": "x",
                                      "applicant_org_id": "org-a"})
    stages = [{"stage_id": "s1", "name": "n", "planned_amount": "1", "commitment": {}}]
    client.call("POST", "/applications", {"application_id": "appSecret", "project_id": "P9",
                                          "fiscal_year": 2026, "stages": stages}, as_user="u-a")
    _, apps_a = client.call("GET", "/applications", as_user="u-a")
    assert {a["application_id"] for a in apps_a["applications"]} == {"appSecret"}
