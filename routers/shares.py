"""共享链接：创建、校验、访问信息、下载、访问日志、视频流、二维码。"""
import io
import os
import uuid
from datetime import datetime, timedelta

import qrcode

from fastapi import APIRouter, Form, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from utils import storage, filetree, security

router = APIRouter(tags=["shares"])


def _find_share(share_id: str):
    """按 ID 查共享记录；不存在返回 None（替代原直接下标取值的 KeyError）。"""
    shares = storage.load_shares()
    return shares.get(share_id)


def _share_unlocked(share: dict, share_id: str, password: str = "", access: str = "") -> bool:
    """共享访问闸门：

    - 未设密码：放行；
    - 设置了密码：需携带正确的短期访问令牌（access，由 /api/share-check 校验
      密码后签发），或本次请求带上正确明文密码。
      用令牌而非把明文密码写进下载 URL，避免密码进入访问日志/分享链接。
    """
    if not share.get("password"):
        return True
    if access and security.verify_share_access(access, share_id):
        return True
    return security.verify_password(share["password"], password or "")


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

def _link_public_url(link_id: str, record: dict) -> str:
    """按 kind 构造链接的公开访问 URL（不含主机，前端可补全 origin）。"""
    if record.get("kind") == "upload":
        return "/upload/" + link_id
    return "/share/" + link_id


@router.get("/api/qr/{link_id}")
def link_qr(link_id: str):
    """返回指定链接（共享或访客上传）的二维码 PNG。

    仅编码"本站该链接的公开路径"，避免把任意 URL 编码进二维码。
    前端拿到 PNG 后可叠加在链接卡片上；无效链接返回 404。
    """
    record = storage.load_links().get(link_id)
    if record is None:
        raise HTTPException(status_code=404, detail="链接不存在")

    path = _link_public_url(link_id, record)
    img = qrcode.make(path)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/share/{share_id}")
def access_share(share_id: str):
    if _find_share(share_id) is None:
        return {"success": False}
    return FileResponse("static/share.html")


@router.get("/share/video/{share_id}/{filename:path}")
def share_video_page(share_id: str, filename: str):
    return FileResponse("static/share_video.html")


@router.get("/api/share-content/{share_id}")
def share_content(share_id: str, password: str = "", access: str = ""):
    share = _find_share(share_id)
    if share is None:
        return {"success": False}

    # 密码闸门：设了密码的共享需携带 access 令牌（/api/share-check 校验后签发）或正确密码
    if not _share_unlocked(share, share_id, password, access):
        return {"success": False, "message": "密码错误"}

    # 过期校验（与 /api/share-download 一致）
    expire_time = datetime.strptime(share["expire"], "%Y-%m-%d %H:%M:%S")
    if datetime.now() > expire_time:
        return {"success": False, "message": "共享已过期"}

    # 存储的共享路径本身也做一次 safe_join，防止历史数据里的脏路径越界
    base = os.path.abspath(storage.UPLOAD_DIR)
    shared_dir = security.safe_join(base, share["path"]) if share.get("path") else base

    if share["type"] == "file":
        # 文件类型：拼 UPLOAD_DIR 前缀取 size；路径守卫 + 存在性校验
        full_path = shared_dir if os.path.isfile(shared_dir) else None
        if full_path is None:
            return {"success": False, "message": "文件不存在"}
        return {
            "success": True,
            "type": "file",
            "share_id": share_id,
            "path": share["path"],
            "name": os.path.basename(share["path"]),
            "files": [{
                "name": os.path.basename(share["path"]),
                "size": os.path.getsize(full_path),
            }],
        }

    if shared_dir is None or not os.path.isdir(shared_dir):
        return {"success": False, "message": "目录不存在"}

    # 树状结构：共享子目录为遍历起点，文件 path 仍相对 UPLOAD_DIR
    tree = filetree.build_file_tree(storage.UPLOAD_DIR, start_dir=shared_dir)
    # 全层级扁平文件列表（相册模式收集所有图片/视频用）
    all_files = filetree.flatten_files(tree)

    return {
        "success": True,
        "type": "folder",
        "share_mode": share.get("share_mode", "normal"),
        "share_id": share_id,
        "path": share["path"],
        "name": share["path"],
        "tree": tree,
        "files": all_files,
    }


