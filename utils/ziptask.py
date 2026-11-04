"""文件夹 zip 打包任务的统一实现。

「管理面板打包文件夹」与「共享页打包文件夹」两个入口共用这里的
底层逻辑：缓存目录、线程执行、状态表。
"""
import hashlib
import os
import threading
import uuid
import zipfile

from . import storage

# task_id -> {status, zip_file, filename, message}
zip_tasks: dict = {}
_zip_lock = threading.Lock()


def get_task(task_id: str) -> dict:
    with _zip_lock:
        return zip_tasks.get(task_id, {"status": "not_found"})


def _set_task(task_id: str, task: dict):
    with _zip_lock:
        zip_tasks[task_id] = task


def get_folder_hash(folder_path: str) -> str:
    """按文件内容 + 修改时间 + 大小计算目录指纹，用于 zip 缓存。"""
    md5 = hashlib.md5()
    for root, _, files in os.walk(folder_path):
        for file in sorted(files):
            file_path = os.path.join(root, file)
            stat = os.stat(file_path)
            md5.update(file.encode())
            md5.update(str(stat.st_mtime).encode())
            md5.update(str(stat.st_size).encode())
    return md5.hexdigest()


def start_zip_task(folder_path: str) -> str:
    """启动文件夹打包，返回任务 ID。

    被 /api/folder-zip-task/{folder_path} 与
    /api/share-folder-zip-task/{share_id} 两个入口共用。
    """
    task_id = str(uuid.uuid4())
    _set_task(task_id, {"status": "processing"})
    threading.Thread(
        target=_build_zip_task,
        args=(task_id, folder_path),
        daemon=True
    ).start()
    return task_id


def _build_zip_task(task_id: str, folder_path: str):
    try:
        folder_hash = get_folder_hash(folder_path)
        cache_zip = os.path.join(storage.CACHE_ZIP_DIR, f"{folder_hash}.zip")

        # 已有缓存则直接复用
        if os.path.exists(cache_zip):
            _set_task(task_id, {
                "status": "completed",
                "zip_file": cache_zip,
                "filename": os.path.basename(folder_path) + ".zip",
            })
            return

        with zipfile.ZipFile(cache_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(folder_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    arc_name = os.path.relpath(file_path, folder_path)
                    z.write(file_path, arc_name)

        _set_task(task_id, {
            "status": "completed",
            "zip_file": cache_zip,
            "filename": os.path.basename(folder_path) + ".zip",
        })

    except Exception as e:
        _set_task(task_id, {"status": "failed", "message": str(e)})
