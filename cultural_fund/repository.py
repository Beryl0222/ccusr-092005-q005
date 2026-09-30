"""基于事件流重建聚合的只读仓储，并维护跨申报的成果指纹索引。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import domain as dm
from .eventstore import Event, EventStore


@dataclass
class FingerprintHit:
    fingerprint: str
    application_id: str
    stage_id: str
    evidence_id: str
    submitted_by_org: str
    kind: str
    achievement: dict[str, Any]
    metrics: dict[str, str]
    adopted: bool | None
    locked: bool


class Repository:
    """从事件存储派生当前状态；按全局 seq 做缓存失效。"""

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self._cache: dict[tuple[str, str], Any] = {}
        self._watermark = -1

    def _maybe_reset(self) -> None:
        last = self.store.all_events()
        head = last[-1].seq if last else 0
        if head != self._watermark:
            self._cache.clear()
            self._watermark = head

    def rule(self, code: str) -> dm.RuleState | None:
        return self._get(dm.AGG_RULE, code)

    def project(self, project_id: str) -> dm.ProjectState | None:
        return self._get(dm.AGG_PROJECT, project_id)

    def application(self, application_id: str) -> dm.ApplicationState | None:
        return self._get(dm.AGG_APPLICATION, application_id)

    def dispute(self, dispute_id: str) -> dm.DisputeState | None:
        return self._get(dm.AGG_DISPUTE, dispute_id)

    def _get(self, agg_type: str, agg_id: str) -> Any:
        self._maybe_reset()
        key = (agg_type, agg_id)
        if key not in self._cache:
            events = self.store.load_stream(agg_type, agg_id)
            if not events:
                return None
            self._cache[key] = dm.fold(agg_type, events)
        return self._cache[key]

    # ---- 列表 ----------------------------------------------------------

    def list_rules(self) -> list[dm.RuleState]:
        return [self.rule(e.aggregate_id) for e in self._latest_per_aggregate(dm.AGG_RULE)]

    def list_projects(self) -> list[dm.ProjectState]:
        return [self.project(e.aggregate_id) for e in self._latest_per_aggregate(dm.AGG_PROJECT)]

    def list_applications(self) -> list[dm.ApplicationState]:
        return [self.application(e.aggregate_id) for e in self._latest_per_aggregate(dm.AGG_APPLICATION)]

    def list_disputes(self) -> list[dm.DisputeState]:
        return [self.dispute(e.aggregate_id) for e in self._latest_per_aggregate(dm.AGG_DISPUTE)]

    def _latest_per_aggregate(self, agg_type: str) -> list[Event]:
        latest: dict[str, Event] = {}
        for e in self.store.all_events():
            if e.aggregate_type == agg_type:
                latest[e.aggregate_id] = e
        return list(latest.values())

    # ---- 成果指纹去重索引 ----------------------------------------------

    def fingerprint_hits(self, fingerprint: str) -> list[FingerprintHit]:
        """跨全部申报查找同一成果指纹的证据（联合申报冲突检测的基础）。"""
        hits: list[FingerprintHit] = []
        for app in self.list_applications():
            for stage in app.stages.values():
                locked = stage["status"] == dm.STAGE_LOCKED
                for ev in stage["evidence"].values():
                    if ev["fingerprint"] == fingerprint:
                        hits.append(
                            FingerprintHit(
                                fingerprint=fingerprint,
                                application_id=app.application_id,
                                stage_id=stage["stage_id"],
                                evidence_id=ev["evidence_id"],
                                submitted_by_org=ev["submitted_by_org"],
                                kind=ev["kind"],
                                achievement=ev["achievement"],
                                metrics=ev["metrics"],
                                adopted=ev["adopted"],
                                locked=locked,
                            )
                        )
        return hits

    def payment(self, payment_id: str) -> tuple[dm.ApplicationState, dict[str, Any]] | None:
        for app in self.list_applications():
            pay = app.payments.get(payment_id)
            if pay is not None:
                return app, pay
        return None

    def applications_for_org(self, org_id: str) -> list[dm.ApplicationState]:
        return [
            app
            for app in self.list_applications()
            if org_id in app.all_orgs()
        ]