@router.get("/api/share-folder-download/{share_id}/{filename:path}")
def share_folder_download(share_id: str, filename: str, password: str = "", access: str = ""):
    """下载共享文件夹内的单个文件。

    安全加固：
    - 过期共享直接拒绝（与 /api/share-download 保持一致）；
    - 密码闸门：设了密码需携带 access 令牌或正确密码；
    - 用 safe_join 校验 filename 不逃出共享目录（阻止 `../` 路径穿越）。
    """
    share = _find_share(share_id)
    if share is None:
        return {"success": False, "message": "共享不存在"}

    if not _share_unlocked(share, share_id, password, access):
        return {"success": False, "message": "密码错误"}

    # 过期校验：与原 share_download 一致
    expire_time = datetime.strptime(share["expire"], "%Y-%m-%d %H:%M:%S")
    if datetime.now() > expire_time:
        return {"success": False, "message": "共享已过期"}

    share_dir = security.safe_join(storage.UPLOAD_DIR, share["path"])
    file_path = security.safe_join(share_dir, filename)
    if file_path is None or not os.path.isfile(file_path):
        return {"success": False, "message": "文件不存在"}
    return FileResponse(file_path)


@router.post("/api/share-check")
def share_check(share_id: str = Form(...), password: str = Form("")):
    """校验共享密码。成功后签发短期访问令牌，后续取内容/下载/播放都带它。

    兼容 bcrypt 与旧 SHA-256 存量哈希（security.verify_password）。
    """
    share = _find_share(share_id)
    if share is None:
        return {"success": False, "message": "共享不存在"}

    if share.get("password", "") == "":
        return {"success": True, "access_token": security.issue_share_access(share_id)}

    if not security.verify_password(share["password"], password):
        return {"success": False, "message": "密码错误"}

    return {"success": True, "access_token": security.issue_share_access(share_id)}


@router.get("/api/share-download/{share_id}")
def share_download(share_id: str, password: str = "", access: str = ""):
    """下载单文件共享。设置了密码的需携带 access 令牌或正确密码。"""
    share = _find_share(share_id)
    if share is None:
        return {"success": False, "message": "共享不存在"}

    if not _share_unlocked(share, share_id, password, access):
        return {"success": False, "message": "密码错误"}

    expire_time = datetime.strptime(
        share["expire"], "%Y-%m-%d %H:%M:%S"
    )
    if datetime.now() > expire_time:
        return {"success": False, "message": "共享已过期"}

    # 路径守卫 + 存在性校验
    file_path = security.safe_join(storage.UPLOAD_DIR, share["path"])
    if file_path is None or not os.path.isfile(file_path):
        return {"success": False, "message": "文件不存在"}

    return FileResponse(
        file_path,
        filename=os.path.basename(share["path"]),
    )


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


@router.get("/api/share-info/{share_id}")
def share_info(share_id: str, request: Request):
    shares = storage.load_shares()
    if share_id not in shares:
        return {"success": False, "message": "共享不存在"}

    share = shares[share_id]
    share["view_count"] = share.get("view_count", 0) + 1
    storage.save_shares(shares)

    storage.write_share_access_log({
        "share_id": share_id,
        "path": share["path"],
        "visit_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "real_ip": request.headers.get("CF-Connecting-IP", ""),
        "ip": request.client.host,
        "user_agent": request.headers.get("User-Agent", ""),
    })

    expired = datetime.now() > datetime.strptime(
        share["expire"], "%Y-%m-%d %H:%M:%S"
    )

    return {
        "success": True,
        "type": share.get("type", "file"),
        "share_mode": share.get("share_mode", "normal"),
        "name": share["path"],
        "expire": share["expire"],
        "expired": expired,
        "has_password": bool(share.get("password", "")),
    }


@router.get("/api/share-access-log/{share_id}")
def get_share_access_log(share_id: str):
    logs = storage.load_json(storage.ACCESS_LOG_CONF, [])
    return [item for item in logs if item.get("share_id") == share_id]


