"""共享测试夹具：构建带虚拟时钟、密件库的应用服务与固定角色。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "custom_rules: 测试自行发布规则，退出默认规则夹具")

from cultural_fund.clock import VirtualClock
from cultural_fund.eventstore import Actor, InMemoryEventStore
from cultural_fund.repository import Repository
from cultural_fund.services import CulturalFundService
from cultural_fund.vault import SecretVault

SEED_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "seed.json"

METRIC_DIMS = [
    {"key": "service_count", "label": "服务人次", "agg": "sum", "traffic": False},
    {"key": "satisfaction", "label": "满意度", "agg": "avg", "traffic": False},
    {"key": "repeat_participation", "label": "重复参与率", "agg": "avg", "traffic": False},
    {"key": "views", "label": "传播量", "agg": "sum", "traffic": True},
]


@pytest.fixture
def clock():
    return VirtualClock("2026-01-10T09:00:00+00:00")


@pytest.fixture
def store():
    return InMemoryEventStore()


@pytest.fixture
def repo(store):
    return Repository(store)


@pytest.fixture
def vault():
    return SecretVault()


@pytest.fixture
def svc(store, clock, repo, vault):
    return CulturalFundService(store, clock, repo, vault)


@pytest.fixture
def seed_data():
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


# ---- 角色 ----

@pytest.fixture
def office():
    return Actor("u-office", "authority", display_name="推进办公室")


@pytest.fixture
def org_a():
    return Actor("u-org-a", "applicant", org_id="org-a", display_name="A中心")


@pytest.fixture
def org_b():
    return Actor("u-org-b", "applicant", org_id="org-b", display_name="B单位")


@pytest.fixture
def biz():
    return Actor("u-biz", "business_reviewer", display_name="业务审核")


@pytest.fixture
def biz2():
    return Actor("u-biz2", "business_reviewer", display_name="另一业务审核")


@pytest.fixture
def fin():
    return Actor("u-fin", "finance_reviewer", display_name="财务审核")


@pytest.fixture
def auditor():
    return Actor("u-auditor", "auditor", display_name="审计")


@pytest.fixture(autouse=True)
def _base_rule(request, svc, office):
    """默认给每个领域测试发布一版 2026-01-01 生效的规则。

    需要自行控制规则生效/换版时点的测试标记 ``custom_rules`` 即可退出。
    """
    if "custom_rules" in request.keywords:
        return
    if svc.repo.list_rules():
        return
    svc.publish_rule(
        office,
        code="R",
        name="扶持规则",
        version=1,
        effective_date="2026-01-01",
        support_categories=["数字文化"],
        stage_cap="150000.00",
        subsidy_rate="0.80",
        required_metrics=["service_count", "satisfaction"],
        metric_dims=METRIC_DIMS,
    )


@pytest.fixture
def rule_v1(svc):
    # 基础规则已由 autouse fixture 发布
    return svc.repo.rule("R")


@pytest.fixture
def rule_v2(svc, office, clock):
    # 6 月发布、7 月生效的换版：提高上限与补贴率
    clock.set("2026-06-01T09:00:00+00:00")
    svc.publish_rule(
        office,
        code="R",
        name="扶持规则（年中修订）",
        version=2,
        effective_date="2026-07-01",
        support_categories=["数字文化", "文化惠民"],
        stage_cap="200000.00",
        subsidy_rate="0.90",
        required_metrics=["service_count", "satisfaction"],
        metric_dims=METRIC_DIMS,
        notes="换版只影响尚未锁定的阶段",
    )
    clock.set("2026-01-10T09:00:00+00:00")


def make_evidence_set(prefix: str, org: str, *, service_count=1200, satisfaction=4.8,
                      views=50000, sensitive=False, secret=None, case=None):
    """构造一套齐备的三类证据。

    case 决定成果指纹：不同申报各自独立成果时保持默认（=prefix）；
    需要模拟联合申报重复同一成果时，各调用方显式传入相同 case。
    """
    case = case if case is not None else prefix
    return [
        {
            "evidence_id": f"{prefix}-contract",
            "kind": "contract",
            "title": f"{prefix} 服务合同",
            "submitted_by_org": org,
            "sensitive": sensitive,
            "secret": secret,
            "achievement": {"identity": {"doc": f"contract-{prefix}-{org}"}},
            "metrics": {},
        },
        {
            "evidence_id": f"{prefix}-feedback",
            "kind": "public_feedback",
            "title": f"{prefix} 公众反馈",
            "submitted_by_org": org,
            "achievement": {"identity": {"feedback": f"feedback-{prefix}-{org}"}},
            "metrics": {"satisfaction": "4.7"},
        },
        {
            "evidence_id": f"{prefix}-service",
            "kind": "service_record",
            "title": f"{prefix} 服务记录",
            "submitted_by_org": org,
            "achievement": {"identity": {"activity": "联合云剧场", "case": case,
                                         "date": "2026-03-01", "venue": "市馆"}},
            "metrics": {
                "service_count": str(service_count),
                "satisfaction": str(satisfaction),
                "views": str(views),
            },
        },
    ]
