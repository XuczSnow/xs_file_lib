"""访客上传链接：创建、页面、信息、匿名上传。"""
import os
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from utils import storage, security

router = APIRouter(tags=["upload-links"])


@router.post("/api/create-upload-link")
def create_upload_link(
    folder: str = Form(...),
    days: int = Form(...),
    password: str = Form("")
):
    # 路径穿越守卫：目标目录必须落在上传根目录内
    base = os.path.abspath(storage.UPLOAD_DIR)
    target_dir = security.safe_join(base, folder)
    if target_dir is None:
        raise HTTPException(status_code=400, detail="非法的目录路径")
    # 存相对路径，下载/上传时再拼回
    rel_folder = os.path.relpath(target_dir, base).replace(os.sep, "/")
    if rel_folder == ".":
        rel_folder = ""

    upload_id = str(uuid.uuid4())[:8]
    expire_time = datetime.now() + timedelta(days=days)

    links = storage.load_upload_links()
    links[upload_id] = {
        "folder": rel_folder,
        "expire": expire_time.strftime("%Y-%m-%d %H:%M:%S"),
        "password": storage.hash_password(password) if password else "",
        "upload_count": 0,
    }
    storage.save_upload_links(links)

    return {"success": True, "upload_id": upload_id}


@router.get("/upload/{upload_id}")
def upload_page(upload_id: str):
    return FileResponse("static/upload.html")


@router.get("/api/upload-info/{upload_id}")
def upload_info(upload_id: str):
    links = storage.load_upload_links()
    if upload_id not in links:
        return {"success": False}

    item = links[upload_id]
    expired = datetime.now() > datetime.strptime(item["expire"], "%Y-%m-%d %H:%M:%S")
    return {
        "success": not expired,
        "folder": item["folder"],
        "expire": item["expire"],
        "has_password": bool(item["password"]),
        "expired": expired,
    }


@router.post("/api/guest-upload")
async def guest_upload(
    upload_id: str = Form(...),
    password: str = Form(""),
    file: UploadFile = File(...)
):
    links = storage.load_upload_links()
    if upload_id not in links:
        return {"success": False, "message": "上传链接无效"}

    item = links[upload_id]

    # 过期校验
    expired = datetime.now() > datetime.strptime(item["expire"], "%Y-%m-%d %H:%M:%S")
    if expired:
        return {"success": False, "message": "上传链接已过期"}

    # 密码校验（兼容 bcrypt 与旧 SHA-256 存量哈希）
    if item["password"] and not security.verify_password(item["password"], password):
        return {"success": False, "message": "密码错误"}

    # 扩展名白名单
    if not security.extension_allowed(file.filename or ""):
        return {"success": False, "message": "不允许上传该文件类型"}

    # 目录守卫 + 文件名只取 basename（防止 ../ 写入越界）
    base = os.path.abspath(storage.UPLOAD_DIR)
    target_dir = security.safe_join(base, item["folder"] or "")
    if target_dir is None:
        return {"success": False, "message": "目标目录非法"}
    os.makedirs(target_dir, exist_ok=True)
    save_path = os.path.join(target_dir, security.safe_filename(file.filename))

    # 流式写入 + 大小上限（0=不限制）
    limit = security.max_upload_bytes()
    written = 0
    with open(save_path, "wb") as buffer:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if limit > 0 and written > limit:
                buffer.write(b"")
                os.remove(save_path)
                return {"success": False, "message": "文件超过大小限制"}
            buffer.write(chunk)

    item["upload_count"] = item.get("upload_count", 0) + 1
    storage.save_upload_links(links)

    return {"success": True}
