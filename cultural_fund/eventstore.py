"""仅追加（append-only）事件存储。

特性：
- 每个事件属于一个聚合流（aggregate_id + version 乐观锁）；
- 全局序列号 seq 与哈希链（prev_hash/hash）使任何事后删改都可被检出；
- 幂等键（idempotency_key）保证失败重试（如支付重试）不会产生重复事件；
- 内存后端用于测试，JSONL 后端把日志逐行落盘，旧行永不重写。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import ConcurrencyError

SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64


def canonical_dumps(obj: Any) -> str:
    """确定性 JSON 序列化：排序键、无空白、非 ASCII 原样输出。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class Actor:
    """发起命令的主体快照（写入事件，便于审计还原“谁做的”）。"""

    user_id: str
    role: str
    org_id: str | None = None
    display_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "role": self.role,
            "org_id": self.org_id,
            "display_name": self.display_name,
        }


@dataclass(frozen=True)
class Event:
    seq: int
    event_id: str
    stream_id: str
    aggregate_type: str
    aggregate_id: str
    version: int
    event_type: str
    data: dict[str, Any]
    actor: dict[str, Any]
    occurred_at: str
    idempotency_key: str | None = None
    causation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    prev_hash: str = GENESIS_HASH
    hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "stream_id": self.stream_id,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "version": self.version,
            "event_type": self.event_type,
            "data": self.data,
            "actor": self.actor,
            "occurred_at": self.occurred_at,
            "idempotency_key": self.idempotency_key,
            "causation_id": self.causation_id,
            "metadata": self.metadata,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "schema": SCHEMA_VERSION,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        return cls(
            seq=raw["seq"],
            event_id=raw["event_id"],
            stream_id=raw["stream_id"],
            aggregate_type=raw["aggregate_type"],
            aggregate_id=raw["aggregate_id"],
            version=raw["version"],
            event_type=raw["event_type"],
            data=raw.get("data", {}),
            actor=raw.get("actor", {}),
            occurred_at=raw["occurred_at"],
            idempotency_key=raw.get("idempotency_key"),
            causation_id=raw.get("causation_id"),
            metadata=raw.get("metadata", {}),
            prev_hash=raw.get("prev_hash", GENESIS_HASH),
            hash=raw.get("hash", ""),
        )


@dataclass(frozen=True)
class PendingEvent:
    """尚未落盘的事件（seq/version/hash 由存储在提交时分配）。"""

    event_type: str
    data: dict[str, Any]
    idempotency_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def compute_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical_dumps(payload)).encode("utf-8")).hexdigest()


