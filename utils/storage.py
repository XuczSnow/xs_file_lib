"""配置存储与通用工具。

原 main.py 中散落的目录常量、JSON 配置文件读写、密码哈希、
首次启动初始化逻辑统一收敛到此处，供 routers/ 各模块复用。
"""
import json
import os
import threading

from utils import security

# ---- 目录与配置文件路径（按功能三分：config 人改 / data 业务数据 / cache 可再生产）----
UPLOAD_DIR = "./uploads"          # ④ 用户文件
THUMB_DIR = "./cache/thumbs"     # ③ 可再生产缓存（缩略图）
CACHE_ZIP_DIR = "./cache/zips"   # ③ 可再生产缓存（zip 打包）

# ① config/：仅保留"人手工可改"的轻量设置
FOLDER_PERMS_CONF = "./config/folder_permissions.json"
SETTING_CONF = "./config/config.json"
# ② data/：程序写入的业务数据（备份必保；含密钥）
USER_CONF = "./data/users.json"
ACCESS_LOG_CONF = "./data/access_log.json"
# 统一链接存储：共享（查看/下载）与访客上传链接合并为一个 JSON，
# 用记录内 kind 字段区分（"share" | "upload"），管理页统一入口增删。
LINK_CONF = "./data/links.json"
# HMAC 会话 token 密钥文件（S1：首次启动随机生成，仅存部署机磁盘，不入库）
SECRET_CONF = "./data/secret.key"

# 旧版分离存储（仅用于首次迁移，迁移后不再读写；仍留在 config/ 旧位置）
LEGACY_SHARE_CONF = "./config/shares.json"
LEGACY_UPLOAD_LINK_CONF = "./config/upload_links.json"

# 目录重组一次性迁移（旧路径 → 新路径）：旧存在且新不存在才 move，幂等
DIR_MIGRATIONS = [
    ("config/users.json", "data/users.json"),
    ("config/secret.key", "data/secret.key"),
    ("config/links.json", "data/links.json"),
    ("config/share_access_log.json", "data/access_log.json"),
]
# 迁移后删除的旧目录（内容属可再生产缓存，无需保留）
LEGACY_DIRS_TO_PURGE = [".thumbs"]

# 访问日志滚动上限（超过后只保留最近 N 条，防止 JSON 无界增长）
ACCESS_LOG_MAX = 5000

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


