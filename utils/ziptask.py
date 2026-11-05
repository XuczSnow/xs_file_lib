"""文件夹 zip 打包任务的统一实现。

「管理面板打包文件夹」与「共享页打包文件夹」两个入口共用这里的
底层逻辑：缓存目录、线程执行、状态表。

A3 缓存生命周期策略：
- 打包前按 index（.index.json）记录"文件夹 → 已生成 zip"的映射；
- 计算文件夹指纹（get_folder_hash），若指纹变化（文件夹有修改）则
  **删除该文件夹的旧 zip，重新打包新文件**，避免陈旧缓存残留；
- 不做按时间的过期删除（zip 长期保留直至对应文件夹再次变更）；
- sync_index() 只在启动时维护 index 一致性（剔除指向已不存在
  文件的映射条目），不删除任何 zip。
"""
import hashlib
import json
import os
import threading
import uuid
import zipfile

from . import storage

# task_id -> {status, zip_file, filename, message}
zip_tasks: dict = {}
_zip_lock = threading.Lock()

# 文件夹缓存映射文件（记录 abspath -> [zip 文件名]，用于变更时删旧包）
_INDEX_FILE = os.path.join(storage.CACHE_ZIP_DIR, ".index.json")


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


# ---------------- index 读写（文件夹 -> zip 映射） ----------------

def _index_load() -> dict:
    try:
        with open(_INDEX_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _index_save(index: dict):
    try:
        os.makedirs(storage.CACHE_ZIP_DIR, exist_ok=True)
        with open(_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _remove_zips(names: list):
    """按文件名删除 cache 目录下的 zip（容忍不存在）。"""
    for name in names:
        if not name:
            continue
        fp = os.path.join(storage.CACHE_ZIP_DIR, name)
        try:
            if os.path.isfile(fp):
                os.remove(fp)
        except OSError:
            pass


def invalidate_folder_zip(folder_path: str, keep_zip: str = None) -> int:
    """文件夹变更时删除该文件夹的旧 zip（A3：变更即删旧重打包）。

    保留 keep_zip（本次要写入的新包文件名），删除其余历史包。
    返回删除条数。
    """
    index = _index_load()
    key = os.path.abspath(folder_path)
    old = index.get(key, [])
    removed = 0
    for name in old:
        if name != keep_zip:
            _remove_zips([name])
            removed += 1
    index[key] = [keep_zip] if keep_zip else []
    _index_save(index)
    return removed


def sync_index() -> int:
    """启动维护：仅剔除 index 里指向已不存在 zip 的条目（不删任何 zip）。

    zip 本身按"文件夹变更即删旧重打包"的生命周期管理，不做按时间
    的过期删除；index 只负责映射一致性，返回清理的条目数。
    """
    index = _index_load()
    removed = 0
    for key, names in list(index.items()):
        kept = [n for n in names
                if os.path.isfile(os.path.join(storage.CACHE_ZIP_DIR, n))]
        if len(kept) != len(names):
            index[key] = kept
            removed += len(names) - len(kept)
        if not kept:
            del index[key]
    _index_save(index)
    return removed


def enforce_capacity() -> int:
    """按容量上限裁剪 zip 缓存（最旧优先删除），返回删除的文件数。

    上限取 storage.cache_max_bytes()（设置 cache_max_gb，默认 10G；
    0 表示不限制）。单遍按 mtime 升序累加，超限后从最旧的开始删，
    删到总量 <= 上限为止；裁剪时同步 index 映射。
    在启动自检与每次打包成功后调用。
    """
    limit = storage.cache_max_bytes()
    if limit <= 0 or not os.path.isdir(storage.CACHE_ZIP_DIR):
        return 0

    # 单遍统计：(mtime, name, size) + 总量
    entries = []
    total = 0
    for name in os.listdir(storage.CACHE_ZIP_DIR):
        if not name.endswith(".zip"):
            continue
        fp = os.path.join(storage.CACHE_ZIP_DIR, name)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        entries.append((st.st_mtime, name, st.st_size))
        total += st.st_size

    if total <= limit:
        return 0

    # 按 mtime 升序（最旧在前）删除，直到总量 <= 上限
    removed = 0
    for _, name, size in sorted(entries, key=lambda t: t[0]):
        if total <= limit:
            break
        try:
            os.remove(os.path.join(storage.CACHE_ZIP_DIR, name))
            total -= size
            removed += 1
        except OSError:
            pass

    if removed:
        # 同步 index：剔除指向已删 zip 的条目
        index = _index_load()
        changed = False
        for key, names in list(index.items()):
            kept = [n for n in names
                    if os.path.isfile(os.path.join(storage.CACHE_ZIP_DIR, n))]
            if kept != names:
                index[key] = kept
                changed = True
            if not kept:
                del index[key]
                changed = True
        if changed:
            _index_save(index)
    return removed


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
        cache_zip_name = f"{folder_hash}.zip"
        cache_zip = os.path.join(storage.CACHE_ZIP_DIR, cache_zip_name)

        # 已有缓存则直接复用（指纹未变 = 文件夹无修改）
        if os.path.exists(cache_zip):
            _set_task(task_id, {
                "status": "completed",
                "zip_file": cache_zip,
                "filename": os.path.basename(folder_path) + ".zip",
            })
            return

        # 指纹变化（文件夹有修改）：删除旧 zip，重新打包新文件
        invalidate_folder_zip(folder_path, keep_zip=cache_zip_name)

        with zipfile.ZipFile(cache_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(folder_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    arc_name = os.path.relpath(file_path, folder_path)
                    z.write(file_path, arc_name)

        # 打包成功后：按容量上限（cache_max_gb，默认 10G）裁剪缓存
        enforce_capacity()

        _set_task(task_id, {
            "status": "completed",
            "zip_file": cache_zip,
            "filename": os.path.basename(folder_path) + ".zip",
        })

    except Exception as e:
        _set_task(task_id, {"status": "failed", "message": str(e)})
