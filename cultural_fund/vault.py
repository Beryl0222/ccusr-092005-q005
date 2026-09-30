"""敏感合同密件保管库。

事件日志与普通查询里只出现 secret_id，合同正文绝不进入事件流；
只有通过实际审核者鉴权后才按 secret_id 取回明文。生产实现应替换为
KMS 信封加密，这里给出接口与落盘格式（base64 封装 + 用途标记）。
"""

from __future__ import annotations

import base64
import dataclasses
import json
import secrets as _secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _encode(plaintext: dict[str, Any]) -> str:
    raw = json.dumps(plaintext, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _decode(blob: str) -> dict[str, Any]:
    return json.loads(base64.b64decode(blob.encode("ascii")).decode("utf-8"))


@dataclass(frozen=True)
class StoredSecret:
    secret_id: str
    title: str
    application_id: str
    evidence_id: str
    created_by: str


class SecretVault:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._blobs: dict[str, str] = {}
        self._meta: dict[str, StoredSecret] = {}

    def store(
        self,
        plaintext: dict[str, Any],
        *,
        title: str,
        application_id: str,
        evidence_id: str,
        created_by: str,
    ) -> str:
        with self._lock:
            secret_id = f"secret-{_secrets.token_hex(8)}"
            self._blobs[secret_id] = _encode(plaintext)
            self._meta[secret_id] = StoredSecret(
                secret_id=secret_id,
                title=title,
                application_id=application_id,
                evidence_id=evidence_id,
                created_by=created_by,
            )
            return secret_id

    def reveal(self, secret_id: str) -> dict[str, Any]:
        with self._lock:
            blob = self._blobs.get(secret_id)
            if blob is None:
                raise KeyError(secret_id)
            return _decode(blob)

    def meta(self, secret_id: str) -> StoredSecret | None:
        with self._lock:
            return self._meta.get(secret_id)


class JsonFileSecretVault(SecretVault):
    """可选的简单落盘保管库（文件权限 600）。"""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for secret_id, item in raw.items():
                self._blobs[secret_id] = item["blob"]
                m = item["meta"]
                self._meta[secret_id] = StoredSecret(**m)

    def store(self, plaintext: dict[str, Any], **kwargs: Any) -> str:
        secret_id = super().store(plaintext, **kwargs)
        self._flush()
        return secret_id

    def _flush(self) -> None:
        raw = {
            sid: {"blob": self._blobs[sid], "meta": dataclasses_asdict(m)}
            for sid, m in self._meta.items()
        }
        self.path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        self.path.chmod(0o600)


def dataclasses_asdict(obj: Any) -> dict[str, Any]:
    import dataclasses

    return dataclasses.asdict(obj)
