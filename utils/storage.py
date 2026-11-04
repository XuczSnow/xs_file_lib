"""配置存储与通用工具。

原 main.py 中散落的目录常量、JSON 配置文件读写、密码哈希、
首次启动初始化逻辑统一收敛到此处，供 routers/ 各模块复用。
"""
import json
import os
import threading

from utils import security

# ---- 目录与配置文件路径（与原 main.py 保持一致）----
UPLOAD_DIR = "./uploads"
THUMB_DIR = "./.thumbs"
CACHE_ZIP_DIR = "./cache/zips"

USER_CONF = "./config/users.json"
FOLDER_PERMS_CONF = "./config/folder_permissions.json"
ACCESS_LOG_CONF = "./config/share_access_log.json"
SETTING_CONF = "./config/config.json"

# 统一链接存储：共享（查看/下载）与访客上传链接合并为一个 JSON，
# 用记录内 kind 字段区分（"share" | "upload"），管理页统一入口增删。
LINK_CONF = "./config/links.json"
# 旧版分离存储（仅用于首次迁移，迁移后不再读写）
LEGACY_SHARE_CONF = "./config/shares.json"
LEGACY_UPLOAD_LINK_CONF = "./config/upload_links.json"

_save_lock = threading.Lock()


def hash_password(password: str) -> str:
    """密码哈希：委托给 security 模块（bcrypt 优先，缺失时退回 SHA-256）。

    存量旧 SHA-256 哈希在下次成功登录时由 verify_password + needs_upgrade
    透明升级为 bcrypt，无需手动迁移。
    """
    return security.hash_password(password)


def load_json(path, default):
    """读取 JSON 配置；文件缺失或内容损坏时返回 default，避免启动崩溃。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    """原子写入 JSON 配置（先写 .tmp 再 replace），避免并发写坏文件。"""
    tmp = path + ".tmp"
    with _save_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


# ---- 各配置文件的便捷读写（替代原 main.py 中重复的 open/json 代码）----

def load_users():
    return load_json(USER_CONF, {})


def save_users(users):
    save_json(USER_CONF, users)


def load_folder_permissions():
    return load_json(FOLDER_PERMS_CONF, {})


def save_folder_permissions(permissions):
    save_json(FOLDER_PERMS_CONF, permissions)


# ---- 统一链接存储（共享 + 访客上传合并为一个 links.json）----

def load_links():
    """统一链接表：{ link_id: {..., "kind": "share"|"upload", ...} }。
    首次调用时自动把旧版分离的两个 JSON 迁移合并进来。"""
    migrate_legacy_links()
    return load_json(LINK_CONF, {})


def save_links(links):
    save_json(LINK_CONF, links)


# 迁移只做一次（进程内缓存），避免每次 load 都扫旧文件
_migrated = threading.Event()


def migrate_legacy_links():
    """把旧版 shares.json / upload_links.json 合并进统一 links.json（一次性）。"""
    global _migrated
    if _migrated.is_set():
        return
    _migrated.set()  # 先置位，防止并发重入

    merged = load_json(LINK_CONF, {})

    old_shares = load_json(LEGACY_SHARE_CONF, {})
    for share_id, record in old_shares.items():
        if share_id in merged:
            continue
        record["kind"] = "share"
        merged[share_id] = record

    old_uploads = load_json(LEGACY_UPLOAD_LINK_CONF, {})
    for link_id, record in old_uploads.items():
        if link_id in merged:
            continue
        record["kind"] = "upload"
        merged[link_id] = record

    save_json(LINK_CONF, merged)


# 兼容旧调用方：shares / upload_links 的便捷读写现在都走统一存储
def load_shares():
    """返回统一存储中所有 kind=="share" 的记录（兼容旧接口）。"""
    return {
        link_id: rec for link_id, rec in load_links().items()
        if rec.get("kind") != "upload"
    }


def save_shares(shares):
    """回写所有 kind=="share" 记录，保留 upload 记录不变。"""
    links = load_links()
    for link_id in [k for k, v in links.items() if v.get("kind") != "upload"]:
        links.pop(link_id, None)
    links.update({k: {**v, "kind": "share"} for k, v in shares.items()})
    save_links(links)


def load_upload_links():
    """返回统一存储中所有 kind=="upload" 的记录（兼容旧接口）。"""
    return {
        link_id: rec for link_id, rec in load_links().items()
        if rec.get("kind") == "upload"
    }


def save_upload_links(links):
    """回写所有 kind=="upload" 记录，保留 share 记录不变。"""
    all_links = load_links()
    for link_id in [k for k, v in all_links.items() if v.get("kind") == "upload"]:
        all_links.pop(link_id, None)
    all_links.update({k: {**v, "kind": "upload"} for k, v in links.items()})
    save_links(all_links)


def load_config():
    return load_json(SETTING_CONF, {"site_name": "XS File Library", "root_path": "uploads"})


def write_share_access_log(data):
    """追加分享访问日志。"""
    logs = load_json(ACCESS_LOG_CONF, [])
    logs.append(data)
    save_json(ACCESS_LOG_CONF, logs)


def init_config():
    """首次启动时创建目录与默认配置文件（应用启动时调用一次）。"""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(THUMB_DIR, exist_ok=True)
    os.makedirs(CACHE_ZIP_DIR, exist_ok=True)

    if not os.path.exists(USER_CONF):
        save_json(USER_CONF, {
            "admin": {
                "password": hash_password("admin123"),
                "permissions": [
                    "upload", "download", "delete",
                    "share", "share_manage", "user_manage"
                ],
            }
        })

    if not os.path.exists(FOLDER_PERMS_CONF):
        save_json(FOLDER_PERMS_CONF, {})

    if not os.path.exists(ACCESS_LOG_CONF):
        save_json(ACCESS_LOG_CONF, [])

    if not os.path.exists(LINK_CONF):
        save_json(LINK_CONF, {})

    if not os.path.exists(SETTING_CONF):
        save_json(SETTING_CONF, {"site_name": "XS File Library", "root_path": "uploads"})
