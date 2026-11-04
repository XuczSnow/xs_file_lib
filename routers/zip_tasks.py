"""文件夹 zip 打包入口。

两个入口（管理面板 / 共享页）共用 utils.ziptask 的同一底层实现，
仅目标文件夹的解析方式不同；状态查询与下载统一。
"""
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from utils import storage, ziptask, security

router = APIRouter(tags=["zip"])


@router.post("/api/folder-zip-task/{folder_path:path}")
def folder_zip_task(folder_path: str):
    """管理面板：直接以 UPLOAD_DIR 下相对路径打包（safe_join 防穿越）。"""
    target = security.safe_join(storage.UPLOAD_DIR, folder_path)
    if target is None:
        raise HTTPException(status_code=400, detail="非法的目录路径")
    return {"task_id": ziptask.start_zip_task(target)}


@router.post("/api/share-folder-zip-task/{share_id}")
def share_folder_zip_task(share_id: str):
    """共享页：先解析共享记录，再复用同一打包实现。"""
    share = storage.load_shares().get(share_id)
    if share is None:
        return {"success": False, "message": "共享不存在"}
    target = security.safe_join(storage.UPLOAD_DIR, share["path"])
    if target is None:
        return {"success": False, "message": "非法的目录路径"}
    return {"task_id": ziptask.start_zip_task(target)}


@router.get("/api/share-folder-zip-status/{task_id}")
def zip_status(task_id: str):
    return ziptask.get_task(task_id)


@router.get("/api/share-folder-zip-download/{task_id}")
def zip_download(task_id: str):
    task = ziptask.get_task(task_id)

    # 修复：原实现在任务不存在/未完成时直接取 zip_file 会 KeyError
    if task.get("status") != "completed":
        return {
            "success": False,
            "message": task.get("message", "打包尚未完成或任务不存在"),
            "task_id": task_id,
        }

    return FileResponse(
        task["zip_file"],
        filename=task["filename"],
        media_type="application/zip",
    )
