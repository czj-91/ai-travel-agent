"""多模态输入：把本地图片编码成 LangChain 标准 image content block。"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024  # 单图上限，超过直接拒绝，避免上下文爆炸
SUPPORTED = ("image/jpeg", "image/png", "image/webp", "image/gif")


class ImageInputError(ValueError):
    """图片输入不合法。"""


def encode_image(path: str | Path) -> dict:
    """读图并返回 {"type":"image","base64":...,"mime_type":...}。"""
    file = Path(path)
    if not file.is_file():
        raise ImageInputError(f"图片不存在：{file}")
    raw = file.read_bytes()
    if not raw:
        raise ImageInputError(f"图片为空文件：{file}")
    if len(raw) > MAX_BYTES:
        raise ImageInputError(f"图片过大 {len(raw) / 1048576:.1f} MB，上限 {MAX_BYTES // 1048576} MB")
    mime = mimetypes.guess_type(file.name)[0] or "image/jpeg"
    if mime not in SUPPORTED:
        raise ImageInputError(f"不支持的图片格式 {mime}，支持 {', '.join(SUPPORTED)}")
    return {"type": "image", "base64": base64.b64encode(raw).decode("ascii"), "mime_type": mime}


def build_content(text: str, image_path: str | Path | None = None):
    """无图时返回纯文本；有图时返回 [image, text] 内容块列表。"""
    if not image_path:
        return text
    return [encode_image(image_path), {"type": "text", "text": text}]