def _save_json_inlock(path, data):
    """原子写入底层实现（调用方须已持有 _save_lock，避免可重入死锁）。

    确保父目录存在（幂等），避免热写/迁移后目录未就绪时写盘报错。
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_json(path, data):
    """原子写入 JSON 配置（先写 .tmp 再 replace），避免并发写坏文件。"""
    with _save_lock:
        _save_json_inlock(path, data)


def apply_links(transform):
    """对 links.json 做原子的"读-改-写"（热写聚合落盘用）。

    transform(links: dict) -> dict，在同一把 _save_lock 内完成
    load + 变换 + 原子写，避免与并发 save_shares/save_links 交错写坏。
    """
    with _save_lock:
        links = load_json(LINK_CONF, {})
        links = transform(links)
        _save_json_inlock(LINK_CONF, links)
        return links


def apply_access_log(transform):
    """对 access_log.json 做原子的"读-改-写"（热写聚合落盘用）。"""
    with _save_lock:
        logs = load_json(ACCESS_LOG_CONF, [])
        logs = transform(logs)
        _save_json_inlock(ACCESS_LOG_CONF, logs)
        return logs


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
    """追加分享访问日志（热写优化：只进内存缓冲，后台线程定时落盘）。

    实际写盘由 utils.hotstore 的 flush 线程完成（每 30s 合并 + 滚动裁剪），
    请求路径零 fsync。函数保留作兼容入口，share_log 已直接调 hotstore。
    """
    from . import hotstore
    hotstore.buffer_access_log(data)


def prune_share_access_log(max_items: int = ACCESS_LOG_MAX):
    """访问日志滚动清理：超出上限只保留最近 max_items 条（S2）。"""
    logs = load_json(ACCESS_LOG_CONF, [])
    if len(logs) > max_items:
        save_json(ACCESS_LOG_CONF, logs[-max_items:])
        return len(logs) - len(logs[-max_items:])
    return 0


def load_or_create_secret() -> str:
    """S1：加载 HMAC 密钥；不存在则首次随机生成 32 字节（64 位 hex）并原子落盘。

    密钥只存在于部署机 data/secret.key（.gitignore 已排除 data/），
    不随代码分发，杜绝源码内置密钥导致 token 可离线伪造。
    """
    if os.path.exists(SECRET_CONF):
        content = open(SECRET_CONF, encoding="utf-8").read().strip()
        if content:
            return content
    import secrets
    key = secrets.token_hex(32)
    tmp = SECRET_CONF + ".tmp"
    with _save_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SECRET_CONF)
    return key


def load_expired_links(now=None) -> list:
    """S2：返回所有已过期（过期时间早于 now）的链接记录 [(link_id, record)]。

    共享与访客上传记录都有 expire 字段（"YYYY-MM-DD HH:MM:SS"）。
    供启动清理与定时清理共用，判断逻辑集中一处。
    """
    from datetime import datetime
    now = now or datetime.now()
    expired = []
    for link_id, rec in load_links().items():
        exp = rec.get("expire")
        if not exp:
            continue
        try:
            if datetime.strptime(exp, "%Y-%m-%d %H:%M:%S") < now:
                expired.append((link_id, rec))
        except ValueError:
            continue
    return expired


def delete_links(link_ids: list) -> int:
    """S2：批量删除链接记录（按 link_id），返回实际删除条数。"""
    if not link_ids:
        return 0
    links = load_links()
    removed = 0
    for link_id in link_ids:
        if links.pop(link_id, None) is not None:
            removed += 1
    if removed:
        save_links(links)
    return removed


def cache_max_bytes() -> int:
    """zip 缓存容量上限（字节）：读取设置 cache_max_gb，默认 10G。

    返回 0 表示不限制（cache_max_gb <= 0 时）。每次打包后与启动时
    由 ziptask.enforce_capacity() 按"最旧优先"裁剪到上限以内。
    """
    try:
        gb = float((load_config() or {}).get("cache_max_gb", 10))
    except (TypeError, ValueError):
        gb = 10
    if gb <= 0:
        return 0
    return int(gb * 1024 * 1024 * 1024)


def _migrate_directory_layout():
    """一次性目录重组迁移（幂等）：旧 config/ 下的业务数据搬到 data/。

    规则：旧路径存在且新路径不存在 → shutil.move（跨盘安全）；
    已迁移过则什么都不做。返回实际迁移的文件数。
    """
    import shutil
    moved = 0
    for old, new in DIR_MIGRATIONS:
        if os.path.isfile(old) and not os.path.exists(new):
            os.makedirs(os.path.dirname(new), exist_ok=True)
            shutil.move(old, new)
            moved += 1
    # 清理旧的可再生产缓存目录（.thumbs 已并入 cache/thumbs，内容可自动重建）
    for legacy in LEGACY_DIRS_TO_PURGE:
        try:
            if os.path.isdir(legacy):
                shutil.rmtree(legacy)
        except OSError:
            pass  # 文件被占用等异常不阻断启动，留待下次
    return moved


def init_config():
    """首次启动时创建目录与默认配置文件（应用启动时调用一次）。"""
    _migrate_directory_layout()

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(THUMB_DIR, exist_ok=True)
    os.makedirs(CACHE_ZIP_DIR, exist_ok=True)
    # 确保 config / data 目录存在（均被 .gitignore 忽略，属部署数据）
    os.makedirs(os.path.dirname(SETTING_CONF) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(USER_CONF) or ".", exist_ok=True)

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
        save_json(SETTING_CONF, {
            "site_name": "XS File Library",
            "root_path": "uploads",
            "public_base_url": "",
            # 二维码缓存容量上限（GB）：cache/zips 总量超过则按最旧优先裁剪
            "cache_max_gb": 10,
            "qr_style": {
                "fg_color": "000000",
                "bg_color": "FFFFFF",
                "error_correction": "M",
                "box_size": 8,
                "border": 2,
            },
        })

    # ---- 启动自检（S1 / S2 / A3 + 热写）----
    # S1：确保 HMAC 密钥已就位（首次随机生成并持久化到 data/secret.key）
    load_or_create_secret()
    # 热写优化：启动后台线程，定期把 view_count / 访问日志内存缓冲落盘
    from utils import hotstore
    hotstore.start_flusher()
    # S2：清理过期链接记录 + 访问日志滚动上限
    expired = load_expired_links()
    if expired:
        delete_links([link_id for link_id, _ in expired])
    prune_share_access_log()
    # A3：维护 zip 缓存 index 一致性（不做按时间过期删除；
    # zip 的生命周期由"文件夹变更即删旧重打包"驱动）
    # 容量上限：cache_max_gb（默认 10G，可配置，0=不限制）
    from utils import ziptask
    ziptask.sync_index()
    ziptask.enforce_capacity()
