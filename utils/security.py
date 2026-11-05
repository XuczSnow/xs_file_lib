"""安全加固工具：路径穿越守卫、登录限流、会话 token、密码哈希。

原散落在各 router 的裸 os.path.join(UPLOAD_DIR, user_input) 与 SHA-256
密码哈希集中到这里，统一提供：

- safe_join()       把"上传根目录 + 用户输入相对路径"安全拼合，
                    阻止 `../` 等路径穿越；同时暴露 safe_filename() 只取 basename。
- 登录限流          per-IP 失败计数 + 冷却窗口（防暴力破解）。
- 会话 token        签名令牌（HMAC-SHA256，无第三方依赖），
                    管理 API 用它鉴权，公开分享/上传页保持开放。
- 密码哈希          bcrypt 优先，存量 SHA-256 记录在下次登录时透明升级。

说明：本项目以 JSON 文件存储、单机运行，故 token 采用带密钥的签名式实现
（而非标准 JWT/RS256），避免引入额外依赖。
密钥优先级：环境变量 XS_TOKEN_SECRET > config/secret.key（首次启动
自动随机生成并持久化，不随代码分发）；签名算法可日后平滑替换为 PyJWT。
"""
import hashlib
import hmac
import json
import os
import time
import threading

try:
    import bcrypt as _bcrypt
    _HAS_BCRYPT = True
except Exception:  # pragma: no cover - bcrypt 缺失时退回 SHA-256
    _bcrypt = None
    _HAS_BCRYPT = False

# ---- 会话 token ----
# 令牌密钥：优先环境变量 XS_TOKEN_SECRET（多实例/多机部署时注入同一密钥）；
# 否则使用 config/secret.key（首次启动随机生成 32 字节并持久化，仅存在于
# 部署机磁盘、不随代码分发），杜绝"源码里写死密钥"导致 token 可离线伪造。
# 默认有效期 12 小时（秒）
_TOKEN_TTL = int(os.environ.get("XS_TOKEN_TTL", str(12 * 3600)))
_SECRET_CACHE = None


def _token_secret() -> str:
    """取令牌密钥（进程内缓存一次）。惰性 import storage 避免循环引用。"""
    global _SECRET_CACHE
    if _SECRET_CACHE is None:
        env_secret = os.environ.get("XS_TOKEN_SECRET")
        if env_secret:
            _SECRET_CACHE = env_secret
        else:
            from utils import storage
            _SECRET_CACHE = storage.load_or_create_secret()
    return _SECRET_CACHE


# ==================== 路径穿越守卫 ====================

def safe_join(base_dir: str, user_path: str) -> str:
    """把用户输入相对路径安全拼到 base_dir 下，阻止路径穿越。

    - 先 os.path.normpath 消解 `.`/`..`；
    - 再确认结果仍落在 base_dir 内（防止 `../../etc/passwd` 之类逃逸）；
    - 越界则返回 None，由调用方决定拒绝响应。

    用于所有 os.path.join(storage.UPLOAD_DIR, <用户输入>) 的入口。
    """
    base = os.path.abspath(base_dir)
    if user_path is None or user_path == "":
        return base
    candidate = os.path.abspath(os.path.join(base, user_path))
    # 保证 candidate 等于 base 或以 base + 分隔符开头
    if candidate != base and not candidate.startswith(base + os.sep):
        return None
    return candidate


def safe_filename(name: str) -> str:
    """只取上传文件名的 basename 部分，剔除目录片段与 `..`，
    防止 `a/b/c.txt` 或 `../../x` 作为文件名造成写入越界。"""
    if not name:
        return ""
    # 统一分隔符后取最后一段，去掉空段
    cleaned = name.replace("\\", "/").split("/")[-1].strip()
    return cleaned or "file"


# ==================== 登录限流（防暴力破解）====================

class _RateLimiter:
    """按 IP 记录登录失败次数与冷却截止时间；线程安全。

    策略：失败累计到 max_failures 次即冻结 cooldown 秒，期间一律拒绝；
    成功登录或冷却结束后重置。
    """

    def __init__(self, max_failures: int = 5, cooldown: int = 300):
        self.max_failures = max_failures
        self.cooldown = cooldown
        self._state: dict = {}
        self._lock = threading.Lock()

    def allowed(self, ip: str) -> bool:
        with self._lock:
            rec = self._state.get(ip)
            if not rec:
                return True
            if rec["locked_until"] and rec["locked_until"] > time.time():
                return False
            # 冷却期已过，自动清零重新计数
            if rec["locked_until"] and rec["locked_until"] <= time.time():
                rec["fails"] = 0
                rec["locked_until"] = 0
            return True

    def record_failure(self, ip: str):
        with self._lock:
            rec = self._state.setdefault(ip, {"fails": 0, "locked_until": 0})
            rec["fails"] += 1
            if rec["fails"] >= self.max_failures:
                rec["locked_until"] = time.time() + self.cooldown

    def record_success(self, ip: str):
        with self._lock:
            rec = self._state.get(ip)
            if rec:
                rec["fails"] = 0
                rec["locked_until"] = 0


