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
    users[username] = {
        "password": storage.hash_password(password),
        "permissions": json.loads(permissions),
    }
    storage.save_users(users)

    return {"success": True}


@router.post("/api/delete-user")
def delete_user(username: str = Form(...)):
    users = storage.load_users()
    if username in users:
        del users[username]
    storage.save_users(users)

    return {"success": True}