class EventStore:
    """事件存储抽象；线程安全，进程内单写者语义由锁保证。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._stream_versions: dict[str, int] = {}
        # idempotency_key -> 首次提交产生的事件 event_id
        self._idempotency_index: dict[str, str] = {}
        self._event_index: dict[str, Event] = {}

    # ---- 读取 ----------------------------------------------------------

    def load_stream(self, aggregate_type: str, aggregate_id: str) -> list[Event]:
        stream_id = f"{aggregate_type}:{aggregate_id}"
        with self._lock:
            return [e for e in self._events if e.stream_id == stream_id]

    def load_all(self, after_seq: int = 0) -> list[Event]:
        with self._lock:
            return [e for e in self._events if e.seq > after_seq]

    def get(self, event_id: str) -> Event | None:
        with self._lock:
            return self._event_index.get(event_id)

    def stream_version(self, aggregate_type: str, aggregate_id: str) -> int:
        stream_id = f"{aggregate_type}:{aggregate_id}"
        with self._lock:
            return self._stream_versions.get(stream_id, 0)

    def all_events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    # ---- 写入 ----------------------------------------------------------

    @dataclass(frozen=True)
    class WriteAtom:
        aggregate_type: str
        aggregate_id: str
        events: list["PendingEvent"]
        expected_version: int | None = None

    def commit(
        self,
        atoms: list["EventStore.WriteAtom"],
        actor: Actor,
        occurred_at: str,
        causation_id: str | None = None,
    ) -> list[Event]:
        """原子提交跨多个聚合流的一批事件（单存储、全局锁、单哈希链）。

        任一版本冲突则整批不落盘；首个幂等键命中时返回首次提交的既有事件。
        用于“争议裁决同时更新多个申报流”这类跨聚合一致性命令。
        """
        atoms = [a for a in atoms if a.events]
        if not atoms:
            return []

        with self._lock:
            for atom in atoms:
                stream_id = f"{atom.aggregate_type}:{atom.aggregate_id}"
                current_version = self._stream_versions.get(stream_id, 0)
                if atom.expected_version is not None and atom.expected_version != current_version:
                    raise ConcurrencyError(
                        f"流 {stream_id} 版本冲突：期望 {atom.expected_version}，实际 {current_version}"
                    )

            first_key = atoms[0].events[0].idempotency_key
            if first_key is not None and first_key in self._idempotency_index:
                return [self._event_index[self._idempotency_index[first_key]]]

            committed: list[Event] = []
            prev_hash = self._events[-1].hash if self._events else GENESIS_HASH
            next_seq = self._events[-1].seq + 1 if self._events else 1
            stream_versions_after = dict(self._stream_versions)

            for atom in atoms:
                stream_id = f"{atom.aggregate_type}:{atom.aggregate_id}"
                version = stream_versions_after.get(stream_id, 0)
                for item in atom.events:
                    version += 1
                    event_id = item.metadata.pop("_event_id", None) or _new_event_id()
                    payload = {
                        "event_id": event_id,
                        "stream_id": stream_id,
                        "aggregate_type": atom.aggregate_type,
                        "aggregate_id": atom.aggregate_id,
                        "version": version,
                        "event_type": item.event_type,
                        "data": item.data,
                        "actor": actor.as_dict(),
                        "occurred_at": occurred_at,
                        "idempotency_key": item.idempotency_key,
                        "causation_id": causation_id,
                        "metadata": item.metadata,
                    }
                    digest = compute_hash(prev_hash, payload)
                    event = Event(seq=next_seq, prev_hash=prev_hash, hash=digest, **payload)
                    committed.append(event)
                    prev_hash = digest
                    next_seq += 1
                    if item.idempotency_key is not None:
                        self._idempotency_index.setdefault(item.idempotency_key, event_id)
                stream_versions_after[stream_id] = version

            # 全部事件构造成功后再一次性生效
            for e in committed:
                self._events.append(e)
                self._event_index[e.event_id] = e
            self._stream_versions.update(stream_versions_after)
            self._persist(committed)
            return committed

    def append(
        self,
        aggregate_type: str,
        aggregate_id: str,
        pending: Iterable[PendingEvent],
        actor: Actor,
        occurred_at: str,
        expected_version: int | None = None,
        event_id_factory=lambda: None,
        causation_id: str | None = None,
    ) -> list[Event]:
        """原子提交一批事件。

        expected_version 为乐观锁：None 表示不检查（调用方自行保证）。
        若批次中首个事件携带已存在的幂等键，则整批不重复落盘，
        直接返回该幂等键对应的既有事件（支付重试安全闭环的基础）。
        """
        pending_list = list(pending)
        if not pending_list:
            return []
        stream_id = f"{aggregate_type}:{aggregate_id}"

        with self._lock:
            current_version = self._stream_versions.get(stream_id, 0)
            if expected_version is not None and expected_version != current_version:
                raise ConcurrencyError(
                    f"流 {stream_id} 版本冲突：期望 {expected_version}，实际 {current_version}"
                )

            first_key = pending_list[0].idempotency_key
            if first_key is not None and first_key in self._idempotency_index:
                existing = self._event_index[self._idempotency_index[first_key]]
                return [existing]

            committed: list[Event] = []
            prev_hash = self._events[-1].hash if self._events else GENESIS_HASH
            next_seq = self._events[-1].seq + 1 if self._events else 1
            version = current_version

            for item in pending_list:
                version += 1
                event_id = item.metadata.pop("_event_id", None) or _new_event_id()
                payload = {
                    "event_id": event_id,
                    "stream_id": stream_id,
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                    "version": version,
                    "event_type": item.event_type,
                    "data": item.data,
                    "actor": actor.as_dict(),
                    "occurred_at": occurred_at,
                    "idempotency_key": item.idempotency_key,
                    "causation_id": causation_id,
                    "metadata": item.metadata,
                }
                digest = compute_hash(prev_hash, payload)
                event = Event(
                    seq=next_seq,
                    prev_hash=prev_hash,
                    hash=digest,
                    **payload,
                )
                committed.append(event)
                self._events.append(event)
                self._event_index[event_id] = event
                if item.idempotency_key is not None:
                    # 同一幂等键重复提交时返回首次事件
                    self._idempotency_index.setdefault(item.idempotency_key, event_id)
                prev_hash = digest
                next_seq += 1

            self._stream_versions[stream_id] = version
            self._persist(committed)
            return committed

    def _persist(self, fresh_events: list["Event"]) -> None:
        """持久化钩子：内存存储为空操作；JSONL 后端覆写为落盘（在存储锁内调用）。"""
        return

    # ---- 校验 ----------------------------------------------------------

    def verify_chain(self) -> None:
        """重放全链哈希；任何篡改、缺行、乱序都抛出 AssertionError。"""
        with self._lock:
            prev = GENESIS_HASH
            for e in self._events:
                payload = {
                    "event_id": e.event_id,
                    "stream_id": e.stream_id,
                    "aggregate_type": e.aggregate_type,
                    "aggregate_id": e.aggregate_id,
                    "version": e.version,
                    "event_type": e.event_type,
                    "data": e.data,
                    "actor": e.actor,
                    "occurred_at": e.occurred_at,
                    "idempotency_key": e.idempotency_key,
                    "causation_id": e.causation_id,
                    "metadata": e.metadata,
                }
                expected = compute_hash(prev, payload)
                assert e.hash == expected, f"事件 {e.event_id} 哈希不匹配，日志可能被篡改"
                assert e.prev_hash == prev, f"事件 {e.event_id} 前驱哈希断裂"
                prev = e.hash


def _new_event_id() -> str:
    import uuid

    return f"evt-{uuid.uuid4().hex}"


class InMemoryEventStore(EventStore):
    """进程内事件存储。"""


class JsonlEventStore(EventStore):
    """JSON Lines 持久化事件存储：每行一个事件，文件只追加不重写。"""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._reload()

    def _reload(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            event = Event.from_dict(json.loads(line))
            # 直接走父类 append 的底层登记，但不重复写盘：采用旁路装载
            self._ingest_loaded(event)

    def _ingest_loaded(self, event: Event) -> None:
        with self._lock:
            self._events.append(event)
            self._event_index[event.event_id] = event
            self._stream_versions[event.stream_id] = event.version
            if event.idempotency_key:
                self._idempotency_index.setdefault(event.idempotency_key, event.event_id)
            self._events.sort(key=lambda e: e.seq)

    def _persist(self, fresh_events: list["Event"]) -> None:
        # append/commit 均在持有存储锁时调用本钩子，多线程下写盘顺序与哈希链一致
        with self.path.open("a", encoding="utf-8") as fh:
            for e in fresh_events:
                fh.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
            fh.flush()

    def verify_chain(self) -> None:
        # 先校验内存，再校验文件与内存一致（行数、逐行哈希）
        super().verify_chain()
        if not self.path.exists():
            return
        disk = [
            Event.from_dict(json.loads(line))
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        with self._lock:
            assert len(disk) == len(self._events), "磁盘事件行数与内存不一致"
            for d, m in zip(disk, self._events):
                assert d.hash == m.hash, f"磁盘事件 {d.event_id} 与内存不一致"
