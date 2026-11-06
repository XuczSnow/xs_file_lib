"""文件管理：浏览、上传、下载、删除、重命名、文件夹与文件夹权限。"""
import json
import os
import shutil
from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from utils import storage, filetree, security

router = APIRouter(tags=["files"])


@router.get("/api/files")
def get_files(username: str):
    result = {}

    # 目录不存在时返回空结果，避免首次启动/路径配置错误直接 500
    if not os.path.isdir(storage.UPLOAD_DIR):
        return result

    folder_permissions = storage.load_folder_permissions()

    for root, dirs, files in os.walk(storage.UPLOAD_DIR):
        rel_dir = os.path.relpath(root, storage.UPLOAD_DIR)
        folder_name = rel_dir

        if folder_name in folder_permissions:
            allowed_users = folder_permissions[folder_name]
            if allowed_users and username not in allowed_users:
                continue

        result[rel_dir] = {}

        folder_size = 0
        file_count = 0
        folder_count = len(dirs)
        result[rel_dir]["files"] = []

        for file in files:
            full_path = os.path.join(root, file)
            folder_size += os.path.getsize(full_path)
            file_count += 1
            result[rel_dir]["files"].append({
                "name": file,
                "path": os.path.join(rel_dir, file).replace("\\", "/"),
                "size": os.path.getsize(full_path),
                "modified": datetime.fromtimestamp(
                    os.path.getmtime(full_path)
                ).strftime("%Y-%m-%d %H:%M"),
                "extension": os.path.splitext(file)[1][1:].lower()
            })

        result[rel_dir]["_meta"] = {
            "file_count": file_count,
            "folder_count": folder_count,
            "size": folder_size,
        }

    return result


@router.get("/api/files/tree")
def get_files_tree(username: str):
    """嵌套树状文件结构：管理页树状显示用。

    权限过滤：设置了目录权限且当前用户不在允许名单中的目录，
    其子内容不展开（返回空节点）。
    """
    if not os.path.isdir(storage.UPLOAD_DIR):
        return {}

    folder_permissions = storage.load_folder_permissions()
    return filetree.build_file_tree(
        storage.UPLOAD_DIR,
        folder_permissions=folder_permissions,
        username=username,
    )


@router.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    folder: str = Form("")
):
    # 扩展名白名单：拒绝不在清单内的文件类型
    if not security.extension_allowed(file.filename or ""):
        raise HTTPException(status_code=400, detail="不允许上传该文件类型")

    # 目录路径守卫：folder 不能以 ../ 等逃出上传根目录
    target_dir = security.safe_join(storage.UPLOAD_DIR, folder)
    if target_dir is None:
        raise HTTPException(status_code=400, detail="非法的目录路径")
    os.makedirs(target_dir, exist_ok=True)

    # 文件名只取末段 basename，防止 ../ 写入越界
    save_path = os.path.join(target_dir, security.safe_filename(file.filename))

    # 流式写入并做大小上限（security.max_upload_bytes()，0=不限制），超限即中断并清理
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
                raise HTTPException(status_code=413, detail="文件超过大小限制")
            buffer.write(chunk)

    return {"success": True}


@router.delete("/api/delete/{filepath:path}")
def delete(filepath: str):
    # 路径穿越守卫：拒绝逃出上传根目录的路径；且禁止删除根目录本身
    base = os.path.abspath(storage.UPLOAD_DIR)
    path = security.safe_join(base, filepath)
    if path is None or path == base:
        raise HTTPException(status_code=400, detail="非法的文件路径")
    if os.path.exists(path):
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    return {"success": True}


@router.get("/api/download/{filepath:path}")
def download(filepath: str):
    # 路径穿越守卫 + 存在性校验，避免把请求打到根目录之外的文件
    path = security.safe_join(storage.UPLOAD_DIR, filepath)
    if path is None or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path)


@router.get("/api/folders")
def get_folders():
    folders = []
    if not os.path.isdir(storage.UPLOAD_DIR):
        return folders
    for item in os.listdir(storage.UPLOAD_DIR):
        path = os.path.join(storage.UPLOAD_DIR, item)
        if os.path.isdir(path):
            folders.append(item)
    return folders


@router.post("/api/create-folder")
def create_folder(folder_name: str = Form(...)):
    # 路径穿越守卫：新建目录必须落在上传根目录内
    base = os.path.abspath(storage.UPLOAD_DIR)
    path = security.safe_join(base, folder_name)
    if path is None or path == base:
        raise HTTPException(status_code=400, detail="非法的文件夹名称")
    os.makedirs(path, exist_ok=True)
    return {"success": True}


@router.post("/api/rename")
async def rename_file(request: Request):
    """重命名：原路径与目标名都经 safe_join 校验，只取 basename 防越界。"""
    data = await request.json()
    path = data["path"]
    new_name = security.safe_filename(data["new_name"])
    if not new_name:
        raise HTTPException(status_code=400, detail="无效的新名称")

    base = os.path.abspath(storage.UPLOAD_DIR)
    old_path = security.safe_join(base, path)
    if old_path is None:
        raise HTTPException(status_code=400, detail="非法的文件路径")

    new_path = os.path.join(os.path.dirname(old_path), new_name)
    # 目标仍需落在根目录内
    if not (new_path == base or new_path.startswith(base + os.sep)):
        raise HTTPException(status_code=400, detail="非法的新名称")

    os.rename(old_path, new_path)
    return {"success": True}


# ---- 文件夹权限 ----

@router.get("/api/folder-permissions")
def get_folder_permissions():
    return storage.load_folder_permissions()


@router.post("/api/folder-permissions")
def set_folder_permissions(
    folder: str = Form(...),
    users: str = Form(...)
):
    permissions = storage.load_folder_permissions()

    if len(users) == 0:
        permissions.pop(folder, None)
    else:
        permissions[folder] = json.loads(users)

    storage.save_folder_permissions(permissions)
    return {"success": True}
