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
    # 单文件共享不支持相册模式（相册依赖目录树/多文件，file 型无此结构）→ 强制 normal
    if share_type == "file":
        share_mode = "normal"
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



def _hex_to_rgb(color_str, default=(0, 0, 0)):
    try:
        v = int(color_str.lstrip("#"), 16)
        if 0 <= v <= 0xFFFFFF:
            return ((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)
    except (ValueError, TypeError):
        pass
    return default


def _load_font(size):
    from PIL import ImageFont
    for path in (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size)
    except Exception:
        return ImageFont.load_default()

def _load_font_bold(size):
    """加载粗体 CJK 字体（用于标题）；找不到退回常规字体。"""
    from PIL import ImageFont
    for path in (
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/msyhbd.ttf",
        "C:/Windows/Fonts/simhei.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return _load_font(size)



def _ellipsize(text, font, max_px, draw, keep_head=True):
    """超宽截断：keep_head=True 保留开头砍尾部（标题/路径）；
    False 保留头尾砍中间（URL）。"""
    if draw.textlength(text, font=font) <= max_px:
        return text
    if keep_head:
        s = text
        while s and draw.textlength(s + "…", font=font) > max_px:
            s = s[:-1]
        return s + "…"
    # 头尾保留式
    head = max(6, len(text) // 3)
    tail = max(4, (len(text) - head) // 3)
    while head + tail > 12:
        probe = text[:head] + "…" + text[len(text) - tail:]
        if draw.textlength(probe, font=font) <= max_px:
            return probe
        head = max(4, head - 2)
        tail = max(2, tail - 2)
    return text[:3] + "…"


def _render_qr_with_info(content, style, title="", url=""):
    """美化的二维码 PNG：圆角卡片 + 留白 + 标题 / URL / "扫码访问" 提示。

    布局（卡片内）：
      顶部留白 → QR（含 quiet zone）→ 间距 → "扫码访问" 小字
      → 标题（加粗大字）→ URL（灰字 + 下划线）→ 底部留白
    卡片四角圆角、轻阴影；配色随 qr_style.fg/bg 自动反色。
    """
    from PIL import Image, ImageDraw, ImageFilter

    # ── QR 主体 ──
    ec = _EC_MAP.get(style.get("error_correction", "M"),
                     qrcode.constants.ERROR_CORRECT_M)
    qr = qrcode.QRCode(error_correction=ec,
                       box_size=int(style.get("box_size", 8)),
                       border=int(style.get("border", 4)))
    qr.add_data(content)
    qr.make(fit=True)
    qr_img = qr.make_image()

    fg = _hex_to_rgb((style.get("fg_color") or "000000").lstrip("#"))
    bg = _hex_to_rgb((style.get("bg_color") or "FFFFFF").lstrip("#"))
    bg_lum = (bg[0] + bg[1] + bg[2]) / 3

    if (fg, bg) != ((0, 0, 0), (255, 255, 255)):
        gray = qr_img.convert("L")
        dark = Image.new("RGB", gray.size, fg)
        light = Image.new("RGB", gray.size, bg)
        qr_img = Image.composite(dark, light, gray)
    qr_img = qr_img.convert("RGBA")
    qw, qh = qr_img.size

    # ── 无标题且无 URL → 直接返回纯 QR（保持 /api/qr-preview 轻量）──
    if not title and not url:
        buf = io.BytesIO()
        qr_img.save(buf, format="PNG")
        return buf.getvalue()

    # ── 字体 ──
    t_size = max(13, qw // 10)
    u_size = max(10, qw // 26)
    h_size = max(9, qw // 32)
    tf = _load_font_bold(t_size)
    uf = _load_font(u_size)
    hf = _load_font(h_size)

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    max_w = float(qw - 8)
    d_title = _ellipsize(title, tf, max_w, probe, keep_head=True) if title else ""
    d_url = _ellipsize(url, uf, max_w, probe, keep_head=False) if url else ""
    d_hint = "扫码访问"

    tw = int(probe.textlength(d_title, font=tf)) if d_title else 0
    uw = int(probe.textlength(d_url, font=uf)) if d_url else 0
    hw = int(probe.textlength(d_hint, font=hf))

    # ── 卡片尺寸 ──
    pad_h = max(18, qw // 9)       # 左右内边距
    pad_top = max(16, qw // 11)    # 顶部内边距
    pad_bot = max(16, qw // 11)    # 底部内边距
    gap = max(10, qh // 14)        # QR 与文字间距

    hint_h = h_size + 6
    title_h = t_size + 6 if d_title else 0
    url_h = u_size + 10 if d_url else 0  # 含下划线

    card_w = qw + pad_h * 2
    card_h = pad_top + qh + gap + hint_h + title_h + url_h + pad_bot

    # ── 画布（含阴影扩展区，四角透明）──
    sh = 6   # 阴影向外扩展
    blur = 10
    canvas_w = card_w + sh * 2 + blur * 2
    canvas_h = card_h + sh * 2 + blur * 2
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    cx = (canvas_w - card_w) // 2
    cy = (canvas_h - card_h) // 2

    # ── 阴影（圆角矩形 + 高斯模糊）──
    card_fill = (255, 255, 255, 255) if bg_lum > 128 else bg + (255,)
    shadow_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer)
    sd.rounded_rectangle(
        [cx - sh + 2, cy - sh + 4, cx + card_w + sh, cy + card_h + sh + 2],
        radius=22, fill=(0, 0, 0, 28))
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(blur))
    canvas = Image.alpha_composite(canvas, shadow_layer)

    # ── 卡片（圆角 + 1px 描边）──
    card_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    cd = ImageDraw.Draw(card_layer)
    cd.rounded_rectangle(
        [cx, cy, cx + card_w, cy + card_h],
        radius=20, fill=card_fill,
        outline=(0, 0, 0, 22), width=1)
    canvas = Image.alpha_composite(canvas, card_layer)

    # ── 画 QR（卡片内居中）──
    qr_x = cx + (card_w - qw) // 2
    qr_y = cy + pad_top
    canvas.paste(qr_img, (qr_x, qr_y), qr_img)

    # ── 文字 ──
    draw = ImageDraw.Draw(canvas)
    mid_x = cx + card_w // 2
    y = qr_y + qh + gap

    # 提示小字（fg 色 45% 透明）
    hint_a = 115 if bg_lum > 128 else 130
    hint_clr = fg[:3] + (hint_a,) if bg_lum > 128 else (255, 255, 255, hint_a)
    draw.text((mid_x, y + h_size // 2), d_hint,
              font=hf, fill=hint_clr, anchor="mm")
    y += hint_h

    # 标题（加粗，fg 色 / 深底反白）
    title_clr = fg + (255,) if bg_lum > 128 else (245, 245, 250, 255)
    if d_title:
        draw.text((mid_x, y + t_size // 2), d_title,
                  font=tf, fill=title_clr, anchor="mm")
        y += title_h

    # URL（灰 + 1px 下划线）
    url_clr = (90, 100, 120, 200) if bg_lum > 128 else (165, 175, 195, 200)
    url_line_clr = (90, 100, 120, 70) if bg_lum > 128 else (165, 175, 195, 70)
    if d_url:
        draw.text((mid_x, y + u_size // 2), d_url,
                  font=uf, fill=url_clr, anchor="mm")
        draw.line(
            [(mid_x - uw // 2, y + u_size + 3),
             (mid_x + uw // 2, y + u_size + 3)],
            fill=url_line_clr, width=1)

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()

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
def qr_preview(fg: str = "000000", bg: str = "FFFFFF", ec: str = "M", box: str = "8"):
    """配色实时预览：渲染一张编码 git 库地址的示例二维码 PNG。

    供设置页"二维码样式"区在切换配色下拉时即时刷新预览（无需先保存设置）；
    颜色/纠错/尺寸由 query 传入，content 固定为 QR_PREVIEW_SAMPLE。
    box 用 str 接收并在函数体内解析：前端空值（box=）回退默认 8，避免 422 破图。
    """
    style = dict(_QR_STYLE_DEFAULT)
    style["fg_color"] = (fg or "000000").lstrip("#")
    style["bg_color"] = (bg or "FFFFFF").lstrip("#")
    style["error_correction"] = ec if ec in _EC_MAP else "M"
    try:
        style["box_size"] = int(str(box).strip() or 8)
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
    title = record.get("path") or record.get("folder") or ""
    return Response(
        content=_render_qr_with_info(full_url, _qr_style(),
                                     title=title, url=full_url),
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
