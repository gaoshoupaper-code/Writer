"""大模型 API 配置接口（桌面化改造，2026-07-07）。

桌面端唯一配置入口：
- GET  /api/config/llm       读当前配置（不回显 key）
- PUT  /api/config/llm       保存配置（key 加密存 llm_config 表）
- POST /api/config/llm/test  测试连通性（用提交的临时 key 发 ping，不落库）

鉴权由 SSOAuthMiddleware 统一处理（白名单用户才能访问）。
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core import db

logger = logging.getLogger("evolution.config")

router = APIRouter(prefix="/config", tags=["config"])


# ── Schemas ────────────────────────────────────────────────


class LlmConfigOut(BaseModel):
    """GET 返回：不回显 key，只标 has_key。"""
    has_key: bool
    name: str | None = None
    base_url: str = ""
    model: str = ""
    updated_at: str | None = None


class LlmConfigIn(BaseModel):
    """PUT 入参：完整 key+base_url+model。"""
    name: str = "default"
    api_key: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)


class LlmConfigTestIn(BaseModel):
    """test 入参：临时配置（不落库），验证连通性。"""
    api_key: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)


class LlmConfigTestOut(BaseModel):
    ok: bool
    latency_ms: int = 0
    error: str | None = None


# ── Routes ─────────────────────────────────────────────────


@router.get("/llm", response_model=LlmConfigOut)
def get_llm_config() -> LlmConfigOut:
    """读取当前 LLM 配置（不回显 key）。"""
    safe = db.LlmConfigRepository.get_safe()
    return LlmConfigOut(**safe)


@router.put("/llm")
def put_llm_config(payload: LlmConfigIn) -> dict:
    """保存 LLM 配置（key 加密存，覆盖单行）。"""
    db.LlmConfigRepository.save(
        api_key=payload.api_key,
        base_url=payload.base_url,
        model=payload.model,
        name=payload.name,
    )
    logger.info("LLM 配置已更新（model=%s, base_url=%s）", payload.model, payload.base_url)
    return {"ok": True}


@router.post("/llm/test", response_model=LlmConfigTestOut)
async def test_llm_config(payload: LlmConfigTestIn) -> LlmConfigTestOut:
    """测试连通性：用提交的临时 key 发最小 chat completion，验证可用。

    不落库。返回延迟和错误信息（若有）。
    """
    import time

    base_url = payload.base_url.rstrip("/")
    url = f"{base_url}/chat/completions"
    # model 可能是 "openai:gpt-4o" 形式，去 provider 前缀
    model = payload.model.split(":", 1)[-1]
    headers = {
        "Authorization": f"Bearer {payload.api_key}",
        "Content-Type": "application/json",
    }
    # 最小请求：max_tokens=1 省钱，只验证连通 + 鉴权
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=body, headers=headers)
        latency_ms = int((time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            return LlmConfigTestOut(ok=True, latency_ms=latency_ms)
        return LlmConfigTestOut(
            ok=False,
            latency_ms=latency_ms,
            error=f"HTTP {resp.status_code}: {resp.text[:200]}",
        )
    except httpx.HTTPError as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        return LlmConfigTestOut(ok=False, latency_ms=latency_ms, error=str(exc))
    except Exception as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        return LlmConfigTestOut(ok=False, latency_ms=latency_ms, error=str(exc))
