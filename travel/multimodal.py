"""多模态输入：把本地图片编码成 LangChain 标准 image content block。"""
from __future__ import annotations

import base64
import re
import mimetypes
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024  # 单图上限，超过直接拒绝，避免上下文爆炸
SUPPORTED = ("image/jpeg", "image/png", "image/webp", "image/gif")

# 魔数表：上传的纯 base64 没有文件名，只能按字节头认格式（浏览器给的 mime 也可能谎报）
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # 下面再校验 WEBP 标记
)


class ImageInputError(ValueError):
    """图片输入不合法。"""


def check_image_model(model: str, has_image: bool) -> None:
    """带图必须用多模态模型：早判早报错，别让请求打到模型后只回一个看不懂的 400。

    CLI（travel.graph.plan_trip）与 HTTP（service./plan）共用这一条判断，避免规则两处漂移。
    """
    if has_image and not (model or "").startswith("qwen"):
        raise ValueError(
            f"当前模型 {model} 不支持图片输入。请在 .env 里把 TRAVEL_MODEL 改成 qwen3.5-plus 后重跑。"
        )


def sniff_mime(raw: bytes) -> str:
    for magic, mime in _MAGIC:
        if raw.startswith(magic):
            if mime == "image/webp" and raw[8:12] != b"WEBP":
                continue
            return mime
    return ""


def _check(raw: bytes, mime: str, label: str) -> None:
    if not raw:
        raise ImageInputError(f"图片内容为空：{label}")
    if len(raw) > MAX_BYTES:
        raise ImageInputError(f"图片过大 {len(raw) / 1048576:.1f} MB，上限 {MAX_BYTES // 1048576} MB")
    if mime not in SUPPORTED:
        raise ImageInputError(f"不支持的图片格式 {mime or '未知'}，支持 {', '.join(SUPPORTED)}")


def encode_image_bytes(raw: bytes, mime: str = "") -> dict:
    """字节流 -> content block。魔数说了算：调用方谎报 mime 时按真实内容纠正。"""
    mime = sniff_mime(raw) or mime
    _check(raw, mime, "上传内容")
    return {"type": "image", "base64": base64.b64encode(raw).decode("ascii"), "mime_type": mime}


def parse_data_image(value: str) -> dict:
    """接受 `data:image/png;base64,xxx` 或纯 base64，返回 content block。"""
    text = (value or "").strip()
    if not text:
        raise ImageInputError("图片内容为空")
    mime = ""
    match = re.match(r"^data:([\w.+-]+/[\w.+-]+)?;?[a-zA-Z0-9_-]*,(.*)$", text, re.S)
    if match:
        mime, text = match.group(1) or "", match.group(2)
    try:
        raw = base64.b64decode(text, validate=False)
    except (ValueError, TypeError) as exc:
        raise ImageInputError(f"base64 解码失败：{type(exc).__name__}") from exc
    return encode_image_bytes(raw, mime)


def encode_image(path: str | Path) -> dict:
    """读本地图片并返回 {"type":"image","base64":...,"mime_type":...}。"""
    file = Path(path)
    if not file.is_file():
        raise ImageInputError(f"图片不存在：{file}")
    raw = file.read_bytes()
    if not raw:
        raise ImageInputError(f"图片为空文件：{file}")
    mime = mimetypes.guess_type(file.name)[0] or "image/jpeg"
    return encode_image_bytes(raw, mime)


def build_content(text: str, image_path: str | Path | None = None, image_block: dict | None = None):
    """无图返回纯文本；有图返回 [image, text] 内容块列表。

    image_path 给 CLI 用，image_block 给 HTTP 上传用（已经解码好，不再读盘）。
    """
    if image_block is None and image_path is None:
        return text
    block = image_block if image_block is not None else encode_image(image_path)
    return [block, {"type": "text", "text": text}]
