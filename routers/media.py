"""媒体缩略图：图片缩略图（600/1200）与视频首帧缩略图。"""
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from PIL import Image
import av

from utils import storage, security

router = APIRouter(tags=["media"])


def _create_thumbnail(source, target, short_side=600):
    """按短边缩放生成 JPEG 缩略图。"""
    img = Image.open(source)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    width, height = img.size
    scale = short_side / min(width, height)

    if scale < 1:
        new_width = int(width * scale)
        new_height = int(height * scale)
    else:
        new_width = width
        new_height = height

    img = img.resize((new_width, new_height), Image.LANCZOS)
    img.save(target, format="JPEG", quality=75, optimize=True)


def _image_proxy_from_source(source: str, filepath: str, short_side: int):
    """图片代理/缩略图统一实现（source 已由调用方 safe_join 校验）。

    原 get_thumbnail（600）与 get_preview_image（1200）逻辑完全相同，
    合并为一个参数化实现，仅缓存目录与尺寸不同。
    """
    base_name = os.path.splitext(filepath)[0]
    thumb_root = os.path.join(
        storage.THUMB_DIR, f".img_thumbs_{short_side}"
    )
    thumb_path = os.path.join(thumb_root, base_name + ".jpg")

    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)

    if not os.path.exists(thumb_path):
        _create_thumbnail(source, thumb_path, short_side)

    # 缩略图是内容级稳定缓存：让浏览器/CDN（Cloudflare）长期复用，不再回源
    return FileResponse(thumb_path, headers={"Cache-Control": "public, max-age=2592000"})


def _generate_video_thumbnail(source_path, thumb_path):
    """取视频首帧存为 JPEG。"""
    container = av.open(source_path)
    try:
        frame = next(container.decode(video=0))
        frame.to_image().save(thumb_path, quality=80)
    finally:
        container.close()


@router.get("/api/thumbnail/{filepath:path}")
def get_thumbnail(filepath: str):
    """列表缩略图（短边 600）。safe_join 防路径穿越。"""
    source = security.safe_join(storage.UPLOAD_DIR, filepath)
    if source is None or not os.path.isfile(source):
        raise HTTPException(status_code=404, detail="图片不存在")
    return _image_proxy_from_source(source, filepath, 600)


@router.get("/api/preview-image/{filepath:path}")
def get_preview_image(filepath: str):
    """预览大图（短边 1200）。safe_join 防路径穿越。"""
    source = security.safe_join(storage.UPLOAD_DIR, filepath)
    if source is None or not os.path.isfile(source):
        raise HTTPException(status_code=404, detail="图片不存在")
    return _image_proxy_from_source(source, filepath, 1200)


@router.get("/api/video-thumbnail/{filepath:path}")
def video_thumbnail(filepath: str):
    """视频首帧缩略图。safe_join 防路径穿越。"""
    source = security.safe_join(storage.UPLOAD_DIR, filepath)
    if source is None or not os.path.isfile(source):
        raise HTTPException(status_code=404, detail="视频不存在")

    thumb_root = os.path.join(storage.THUMB_DIR, ".video_thumbs")
    thumb_file = os.path.join(thumb_root, filepath + ".jpg")
    os.makedirs(os.path.dirname(thumb_file), exist_ok=True)

    if not os.path.exists(thumb_file):
        _generate_video_thumbnail(source, thumb_file)

    return FileResponse(thumb_file, headers={"Cache-Control": "public, max-age=2592000"})
