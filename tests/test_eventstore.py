"""事件存储：哈希链、乐观锁、幂等、JSONL 持久化与篡改检测。"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.custom_rules

from cultural_fund.errors import ConcurrencyError
from cultural_fund.eventstore import (
    Actor,
    InMemoryEventStore,
    JsonlEventStore,
    PendingEvent,
)


@pytest.fixture
def actor():
    return Actor("u1", "authority")


def test_events_are_append_only_with_hash_chain(store, actor):
    store.append("rule", "R", [PendingEvent("RulePublished", {"v": 1})], actor, "2026-01-01T00:00:00+00:00")
    store.append("rule", "R", [PendingEvent("RulePublished", {"v": 2})], actor, "2026-02-01T00:00:00+00:00",
                 expected_version=1)
    events = store.load_stream("rule", "R")
    assert [e.version for e in events] == [1, 2]
    assert events[0].prev_hash == "0" * 64
    assert events[1].prev_hash == events[0].hash
    store.verify_chain()


def test_optimistic_concurrency_conflict(store, actor):
    store.append("rule", "R", [PendingEvent("RulePublished", {"v": 1})], actor, "2026-01-01T00:00:00+00:00")
    with pytest.raises(ConcurrencyError):
        store.append("rule", "R", [PendingEvent("RulePublished", {"v": 2})], actor,
                     "2026-02-01T00:00:00+00:00", expected_version=0)


def test_idempotent_append_returns_first_event(store, actor):
    first = store.append("application", "a1",
                         [PendingEvent("PaymentAttempted", {"result": "failed"}, idempotency_key="pay-k-1")],
                         actor, "2026-01-01T00:00:00+00:00")
    replay = store.append("application", "a1",
                          [PendingEvent("PaymentAttempted", {"result": "succeeded"}, idempotency_key="pay-k-1")],
                          actor, "2026-01-01T00:01:00+00:00", expected_version=1)
    assert replay[0].event_id == first[0].event_id
    assert replay[0].data["result"] == "failed"  # 保留首次事实，不被重放覆盖
    assert len(store.load_stream("application", "a1")) == 1


def test_commit_is_atomic_across_streams(store, actor):
    store.append("project", "P1", [PendingEvent("ProjectRegistered", {})], actor, "2026-01-01T00:00:00+00:00")
    atom_ok = store.WriteAtom("project", "P2", [PendingEvent("ProjectRegistered", {})], expected_version=0)
    atom_bad = store.WriteAtom("project", "P1", [PendingEvent("ProjectMerged", {})], expected_version=99)
    with pytest.raises(ConcurrencyError):
        store.commit([atom_ok, atom_bad], actor, "2026-01-02T00:00:00+00:00")
    # 整批回滚：P2 也不存在
    assert store.load_stream("project", "P2") == []


def test_jsonl_persistence_replay_and_tamper_detection(tmp_path, actor):
    path = tmp_path / "events.jsonl"
    store = JsonlEventStore(path)
    store.append("rule", "R", [PendingEvent("RulePublished", {"stage_cap": "100"})], actor,
                 "2026-01-01T00:00:00+00:00")
    store.append("rule", "R", [PendingEvent("RulePublished", {"stage_cap": "200"})], actor,
                 "2026-02-01T00:00:00+00:00", expected_version=1)

    reloaded = JsonlEventStore(path)
    reloaded.verify_chain()
    assert [e.version for e in reloaded.load_stream("rule", "R")] == [1, 2]

    # 篡改旧行金额 -> 哈希链立即断裂
    lines = path.read_text(encoding="utf-8").splitlines()
    obj = json.loads(lines[0])
    obj["data"]["stage_cap"] = "999999"
    tampered = tmp_path / "tampered.jsonl"
    tampered.write_text(json.dumps(obj, ensure_ascii=False) + "\n" + lines[1] + "\n", encoding="utf-8")
    with pytest.raises(AssertionError):
        JsonlEventStore(tampered).verify_chain()


def test_jsonl_idempotent_retry_does_not_duplicate_line(tmp_path, actor):
    path = tmp_path / "events.jsonl"
    store = JsonlEventStore(path)
    payload = [PendingEvent("PaymentAttempted", {"result": "failed"}, idempotency_key="k1")]
    store.append("application", "a1", payload, actor, "2026-01-01T00:00:00+00:00")
    store.append("application", "a1", payload, actor, "2026-01-01T00:01:00+00:00", expected_version=1)
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
