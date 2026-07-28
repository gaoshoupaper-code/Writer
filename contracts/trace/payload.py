"""受治理的 Trace 正文载荷。

这个模块刻意不依赖 executor 或 evolution。两端在写入任何完整业务内容前都用同一
PayloadGate，随后把正文落到各自的本地内容寻址存储，事件只保留 PayloadRef。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import TracePayloadRef


class PayloadRejected(ValueError):
    """载荷含有 Writer 明确禁止进入 Trace 的内容。"""


_FORBIDDEN_KEY_PARTS = (
    "authorization", "cookie", "secret", "password", "api_key", "apikey",
    "access_token", "refresh_token", "environment", "env", "embedding",
    "chain_of_thought", "cot", "reasoning", "thinking",
)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----"),
)
_STRUCTURAL_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|cookie|secret|password|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token)\b\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)


@dataclass(frozen=True)
class PreparedPayload:
    canonical_json: bytes
    content_hash: str


class PayloadGate:
    """固定白名单的载荷检查器。

    未知复杂对象和禁止字段一律拒绝整个语义载荷，而不是尝试猜测如何保留它。这避免
    ``Authorization``、CoT 或 embedding 在任意嵌套位置漏网。
    """

    def prepare(self, value: Any) -> PreparedPayload:
        normalized = self._normalize(value)
        encoded = json.dumps(
            normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return PreparedPayload(encoded, hashlib.sha256(encoded).hexdigest())

    def _normalize(self, value: Any) -> Any:
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, str):
            if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
                raise PayloadRejected("secret-like value")
            return value
        if isinstance(value, bytes | bytearray | memoryview):
            raise PayloadRejected("binary payload")
        if isinstance(value, list | tuple):
            return [self._normalize(item) for item in value]
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                lowered = key_text.lower().replace("-", "_")
                if any(part in lowered for part in _FORBIDDEN_KEY_PARTS):
                    raise PayloadRejected(f"forbidden field: {key_text}")
                normalized[key_text] = self._normalize(item)
            return normalized
        raise PayloadRejected(f"unsupported payload type: {type(value).__name__}")


def sanitize_structural_text(value: Any, *, max_length: int = 500) -> str | None:
    """Keep diagnostic metadata useful without allowing secrets into structural fields."""
    if value is None:
        return None
    text = str(value)
    if "-----BEGIN " in text and " PRIVATE KEY-----" in text:
        return "[redacted secret]"
    text = _STRUCTURAL_SECRET_ASSIGNMENT.sub(r"\1=[redacted]", text)
    for pattern in _SECRET_PATTERNS[:2]:
        text = pattern.sub("[redacted]", text)
    if len(text) > max_length:
        return f"{text[:max_length]}..."
    return text


class ContentAddressedPayloadStore:
    """本地原子写入的 JSON Payload 存储。"""

    def __init__(self, root: Path, retention_days: int = 90) -> None:
        self.root = root
        self.retention_days = retention_days
        self.gate = PayloadGate()

    def put(self, value: Any, *, kind: str = "semantic_full") -> TracePayloadRef:
        prepared = self.gate.prepare(value)
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{prepared.content_hash}.json"
        if not target.exists():
            temporary = self.root / f".{uuid4().hex}.tmp"
            try:
                temporary.write_bytes(prepared.canonical_json)
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != prepared.content_hash:
                    raise OSError("payload readback hash mismatch")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        expires_at = datetime.now(UTC) + timedelta(days=self.retention_days)
        return TracePayloadRef(
            payload_id=prepared.content_hash,
            content_hash=prepared.content_hash,
            kind=kind,  # type: ignore[arg-type]
            size_bytes=len(prepared.canonical_json),
            expires_at=expires_at.isoformat(),
        )

    def get(self, payload_id: str) -> Any:
        path = self.root / f"{payload_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def delete(self, payload_id: str) -> None:
        (self.root / f"{payload_id}.json").unlink(missing_ok=True)
