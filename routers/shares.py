"""共享链接管理：创建、落地页、删除、统一链接列表、二维码、兼容接口。

A2 拆分后的职责（路由路径与前端调用保持不变）：
- 创建/删除/列表：/api/share、/api/delete-share、/api/delete-link、
  /api/links、/api/shares（兼容）
- 落地页：/share/{id}、/share/video/{id}/{file}
- 二维码：/api/qr/{id}（完整 URL + 可配置样式）

媒体访问（内容/下载/校验/视频流）在 routers/share_media.py；
访问信息/日志/看板在 routers/share_log.py。
共享 helper（find_share / share_unlocked / video_content_type）位于 utils/share.py。
"""
import io
import uuid
from datetime import datetime, timedelta

import qrcode

from fastapi import APIRouter, Form, Request, HTTPException
from fastapi.responses import FileResponse, Response

from utils import storage
from utils import share as share_util

router = APIRouter(tags=["shares"])


def _find_share(share_id: str):
    """委托 utils.share（本模块仅保留别名供内部复用）。"""
    return share_util.find_share(share_id)


@router.post("/api/share")
def create_share(
    filepath: str = Form(...),
    share_type: str = Form(...),
    share_mode: str = Form("normal"),
    days: int = Form(...),
    password: str = Form("")
):
    share_id = str(uuid.uuid4())[:8]
    expire_time = datetime.now() + timedelta(days=days)

    shares = storage.load_shares()

    # 同路径已存在未过期共享时直接复用，避免重复创建
    for sid, share in shares.items():
        if share["path"] == filepath:
            expire_time = datetime.strptime(
                share["expire"], "%Y-%m-%d %H:%M:%S"
            )
            if expire_time > datetime.now():
                return {
                    "success": True,
                    "share_id": sid,
                    "expire": share["expire"],
                    "duplicate": True
                }

    shares[share_id] = {
        "path": filepath,
        "type": share_type,
        "share_mode": share_mode,
        "view_count": 0,
        "expire": expire_time.strftime("%Y-%m-%d %H:%M:%S"),
        "password": storage.hash_password(password) if password else "",
    }
    storage.save_shares(shares)

    return {
        "success": True,
        "share_id": share_id,
        "expire": shares[share_id]["expire"],
    }


# ---- 二维码：对共享/上传链接生成 PNG（管理页"显示二维码"按钮用）----

# 二维码样式默认值（可在 config/config.json 的 qr_style 中覆盖）
_QR_STYLE_DEFAULT = {
    "fg_color": "000000",
    "bg_color": "FFFFFF",
    "error_correction": "M",  # L / M / Q / H
    "box_size": 8,
    "border": 2,
}


def _qr_style() -> dict:
    """取二维码样式（设置页可配），与默认值合并。"""
    style = dict(_QR_STYLE_DEFAULT)
    try:
        user_style = storage.load_config().get("qr_style") or {}
        if isinstance(user_style, dict):
            style.update(user_style)
    except Exception:
        pass
    return style


def _public_base_url(request: Request) -> str:
    """二维码编码的完整 URL 基址（协议 + host）。

    优先级：
      1. 设置里的 public_base_url（手动配置，如 https://files.example.com）；
      2. 反向代理头 X-Forwarded-Proto + X-Forwarded-Host（Cloudflare/nginx）；
      3. 当前请求的 scheme + host。
    这样公网扫码能打开真实可访问地址，而非 localhost。
    """
    cfg = storage.load_config()
    base = (cfg.get("public_base_url") or "").strip()
    if base:
        return base.rstrip("/")
    fwd_host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host")
    if not fwd_host:
        fwd_host = request.headers.get("Host") or "localhost"
    proto = request.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip()
    return f"{proto}://{fwd_host}"


def _link_public_url(link_id: str, record: dict, request: Request) -> str:
    """构造链接的完整公开 URL（含基址）。"""
    path = "/upload/" + link_id if record.get("kind") == "upload" else "/share/" + link_id
    return _public_base_url(request) + path


# 预览二维码的示例内容：编码本项目的 git 库地址（设置页配色实时预览用，
# 与具体某条共享链接无关，仅为演示"该配色下二维码长什么样"）
QR_PREVIEW_SAMPLE = "https://github.com/XuczSnow/xs_file_lib"

# qrcode 库的纠错等级是四个独立常量（无 ERROR_CORRECT 字典）
_EC_MAP = {
    "L": qrcode.constants.ERROR_CORRECT_L,
    "M": qrcode.constants.ERROR_CORRECT_M,
    "Q": qrcode.constants.ERROR_CORRECT_Q,
    "H": qrcode.constants.ERROR_CORRECT_H,
}