# 视频 Content-Type 按扩展名推断（弱服务器 + 浏览器解码正确性）；未知类型给通用值
_VIDEO_CONTENT_TYPES = {
    "mp4": "video/mp4", "m4v": "video/mp4",
    "webm": "video/webm", "ogv": "video/ogg",
    "mov": "video/quicktime", "mkv": "video/x-matroska",
    "avi": "video/x-msvideo", "ts": "video/mp2t",
}


def _video_content_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lstrip(".").lower()
    return _VIDEO_CONTENT_TYPES.get(ext, "application/octet-stream")


@router.get("/api/video-stream/{share_id}/{filename:path}")
def share_video_stream(share_id: str, filename: str, request: Request,
                       password: str = "", access: str = ""):
    """支持 Range 的视频流。

    P0 优化（弱服务器 / Cloudflare 场景）：
    - 同步 def：FastAPI 放线程池执行，慢客户端不再阻塞事件循环
      （原 async def + 同步 open/read 会卡死其他请求）；
    - 4MB 分块（原 1MB）：减少系统调用次数；
    - Content-Type 按扩展名推断（原写死 video/mp4，webm/mov 会解码异常）；
    - ETag + Cache-Control：公开流 s-maxage=3600（让 CF 边缘缓存整对象、
      拖动/回看由边缘合成 206）；带 access 密码令牌的流 no-store
      （防共享过期后缓存仍可访问，防令牌进入缓存键）。

    注意：{filename:path} 到达处理器前已被 ASGI 解码，不要再 unquote。
    密码闸门：设了密码的共享需 URL 携带 access 令牌（?access=xxx）。
    """
    share = _find_share(share_id)
    if share is None:
        return JSONResponse({"success": False}, status_code=404)

    # 密码闸门（视频流是直链，无法弹框收密码，故用 share-check 签发的 access 令牌）
    if not _share_unlocked(share, share_id, password, access):
        return JSONResponse({"success": False, "message": "密码错误"}, status_code=403)

    # 过期校验 + 路径穿越守卫（防止 `../` 逃出共享目录）
    expire_time = datetime.strptime(share["expire"], "%Y-%m-%d %H:%M:%S")
    if datetime.now() > expire_time:
        return JSONResponse({"success": False, "message": "共享已过期"}, status_code=410)

    share_dir = security.safe_join(storage.UPLOAD_DIR, share["path"])
    video_path = security.safe_join(share_dir, filename)
    if video_path is None or not os.path.isfile(video_path):
        return JSONResponse({"success": False}, status_code=404)

    file_size = os.path.getsize(video_path)
    mtime = int(os.path.getmtime(video_path))
    media_type = _video_content_type(filename)

    # 缓存头：密码共享（带 access 令牌）不缓存；公开共享边缘缓存 1 小时
    cache_ctl = "no-store" if (access or password) else "public, s-maxage=3600, max-age=3600"
    etag = f'"{share_id}-{file_size}-{mtime}"'
    common_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": cache_ctl,
        "ETag": etag,
    }

    # 协商 ETag（If-None-Match 命中直接 304，省带宽）
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=common_headers)

    range_header = request.headers.get("range")
    if range_header:
        try:
            start_s, end_s = range_header.replace("bytes=", "").split("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else file_size - 1
        except ValueError:
            return JSONResponse({"success": False}, status_code=416)
        end = min(end, file_size - 1)
        if start > end or start >= file_size:
            return Response(status_code=416, headers={
                **common_headers,
                "Content-Range": f"bytes */{file_size}",
            })
        length = end - start + 1

        def iter_range():
            # 4MB 分块（原 1MB）：弱服务器下减少系统调用
            with open(video_path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(4 * 1024 * 1024, remaining))
                    if not chunk:
                        break
                    yield chunk
                    remaining -= len(chunk)

        return StreamingResponse(
            iter_range(),
            status_code=206,
            media_type=media_type,
            headers={
                **common_headers,
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(length),
            },
        )

    return FileResponse(video_path, media_type=media_type, headers=common_headers)
