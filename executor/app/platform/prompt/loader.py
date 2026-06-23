"""统一 prompt loader（Phase 5 T9/T10/T13）。

职责：
  - 按 name + label 从 evolution 拉 prompt（source of truth）
  - 本地缓存（evolution 不可用时降级读缓存，T10 调和方案）
  - evolution 未配置/不可用时降级读执行端 .md 文件（向后兼容）
  - 拉取时把 version/label 暴露给调用方（T13：写进 trace）

设计依据：T10（evolution主+执行端缓存）、T13（版本进trace）。
"""

from __future__ import annotations

import contextvars
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("writer.prompt_loader")

PRODUCTION = "production"

# ── A/B 回放 prompt label override（Phase 3 T3.1）──
# contextvar：回放端点 set 此 var，生成链路所有 load_prompt 自动用该 label，
# 实现回放时用 candidate prompt 跑（无需改各 build_*_subagent）。
# 未 set（None）→ 用调用方传入的 label（默认 production），行为不变。
# contextvar 随 asyncio task 传播：回放在独立 task 内 set，不影响其他请求。
_prompt_label_override: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "prompt_label_override", default=None
)


def set_prompt_label_override(label: str | None) -> contextvars.Token[str | None]:
    """设置 prompt label override（供 A/B 回放端点用）。

    返回 token，调用方应在 finally 里 reset_prompt_label_override(token) 复位，
    避免泄漏到后续请求（虽 contextvar 随 task 隔离，显式复位更安全）。

    用法：
        token = set_prompt_label_override("candidate")
        try:
            await generate_stream(...)
        finally:
            reset_prompt_label_override(token)
    """
    return _prompt_label_override.set(label)


def reset_prompt_label_override(token: contextvars.Token[str | None]) -> None:
    """复位 prompt label override。"""
    _prompt_label_override.reset(token)


@dataclass
class PromptContent:
    """拉取到的 prompt 内容（含版本元数据，供 T13 写进 trace）。"""

    name: str
    content: str
    version: int
    label: str
    source: str  # "evolution" / "cache" / "local_file"（降级来源）


class PromptLoader:
    """统一 prompt 加载器。

    优先级：evolution（远程）> 本地缓存 > 执行端 .md 文件（降级）。
    降级是静默的：evolution 不可用不影响 agent 运行。

    Phase 5（D7 方案B）：支持 stale 标记——evolution 上线新版本时通知执行端，
    执行端标记对应缓存为 stale，下次 get() 强制重拉 evolution（不读旧缓存）。
    """

    def __init__(
        self,
        evolution_url: str = "",
        cache_dir: str = ".prompt_cache",
    ) -> None:
        self._evolution_url = evolution_url.rstrip("/")
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        # stale 标记集合：被标记的 (name, label) 下次 get() 跳过缓存直接拉 evolution。
        # 由 mark_stale() 设置（POST /internal/prompts/refreshed 端点调用）。
        self._stale: set[tuple[str, str]] = set()

    def mark_stale(self, name: str, label: str = PRODUCTION) -> None:
        """标记某 prompt 的缓存为 stale（evolution 通知有新版本时调用）。

        幂等：重复标记无害。下次 get() 会强制重拉 evolution。
        """
        self._stale.add((name, label))

    def get(self, name: str, label: str = PRODUCTION) -> PromptContent:
        """按 name + label 加载 prompt。

        降级链：evolution → 缓存 → 报错（无本地 .md 兜底，prompt 必须存在于 evolution）。

        stale 行为：若 (name, label) 被 mark_stale 标记，跳过缓存直接拉 evolution。
        拉取成功后清除 stale 标记；拉取失败仍降级读缓存（保证可用性）。
        """
        is_stale = (name, label) in self._stale
        # 1. 尝试 evolution（远程）——stale 时强制拉，非 stale 时也优先拉
        if self._evolution_url:
            content = self._fetch_from_evolution(name, label)
            if content is not None:
                self._save_cache(name, label, content)
                self._stale.discard((name, label))  # 拉取成功，清 stale
                return content

        # 2. 降级：读缓存（stale 时也降级——拉不到新版至少保证可用性）
        cached = self._load_cache(name, label)
        if cached is not None:
            if is_stale:
                logger.warning("prompt %s stale 但 evolution 不可用，用旧缓存降级", name)
            else:
                logger.warning("prompt %s 从缓存降级加载（evolution 不可用）", name)
            return cached

        # 3. 都没有：报错（prompt 必须存在于 evolution，运行前应导入）
        raise FileNotFoundError(
            f"Prompt '{name}' (label={label}) 未找到：evolution 未配置或不可用，且无本地缓存。"
            f" 请运行 evolution 的 python -m app.prompt_import 导入 prompt。"
        )

    def _fetch_from_evolution(self, name: str, label: str) -> PromptContent | None:
        """从 evolution HTTP 拉取。失败返回 None（触发降级）。"""
        try:
            import httpx

            url = f"{self._evolution_url}/api/prompts/{name}?label={label}"
            resp = httpx.get(url, timeout=3.0)
            if resp.status_code == 404:
                logger.warning("prompt %s 在 evolution 中不存在", name)
                return None
            resp.raise_for_status()
            data = resp.json()
            return PromptContent(
                name=data["name"],
                content=data["content"],
                version=data["version"],
                label=label,
                source="evolution",
            )
        except Exception as exc:
            logger.warning("从 evolution 拉 prompt %s 失败，降级：%s", name, exc)
            return None

    def _cache_path(self, name: str, label: str) -> Path:
        return self._cache_dir / f"{name}__{label}.json"

    def _save_cache(self, name: str, label: str, content: PromptContent) -> None:
        """保存到本地缓存（JSON，含版本元数据）。"""
        try:
            path = self._cache_path(name, label)
            path.write_text(
                json.dumps(
                    {
                        "name": content.name,
                        "content": content.content,
                        "version": content.version,
                        "label": content.label,
                        "source": content.source,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass  # 缓存写入失败不影响主流程

    def _load_cache(self, name: str, label: str) -> PromptContent | None:
        """从本地缓存读取。"""
        path = self._cache_path(name, label)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return PromptContent(
                name=data["name"],
                content=data["content"],
                version=data["version"],
                label=data["label"],
                source="cache",
            )
        except (OSError, json.JSONDecodeError, KeyError):
            return None


# ── 模块级单例 ──

_loader: PromptLoader | None = None


def get_loader() -> PromptLoader:
    """获取全局 PromptLoader 单例（首次调用时从 settings 初始化）。"""
    global _loader
    if _loader is None:
        from app.platform.core.settings import get_settings

        s = get_settings()
        _loader = PromptLoader(
            evolution_url=s.evolution_url,
            cache_dir=s.prompt_cache_dir,
        )
    return _loader


def load_prompt(name: str, label: str = PRODUCTION) -> PromptContent:
    """便捷函数：加载 prompt（供 agent 构建时调用，替代 Path.read_text）。

    A/B 回放 override（T3.1）：若 contextvar _prompt_label_override 被 set，
    则用 override label 替代调用方传入的 label。普通生成不受影响。
    """
    override = _prompt_label_override.get()
    effective_label = override if override is not None else label
    return get_loader().get(name, effective_label)