def _render_qr_png(content: str, style: dict) -> bytes:
    """把 content 渲染成带自定义前景/背景色的二维码 PNG 字节流。

    默认黑白直接输出；非默认配色时用 Pillow 把黑像素重映射为 fg、
    白像素重映射为 bg（Image.composite + 亮度遮罩，比逐像素更快且无损）。
    """
    ec = _EC_MAP.get(style.get("error_correction", "M"),
                     qrcode.constants.ERROR_CORRECT_M)
    img = qrcode.QRCode(
        error_correction=ec,
        box_size=int(style.get("box_size", 8)),
        border=int(style.get("border", 2)),
    )
    img.add_data(content)
    img.make(fit=True)
    qr_img = img.make_image()

    fg = (style.get("fg_color") or "000000").lstrip("#")
    bg = (style.get("bg_color") or "FFFFFF").lstrip("#")
    if (fg, bg) != ("000000", "FFFFFF"):
        from PIL import Image

        def _hex_rgb(color: str, default):
            try:
                v = int(color, 16)
                if 0 <= v <= 0xFFFFFF:
                    return ((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)
            except (ValueError, TypeError):
                pass
            return default

        fg_rgb = _hex_rgb(fg, (0, 0, 0))
        bg_rgb = _hex_rgb(bg, (255, 255, 255))
        gray = qr_img.convert("L")  # 0=黑 255=白
        dark = Image.new("RGB", gray.size, fg_rgb)
        light = Image.new("RGB", gray.size, bg_rgb)
        qr_img = Image.composite(dark, light, gray).convert("RGBA")

    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    return buf.getvalue()


@router.get("/api/qr-preview")
def qr_preview(fg: str = "000000", bg: str = "FFFFFF", ec: str = "M", box: int = 8):
    """配色实时预览：渲染一张编码 git 库地址的示例二维码 PNG。

    供设置页"二维码样式"区在切换配色下拉时即时刷新预览（无需先保存设置）；
    颜色/纠错/尺寸由 query 传入，content 固定为 QR_PREVIEW_SAMPLE。
    """
    style = dict(_QR_STYLE_DEFAULT)
    style["fg_color"] = (fg or "000000").lstrip("#")
    style["bg_color"] = (bg or "FFFFFF").lstrip("#")
    style["error_correction"] = ec if ec in _EC_MAP else "M"
    try:
        style["box_size"] = int(box)
    except (ValueError, TypeError):
        pass
    return Response(
        content=_render_qr_png(QR_PREVIEW_SAMPLE, style),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/qr/{link_id}")
def link_qr(link_id: str, request: Request):
    """返回指定链接（共享或访客上传）的二维码 PNG（编码完整 URL，样式可配）。

    无效链接返回 404；二维码样式（颜色/纠错/尺寸/边距）由设置页配置。
    """
    record = storage.load_links().get(link_id)
    if record is None:
        raise HTTPException(status_code=404, detail="链接不存在")

    full_url = _link_public_url(link_id, record, request)
    return Response(
        content=_render_qr_png(full_url, _qr_style()),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


# ---- 共享落地页 ----

@router.get("/share/{share_id}")
def access_share(share_id: str):
    """共享落地页：存在则返回 share.html，否则返回 JSON 提示。"""
    if _find_share(share_id) is None:
        return {"success": False}
    return FileResponse("static/share.html")


@router.get("/share/video/{share_id}/{filename:path}")
def share_video_page(share_id: str, filename: str):
    return FileResponse("static/share_video.html")


# ---- 列表与删除（含旧接口兼容）----

@router.get("/api/shares")
def get_shares():
    """兼容旧接口：仅返回 kind==share 的记录。"""
    return storage.load_shares()


@router.get("/api/links")
def get_links():
    """统一链接列表：共享（查看/下载）+ 访客上传，管理页一次拉取。
    每条记录含 kind（share/upload）等字段。"""
    return storage.load_links()


@router.post("/api/delete-share")
def delete_share(share_id: str = Form(...)):
    """兼容旧接口：仅删除 share 记录。"""
    return _delete_share(share_id)


def _delete_share(share_id: str):
    """保留旧 /api/delete-share 语义：仅删除 share 记录。"""
    links = storage.load_links()
    rec = links.get(share_id)
    if rec and rec.get("kind") != "upload":
        links.pop(share_id, None)
        storage.save_links(links)
    return {"success": True}


@router.post("/api/delete-link")
def delete_link(link_id: str = Form(...)):
    """统一删除：按 link_id 删除任意 kind 的记录。"""
    links = storage.load_links()
    if link_id in links:
        del links[link_id]
    storage.save_links(links)
    return {"success": True}
