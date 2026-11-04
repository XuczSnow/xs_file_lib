"""系统设置：站点名称、文件根目录。"""
from fastapi import APIRouter, Request

from utils import storage

router = APIRouter(tags=["settings"])


@router.get("/api/settings")
def get_settings():
    return storage.load_config()


@router.post("/api/settings")
async def save_settings(request: Request):
    data = await request.json()
    storage.save_json(storage.SETTING_CONF, data)

    # 应用新的文件根目录（通过 storage 模块属性共享给所有路由）
    storage.UPLOAD_DIR = storage.load_config().get("root_path", "uploads")

    return {"success": True}
