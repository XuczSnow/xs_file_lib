"""系统设置：站点名称、文件根目录、上传限制、共享默认值、安全与热写参数。"""
from fastapi import APIRouter, Request

from utils import storage

router = APIRouter(tags=["settings"])


@router.get("/api/settings")
def get_settings():
    """返回全部设置（GET 开放，各页面取站点名/根目录/默认值）。"""
    return storage.load_config()


@router.post("/api/settings")
async def save_settings(request: Request):
    """保存设置（受 token 保护），并把"运行时热参数"立即生效，无需重启：
    - 登录限流参数（security._login_limiter）
    - 热写 flush 间隔（hotstore 后台线程）
    其余项（上传限制/扩展名白名单/日志上限/token 有效期/缩略图尺寸等）
    本身按请求动态读取 config，保存后即生效。
    """
    data = await request.json()
    storage.save_json(storage.SETTING_CONF, data)

    # 应用新的文件根目录（通过 storage 模块属性共享给所有路由）
    storage.UPLOAD_DIR = storage.load_config().get("root_path", "uploads")

    # 运行时热生效：限流 + 热写间隔
    storage._apply_login_limiter_from_config()
    from utils import hotstore
    hotstore.apply_from_config()

    return {"success": True}
