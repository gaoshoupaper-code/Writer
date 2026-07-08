"""大模型 API 配置接口（多配置管理，2026-07-08）。

桌面端唯一配置入口。支持保存多个配置（deepseek/glm/openai 等），选一个激活，
runtime 读激活项。测试连通性时按 id 读库解密（不再要求每次重输 key）。

端点：
- GET    /api/config/llm              激活配置安全视图（旧契约，首页 status 用）
- GET    /api/config/llm/list         所有配置列表（含 key_hint 脱敏）
- POST   /api/config/llm              新建配置
- PUT    /api/config/llm/{id}         更新配置（api_key 空=不改）
- DELETE /api/config/llm/{id}         删除配置
- POST   /api/config/llm/{id}/activate 设为激活
- POST   /api/config/llm/test         测试连通性（id 读库 / 临时 key 二选一）

鉴权由 SSOAuthMiddleware 统一处理（白名单用户才能访问）。
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core import db

logger = logging.getLogger("evolution.config")

router = APIRouter(prefix="/config", tags=["config"])


# ── Schemas ────────────────────────────────────────────────


class LlmConfigOut(BaseModel):
    """GET /llm 返回：激活配置（不回显 key，只标 has_key）。"""
    has_key: bool
    name: str | None = None
    base_url: str = ""
    model: str = ""
    updated_at: str | None = None


class LlmConfigItemOut(BaseModel):
    """列表项（不回显 key 明文，附 key_hint 尾 4 位脱敏）。"""
    id: int
    name: str
    base_url: str
    model: str
    has_key: bool
    key_hint: str | None = None
    is_active: bool
    created_at: str
    updated_at: str


class LlmConfigCreateIn(BaseModel):
    """新建入参。"""
    name: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)


class LlmConfigUpdateIn(BaseModel):
    """更新入参：所有字段可选，api_key 空/省略=不改。"""
    name: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


class LlmConfigTestIn(BaseModel):
    """测试入参：两条路径二选一。

    路径 A（读库测已存配置）：只填 id，后端读库解密该 id 的 key。
    路径 B（测草稿）：填 api_key + base_url + model，不落库。
    id 优先：若同时填了 id 和三字段，按 id 读库（忽略三字段）。
    """
    id: int | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


class LlmConfigTestOut(BaseModel):
    ok: bool
    latency_ms: int = 0
    error: str | None = None


# ── Routes ─────────────────────────────────────────────────


@router.get("/llm", response_model=LlmConfigOut)
def get_llm_config() -> LlmConfigOut:
    """读取激活配置（不回显 key）。旧契约，首页 status 用。"""
    safe = db.LlmConfigsRepository.get_active_safe()
    return LlmConfigOut(**safe)


@router.get("/llm/list", response_model=list[LlmConfigItemOut])
def list_llm_configs() -> list[LlmConfigItemOut]:
    """读取所有配置列表（不回显 key 明文，附 key_hint）。"""
    rows = db.LlmConfigsRepository.list_all()
    return [LlmConfigItemOut(**r) for r in rows]


@router.post("/llm", response_model=LlmConfigItemOut, status_code=201)
def create_llm_config(payload: LlmConfigCreateIn) -> LlmConfigItemOut:
    """新建配置。首条自动激活。"""
    new_id = db.LlmConfigsRepository.create(
        name=payload.name,
        api_key=payload.api_key,
        base_url=payload.base_url,
        model=payload.model,
    )
    logger.info("LLM 配置已新建（id=%s, name=%s）", new_id, payload.name)
    # 回读返回完整项
    rows = db.LlmConfigsRepository.list_all()
    item = next((r for r in rows if r["id"] == new_id), None)
    if item is None:
        raise HTTPException(500, "新建后回读失败")
    return LlmConfigItemOut(**item)


@router.put("/llm/{cfg_id}", response_model=LlmConfigItemOut)
def update_llm_config(cfg_id: int, payload: LlmConfigUpdateIn) -> LlmConfigItemOut:
    """更新配置。api_key 空/省略=不改 key。"""
    ok = db.LlmConfigsRepository.update(
        cfg_id,
        name=payload.name,
        api_key=payload.api_key,
        base_url=payload.base_url,
        model=payload.model,
    )
    if not ok:
        raise HTTPException(404, f"配置 id={cfg_id} 不存在")
    logger.info("LLM 配置已更新（id=%s）", cfg_id)
    rows = db.LlmConfigsRepository.list_all()
    item = next((r for r in rows if r["id"] == cfg_id), None)
    if item is None:
        raise HTTPException(500, "更新后回读失败")
    return LlmConfigItemOut(**item)


@router.delete("/llm/{cfg_id}")
def delete_llm_config(cfg_id: int) -> dict:
    """删除配置。若删的是激活项 → 自动激活剩余中 id 最小的一条。"""
    ok = db.LlmConfigsRepository.delete(cfg_id)
    if not ok:
        raise HTTPException(404, f"配置 id={cfg_id} 不存在")
    logger.info("LLM 配置已删除（id=%s）", cfg_id)
    return {"ok": True}


@router.post("/llm/{cfg_id}/activate", response_model=LlmConfigItemOut)
def activate_llm_config(cfg_id: int) -> LlmConfigItemOut:
    """设为激活（全局唯一）。"""
    ok = db.LlmConfigsRepository.activate(cfg_id)
    if not ok:
        raise HTTPException(404, f"配置 id={cfg_id} 不存在")
    logger.info("LLM 配置已激活（id=%s）", cfg_id)
    rows = db.LlmConfigsRepository.list_all()
    item = next((r for r in rows if r["id"] == cfg_id), None)
    if item is None:
        raise HTTPException(500, "激活后回读失败")
    return LlmConfigItemOut(**item)


@router.post("/llm/test", response_model=LlmConfigTestOut)
async def test_llm_config(payload: LlmConfigTestIn) -> LlmConfigTestOut:
    """测试连通性。两条路径二选一（id 优先）：

    A. 读库测已存配置：payload 只填 id → 后端读库解密该 id 的 key + base_url + model。
    B. 测草稿：payload 填 api_key + base_url + model → 不落库直接测。

    发最小 chat completion（max_tokens=1）验证连通 + 鉴权。
    """
    import time

    # ── 解析测试参数 ──
    if payload.id is not None:
        # 路径 A：读库
        decrypted = db.LlmConfigsRepository.get_decrypted(payload.id)
        if decrypted is None:
            return LlmConfigTestOut(
                ok=False, error=f"配置 id={payload.id} 不存在或尚未填写 api_key"
            )
        api_key, base_url_raw, model_raw = decrypted
    else:
        # 路径 B：草稿（要求三字段齐全）
        if not payload.api_key or not payload.base_url or not payload.model:
            return LlmConfigTestOut(
                ok=False,
                error="请提供 id（测已存配置）或完整的 api_key + base_url + model（测草稿）",
            )
        api_key = payload.api_key
        base_url_raw = payload.base_url
        model_raw = payload.model

    base_url = base_url_raw.rstrip("/")
    url = f"{base_url}/chat/completions"
    # model 可能是 "openai:gpt-4o" 形式，去 provider 前缀
    model = model_raw.split(":", 1)[-1]
    headers = {
        "Authorization": f"Bearer {api_key}",
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
