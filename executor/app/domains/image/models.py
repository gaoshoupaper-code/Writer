"""image domain 模型构建（PR-14 解耦：从 writing.models 独立）。

image 当前复用 writer_model 配置（settings.writer_model + DeepSeek 适配），
但物理上独立于 writing domain，消除 image→writing 跨域依赖（R2）。
未来可扩展独立的 image_model 配置。
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from app.platform.core.settings import Settings

DEEPSEEK_PROVIDER = "deepseek"


def _parse_model(raw_model: str) -> tuple[str, str]:
    """解析 '<provider>:<model>' 或 '<model>' 格式。"""
    if ":" not in raw_model:
        provider = DEEPSEEK_PROVIDER if raw_model.startswith("deepseek-") else "openai"
        return provider, raw_model
    provider, model_name = raw_model.split(":", 1)
    if not provider or not model_name:
        raise ValueError("model must be '<provider>:<model>' or '<model>'.")
    return provider.lower(), model_name


def build_image_model(
    settings: Settings,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model_name_override: str | None = None,
) -> ChatOpenAI:
    """构建 image agent 模型。

    多用户隔离（D9）：普通用户传入解密后的 api_key + base_url + model，
    覆盖 settings 全局值。所有参数为 None 时回退 settings（管理员兜底）。
    """
    # image 当前复用 writer_model 配置（无独立 image_model 字段）
    effective_model = model_name_override if model_name_override else settings.writer_model
    provider, model_name = _parse_model(effective_model)
    options: dict = {}

    if settings.writer_temperature is not None:
        options["temperature"] = settings.writer_temperature
    if settings.writer_top_p is not None:
        options["top_p"] = settings.writer_top_p

    effective_key = api_key if api_key is not None else settings.openai_api_key
    effective_base = base_url if base_url is not None else settings.openai_base_url

    # image 用标准 ChatOpenAI（不需要 DeepSeek thinking 适配——文生图无需推理链）
    return ChatOpenAI(
        model=model_name,
        api_key=effective_key,
        base_url=effective_base,
        request_timeout=120,
        stream_usage=True,
        **options,
    )
