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


# 内部推理字段（DEC-001 / FR-001）：模型私密推理不属于业务证据，定向剥离而非整包拒绝。
# 命中这些键时删除该键值对并继续规范化剩余结构，保证业务正文 100% 保留。
_INTERNAL_REASONING_KEY_PARTS = (
    "chain_of_thought", "cot", "reasoning", "thinking",
)

# 敏感凭据/配置字段（CON-002）：值或结构可能泄露认证材料，必须 fail-closed 整包拒绝。
# 不得因"删字段后尽量保存"弱化安全边界——与内部推理采用不同策略。
_SECRET_KEY_PARTS = (
    "authorization", "cookie", "secret", "password", "api_key", "apikey",
    "access_token", "refresh_token", "environment", "env", "embedding",
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
    # 定向剥离了哪些内部推理键（去重后的原始键名集合，不含值）。
    # 不含私密原文，仅作为 recorder 的诊断元数据——用于证明"业务正文保留，
    # 推理已剥离"，而非把剥离当降级（DEC-001 / FR-001）。
    stripped_reasoning_keys: tuple[str, ...] = ()


class PayloadGate:
    """固定白名单的载荷检查器。

    内部推理字段（reasoning/CoT/thinking）定向剥离后保留业务正文（DEC-001）；
    敏感凭据、密钥、未知复杂对象仍一律整包拒绝，避免在任意嵌套位置漏网（CON-002）。
    两者风险性质不同，必须采用不同策略。
    """

    def prepare(self, value: Any) -> PreparedPayload:
        normalized, stripped = self._normalize(value)
        encoded = json.dumps(
            normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        # 剥离后去重并稳定排序，保证同一输入的 strip_report 确定。
        unique_stripped = tuple(sorted(set(stripped))) if stripped else ()
        return PreparedPayload(
            encoded,
            hashlib.sha256(encoded).hexdigest(),
            unique_stripped,
        )

    def _normalize(self, value: Any) -> tuple[Any, list[str]]:
        """规范化载荷，返回 (规范化值, 被剥离的内部推理键列表)。

        - 内部推理键：删除并继续规范化剩余结构（业务正文保留）。
        - 敏感凭据键 / 密钥模式 / 二进制 / 不支持类型：raise PayloadRejected（fail-closed）。
        - 剥离推理后剩余结构仍会递归检测密钥（EDGE-001：先剥离 reasoning，
          剩余命中密钥规则后必须拒绝该语义载荷）。
        """
        if value is None or isinstance(value, bool | int | float):
            return value, []
        if isinstance(value, str):
            if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
                raise PayloadRejected("secret-like value")
            return value, []
        if isinstance(value, bytes | bytearray | memoryview):
            raise PayloadRejected("binary payload")
        if isinstance(value, list | tuple):
            stripped_all: list[str] = []
            items: list[Any] = []
            for item in value:
                normalized_item, stripped = self._normalize(item)
                items.append(normalized_item)
                stripped_all.extend(stripped)
            return items, stripped_all
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            stripped_keys: list[str] = []
            for key, item in value.items():
                key_text = str(key)
                lowered = key_text.lower().replace("-", "_")
                if any(part in lowered for part in _INTERNAL_REASONING_KEY_PARTS):
                    # 定向剥离：跳过该键值对，记录键名（不含值）供诊断。
                    stripped_keys.append(key_text)
                    continue
                if any(part in lowered for part in _SECRET_KEY_PARTS):
                    raise PayloadRejected(f"forbidden field: {key_text}")
                normalized_child, stripped_child = self._normalize(item)
                normalized[key_text] = normalized_child
                stripped_keys.extend(stripped_child)
            return normalized, stripped_keys
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
