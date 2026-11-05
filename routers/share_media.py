"""共享媒体访问：共享内容（树/扁平）、单文件与文件夹下载、密码校验、视频流。

从原 routers/shares.py 拆出（A2），路由路径与前端调用保持不变；
共享 helper（find_share / share_unlocked / video_content_type）位于
utils/share.py，避免跨路由循环依赖。
"""
import os
from datetime import datetime

from fastapi import APIRouter, Form, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from utils import storage, filetree, security
from utils import share as share_util

router = APIRouter(tags=["share-media"])


@router.get("/api/share-content/{share_id}")
def share_content(share_id: str, password: str = "", access: str = ""):
    share = share_util.find_share(share_id)
    if share is None:
        return {"success": False}

    # 密码闸门：设了密码的共享需携带 access 令牌（/api/share-check 校验后签发）或正确密码
    if not share_util.share_unlocked(share, share_id, password, access):
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
    share = share_util.find_share(share_id)
    if share is None:
        return {"success": False, "message": "共享不存在"}

    if not share_util.share_unlocked(share, share_id, password, access):
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
    share = share_util.find_share(share_id)
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
    share = share_util.find_share(share_id)
    if share is None:
        return {"success": False, "message": "共享不存在"}

    if not share_util.share_unlocked(share, share_id, password, access):
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


@router.get("/api/video-stream/{share_id}/{filename:path}")
def share_video_stream(share_id: str, filename: str, request: Request,
                       password: str = "", access: str = ""):
    """支持 Range 的视频流。

    P0 优化（弱服务器 / Cloudflare 场景）：
    - 同步 def：FastAPI 放线程池执行，慢客户端不再阻塞事件循环；
    - 4MB 分块（原 1MB）：减少系统调用次数；
    - Content-Type 按扩展名推断（原写死 video/mp4，webm/mov 会解码异常）；
    - ETag + Cache-Control：公开流 s-maxage=3600（让 CF 边缘缓存整对象、
      拖动/回看由边缘合成 206）；带 access 密码令牌的流 no-store
      （防共享过期后缓存仍可访问，防令牌进入缓存键）。

    注意：{filename:path} 到达处理器前已被 ASGI 解码，不要再 unquote。
    密码闸门：设了密码的共享需 URL 携带 access 令牌（?access=xxx）。
    """
    share = share_util.find_share(share_id)
    if share is None:
        return JSONResponse({"success": False}, status_code=404)

    # 密码闸门（视频流是直链，无法弹框收密码，故用 share-check 签发的 access 令牌）
    if not share_util.share_unlocked(share, share_id, password, access):
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
    media_type = share_util.video_content_type(filename)

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