# 单例限流器（进程内共享）
_login_limiter = _RateLimiter(max_failures=5, cooldown=300)


def login_allowed(ip: str) -> bool:
    return _login_limiter.allowed(ip)


def login_failed(ip: str):
    _login_limiter.record_failure(ip)


def login_succeeded(ip: str):
    _login_limiter.record_success(ip)


# ==================== 会话 token（签名式）====================

def _sign(payload_b64: str) -> str:
    """对 payload 做 HMAC-SHA256 签名（密钥来自 env 或 config/secret.key）。"""
    return hmac.new(
        _token_secret().encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def issue_token(username: str, permissions: list) -> str:
    """生成签名字符串令牌：base64(json).hmac。

    前端登录成功后持有，后续管理 API 请求在 Authorization 头或 token
    查询参数里带上，中间件校验通过才放行。
    """
    payload = {
        "sub": username,
        "perms": permissions or [],
        "exp": int(time.time()) + _TOKEN_TTL,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    import base64
    b64 = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    return f"{b64}.{_sign(b64)}"


def verify_token(token: str):
    """校验 token，返回 (username, permissions)；无效/过期返回 None。"""
    if not token or "." not in token:
        return None
    b64, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(b64)):
        return None
    import base64
    try:
        raw = base64.urlsafe_b64decode(b64.encode("ascii")).decode("utf-8")
        payload = json.loads(raw)
    except Exception:
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    return payload.get("sub"), payload.get("perms", [])


# ==================== 密码哈希（bcrypt + SHA-256 透明迁移）====================

def hash_password(password: str) -> str:
    """哈希密码。

    优先用 bcrypt（带随机盐，抗字典/彩虹表）。bcrypt 哈希以 `$2` 开头，
    与 SHA-256 的 64 位十六进制串天然区分，便于存量数据识别。
    bcrypt 缺失时退回 SHA-256（保持与旧行为一致）。
    """
    if _HAS_BCRYPT:
        return _bcrypt.hashpw(
            password.encode("utf-8"), _bcrypt.gensalt()
        ).decode("utf-8")
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(stored: str, password: str) -> bool:
    """校验密码。stored 可能是 bcrypt 或旧 SHA-256 哈希，都能验证。

    bcrypt 哈希（$2 开头）用 bcrypt.checkpw；
    其余按 SHA-256 十六进制比较（大小写不敏感）。
    """
    if not stored:
        return False
    if stored.startswith("$2"):
        if not _HAS_BCRYPT:
            # 退到 SHA-256 兜底（部署未装 bcrypt 的极端情形）
            return hashlib.sha256(password.encode("utf-8")).hexdigest() == stored.lower()
        try:
            return _bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
        except Exception:
            return False
    # 旧 SHA-256 记录
    return hashlib.sha256(password.encode("utf-8")).hexdigest() == stored.lower()


def needs_upgrade(stored: str) -> bool:
    """判断存量的旧 SHA-256 哈希是否需要在下次登录时升级到 bcrypt。"""
    return bool(stored) and not stored.startswith("$2")


# ==================== 共享密码访问令牌（短期）====================

# 密码保护共享的访问令牌有效期（秒），默认 1 小时。
# /api/share-check 校验密码成功后签发；后续 content/download/video
# 请求携带该令牌，避免把明文密码写进 URL/访问日志。
_SHARE_ACCESS_TTL = int(os.environ.get("XS_SHARE_ACCESS_TTL", str(3600)))


def issue_share_access(share_id: str) -> str:
    """为通过密码校验的共享签发短期访问令牌（share_id 绑定 + 过期时间）。"""
    import base64
    payload = {
        "share": share_id,
        "exp": int(time.time()) + _SHARE_ACCESS_TTL,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    b64 = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    return f"{b64}.{_sign(b64)}"


def verify_share_access(token: str, share_id: str):
    """校验共享访问令牌：签名正确、未过期、且绑定到指定 share_id。"""
    if not token or "." not in token:
        return False
    b64, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(b64)):
        return False
    import base64
    try:
        payload = json.loads(base64.urlsafe_b64decode(b64.encode("ascii")).decode("utf-8"))
    except Exception:
        return False
    if int(payload.get("exp", 0)) < time.time():
        return False
    return payload.get("share") == share_id


# ==================== 上传限制 ====================

# 单次上传大小上限（字节）与允许扩展名白名单（管理页/访客上传共用）
MAX_UPLOAD_BYTES = int(os.environ.get("XS_MAX_UPLOAD_MB", "100")) * 1024 * 1024
ALLOWED_EXTENSIONS = [
    # 文档
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "csv",
    "json", "log",
    # 图片
    "jpg", "jpeg", "png", "gif", "webp", "bmp",
    # 视频/音频
    "mp4", "webm", "ogg", "mov", "avi", "mp3", "wav", "m4a", "flac",
    # 压缩包
    "zip", "rar", "7z", "tar", "gz",
    # 代码/其它
    "py", "js", "html", "css", "csv",
]


def extension_allowed(filename: str) -> bool:
    """按扩展名白名单判断文件是否允许上传（无扩展名视为不允许）。"""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in ALLOWED_EXTENSIONS
