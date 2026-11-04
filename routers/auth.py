"""登录认证：限流、bcrypt/SHA-256 兼容校验、令牌签发。

- 登录前按客户端 IP 做失败冷却（security.login_allowed）；
- 校验密码时支持 bcrypt 与旧 SHA-256 两种存量哈希（security.verify_password）；
- 成功登录后把旧 SHA-256 透明升级为 bcrypt（needs_upgrade 命中时回写）；
- 返回签名 token，前端后续管理 API 请求需携带它。
"""
from fastapi import APIRouter, Form, Request

from utils import security, storage

router = APIRouter(tags=["auth"])


def _client_ip(request: Request) -> str:
    """取客户端 IP（支持 CF-Connecting-IP 代理头）。"""
    ip = request.headers.get("CF-Connecting-IP", "").strip()
    if ip:
        return ip
    return request.client.host if request.client else "unknown"


@router.post("/api/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = _client_ip(request)

    # 登录限流：该 IP 处于冷却窗口时直接拒绝（防暴力破解）
    if not security.login_allowed(ip):
        return {"success": False, "message": "登录失败次数过多，请稍后再试"}

    users = storage.load_users()
    user = users.get(username)
    if not user or not security.verify_password(user.get("password", ""), password):
        security.login_failed(ip)
        return {"success": False, "message": "用户名或密码错误"}

    security.login_succeeded(ip)

    # 存量旧 SHA-256 哈希透明升级为 bcrypt（仅当命中时写一次）
    if security.needs_upgrade(user.get("password", "")):
        user["password"] = storage.hash_password(password)
        storage.save_users(users)

    token = security.issue_token(username, user.get("permissions", []))
    return {
        "success": True,
        "username": username,
        "permissions": user.get("permissions", []),
        "token": token,
    }
