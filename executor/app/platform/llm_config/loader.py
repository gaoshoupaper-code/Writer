"""LLM 配置 loader —— 从 evolution 拉取激活的大模型 API 配置。

职责：
  - 从 evolution GET /api/ingestion/active-key 拉取激活配置的明文三元组
    (api_key, base_url, model)
  - 进程内 TTL 缓存（避免每次 build_writer_model 都内网往返）
  - 本地文件缓存（evolution 不可达时降级）
  - evolution 未配置/不可用/未配置 LLM 时返回 None（交由 build_writer_model 走环境变量降级）

设计依据：与 app/platform/prompt/loader.py 同构（拉取+缓存+降级范式）。
区别：GET 带 X-Notify-Token 鉴权（本端点返回明文 API key，比 prompt 敏感），
且多一层进程内 TTL 缓存（LLM 配置每次构建 model 都要读，不能每次走 HTTP）。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("writer.llm_config_loader")

# evolution 端点路径（挂在 /api/ingestion/ 下，受 NotifyTokenMiddleware 的 X-Notify-Token 保护）
_ACTIVE_KEY_PATH = "/api/ingestion/active-key"

# 进程内缓存 TTL（秒）：LLM 配置变更不频繁，60s 内复用拉取结果。
# 配合本地文件降级缓存：evolution 改配置后最多 60s 生效（方式 A，见设计文档）。
_CACHE_TTL_SECONDS = 60.0


@dataclass
class LlmConfig:
    """拉取到的激活 LLM 配置（明文，含 API key）。"""

    api_key: str
    base_url: str
    model: str


class LlmConfigLoader:
    """激活 LLM 配置加载器。

    优先级：进程内缓存（TTL 内）> evolution（远程）> 本地文件缓存 > None。
    全程静默降级：任何失败都不抛异常，返回 None 让 build_writer_model 走环境变量。
    """

    def __init__(
        self,
        evolution_url: str = "",
        evolution_notify_token: str = "",
        cache_dir: str = ".llm_config_cache",
    ) -> None:
        self._evolution_url = evolution_url.rstrip("/")
        self._notify_token = evolution_notify_token
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        # 进程内缓存：(LlmConfig, 过期时间戳)；None 表示已查过但无配置（避免重复打 HTTP）
        self._cached: tuple[LlmConfig, float] | None = None

    def mark_stale(self) -> None:
        """标记缓存失效（下次 get 强制重拉 evolution）。"""
        self._cached = None

    def get(self) -> LlmConfig | None:
        """获取激活 LLM 配置。

        Returns:
            LlmConfig 明文三元组；未配置/拉取失败返回 None（降级）。
        """
        # 1. 进程内缓存命中（TTL 内）
        if self._cached is not None:
            config, expires_at = self._cached
            if time.time() < expires_at:
                return config

        # 2. 从 evolution 拉取
        if self._evolution_url:
            config = self._fetch_from_evolution()
            if config is not None:
                self._save_cache(config)
                self._cached = (config, time.time() + _CACHE_TTL_SECONDS)
                return config

        # 3. 降级：读本地文件缓存
        cached = self._load_cache()
        if cached is not None:
            logger.warning("LLM 配置从本地缓存降级加载（evolution 不可用或未配置）")
            return cached

        # 4. 都没有：返回 None（build_writer_model 走环境变量/占位降级）
        return None

    def _fetch_from_evolution(self) -> LlmConfig | None:
        """从 evolution HTTP 拉取激活配置明文。失败返回 None（触发降级）。"""
        try:
            import httpx

            url = f"{self._evolution_url}{_ACTIVE_KEY_PATH}"
            # 明文 key 端点必须带鉴权（与 prompt loader 不带 token 的区别）
            headers = {"X-Notify-Token": self._notify_token} if self._notify_token else None
            resp = httpx.get(url, timeout=3.0, headers=headers)
            if resp.status_code == 404:
                # evolution 还没配 LLM（用户没在桌面端填 key）
                logger.warning("evolution 未配置激活的 LLM 配置（404）")
                return None
            resp.raise_for_status()
            data = resp.json()
            return LlmConfig(
                api_key=data["api_key"],
                base_url=data["base_url"],
                model=data["model"],
            )
        except Exception as exc:
            logger.warning("从 evolution 拉 LLM 配置失败，降级：%s", exc)
            return None

    def _cache_path(self) -> Path:
        return self._cache_dir / "active_llm_config.json"

    def _save_cache(self, config: LlmConfig) -> None:
        """保存到本地文件缓存（evolution 不可达时降级用）。"""
        try:
            path = self._cache_path()
            path.write_text(
                json.dumps(
                    {
                        "api_key": config.api_key,
                        "base_url": config.base_url,
                        "model": config.model,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass  # 缓存写入失败不影响主流程

    def _load_cache(self) -> LlmConfig | None:
        """从本地文件缓存读取。"""
        path = self._cache_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return LlmConfig(
                api_key=data["api_key"],
                base_url=data["base_url"],
                model=data["model"],
            )
        except (OSError, json.JSONDecodeError, KeyError):
            return None


# ── 模块级单例 ──

_loader: LlmConfigLoader | None = None


def get_loader() -> LlmConfigLoader:
    """获取全局 LlmConfigLoader 单例（首次调用时从 settings 初始化）。"""
    global _loader
    if _loader is None:
        from app.platform.core.settings import get_settings

        s = get_settings()
        _loader = LlmConfigLoader(
            evolution_url=s.evolution_url,
            evolution_notify_token=s.evolution_notify_token,
            cache_dir=s.llm_config_cache_dir,
        )
    return _loader


def get_active_llm_config() -> LlmConfig | None:
    """便捷函数：获取激活 LLM 配置（供 build_writer_model 调用）。"""
    try:
        return get_loader().get()
    except Exception as exc:
        # loader 内部已降级，这里再兜一层（防止初始化失败拖垮 model 构建）
        logger.warning("LLM 配置加载异常，降级到环境变量：%s", exc)
        return None
