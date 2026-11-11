"""用户管理：列表、新增、删除、修改密码。"""
import json

from fastapi import APIRouter, Form

from utils import storage

router = APIRouter(tags=["users"])


@router.get("/api/users")
def get_users():
    """用户列表。对外不下发密码哈希（前端仅使用用户名与权限）。"""
    users = storage.load_users()
    return {
        name: {"permissions": info.get("permissions", [])}
        for name, info in users.items()
    }


@router.post("/api/change-password")
def change_password(username: str = Form(...), new_password: str = Form(...)):
    users = storage.load_users()
    if username not in users:
        return {"success": False}

    users[username]["password"] = storage.hash_password(new_password)
    storage.save_users(users)

    return {"success": True}


@router.post("/api/add-user")
def add_user(
    username: str = Form(...),
    password: str = Form(...),
    permissions: str = Form("[]")
):
    users = storage.load_users()
    # 用户名校验：非空、不含会破坏 JSON 键 / 内联事件 / 链接拼接的字符
    name = (username or "").strip()
    if not name or any(ch in name for ch in ('/', '\\', '"', "'", '\n')):
        return {"success": False, "message": "无效的用户名"}
    try:
        perms = json.loads(permissions)
        if not isinstance(perms, list):
            raise ValueError
    except (ValueError, TypeError):
        return {"success": False, "message": "权限格式错误"}
    users[name] = {
        "password": storage.hash_password(password),
        "permissions": perms,
    }
    storage.save_users(users)
    return {"success": True}


@router.post("/api/delete-user")
def delete_user(username: str = Form(...)):
    users = storage.load_users()
    username = (username or "").strip()
    if username not in users:
        return {"success": False, "message": "用户不存在"}
    # 防锁死：最后一位拥有 user_manage 权限的用户（即最后的"管理员"）不可删除，
    # 否则删掉后无人能再登录管理界面
    target_perms = users[username].get("permissions") or []
    if "user_manage" in target_perms:
        has_other_admin = any(
            "user_manage" in (info.get("permissions") or [])
            for n, info in users.items() if n != username
        )
        if not has_other_admin:
            return {"success": False,
                    "message": "不能删除：这是最后一位管理员，删除后将无法再登录管理"}
    del users[username]
    storage.save_users(users)
    return {"success": True}
