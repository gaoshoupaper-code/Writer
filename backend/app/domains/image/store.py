"""image domain 产物存储 + provider 解析。

职责：
- 图片文件落盘（workspace/images/<round>_<version>_<sample>.png）
- ImageRepository CRUD 封装
- 按 owner 解析生图/视觉 provider（复用 _resolve_model 模式，DD8c）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.platform.core.settings import Settings
from app.db import Database, ImageRepository, get_database
from app.domains.image.providers.bytedance import BytedanceImageProvider, BytedanceVisionProvider
from app.platform.providers.image_generation import ImageGenerationProvider
from app.platform.providers.image_understanding import ImageUnderstandingProvider


class ImageArtifactStore:
    """image domain 产物存储：图片文件落盘 + ImageRepository 封装。"""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.images = ImageRepository(db)

    def image_dir(self, workspace_path: Path) -> Path:
        """workspace 下的 images 目录（D10：本地文件系统，按 workspace 归属）。"""
        d = workspace_path / "images"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_image(
        self, workspace_path: Path, image_data: bytes, fmt: str,
        round_num: int, version_id: str, sample_index: int,
    ) -> str:
        """落盘图片，返回相对 workspace 的虚拟路径（D10）。"""
        d = self.image_dir(workspace_path)
        filename = f"r{round_num}_{version_id}_s{sample_index}.{fmt}"
        path = d / filename
        path.write_bytes(image_data)
        return f"/images/{filename}"

    def physical_path(self, workspace_path: Path, virtual_path: str) -> Path:
        """虚拟路径 → 物理路径（图片服务端点读图用）。"""
        return workspace_path / virtual_path.lstrip("/")

    def delete_image_file(self, workspace_path: Path, virtual_path: str) -> None:
        """删除图片物理文件（D11 废弃清理）。"""
        p = self.physical_path(workspace_path, virtual_path)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def resolve_image_provider(owner_id: str | None, settings: Settings) -> ImageGenerationProvider:
    """按 owner 解析生图 provider（DD8c → ii：按 owner 注入）。

    复用 _resolve_model 的模式：按 owner 解密 key 构建 provider，无 key 回退全局。
    """
    api_key, base_url = _owner_credentials(owner_id)
    return BytedanceImageProvider(api_key=api_key, base_url=base_url)


def resolve_vision_provider(owner_id: str | None, settings: Settings) -> ImageUnderstandingProvider:
    """按 owner 解析视觉 provider（DD8c → ii）。"""
    api_key, base_url = _owner_credentials(owner_id)
    return BytedanceVisionProvider(api_key=api_key, base_url=base_url)


def _owner_credentials(owner_id: str | None) -> tuple[str | None, str | None]:
    """从 DB 读 owner 的 API key/base_url。无 owner 返回 (None, None)。

    复用 UserRepository.get_api_key_plain（与 build_writer_model 同源）。
    生图/视觉 API 可能与写作 API 是不同 key——当前占位共用同一 key，
    真实接入后若分离需扩展 users 表或加独立配置。
    """
    if not owner_id:
        return None, None
    try:
        from app.db import UserRepository
        users = UserRepository(get_database())
        key, base_url, _model = users.get_api_key_plain(owner_id)
        return key, base_url
    except Exception:
        return None, None


__all__ = [
    "ImageArtifactStore",
    "resolve_image_provider",
    "resolve_vision_provider",
]
