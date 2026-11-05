"""应用入口。

路由按功能场景拆分在 routers/，公共工具在 utils/：

- routers/auth         登录（限流 + 令牌签发）
- routers/users        用户管理
- routers/files        文件管理（浏览/上传/下载/删除/重命名/文件夹/权限）
- routers/shares       共享链接（创建/校验/下载/日志/视频流/二维码）
- routers/media        图片与视频缩略图
- routers/settings     系统设置
- routers/upload_links 访客上传链接
- routers/zip_tasks    文件夹 zip 打包（两个入口共用底层实现）

安全边界：
- 登录接口签发签名 token（utils.security），管理/数据类 API 由下方
  require_token 中间件校验；公开分享页 / 访客上传页 / 媒体缩略图保持开放。
"""
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from routers import (
    auth,
    files,
    media,
    settings,
    share_log,
    share_media,
    shares,
    upload_links,
    users,
    zip_tasks,
)
from utils import security, storage

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")

# 首次启动创建目录与默认配置
storage.init_config()

for router in (
    auth.router,
    users.router,
    files.router,
    shares.router,        # 共享链接 CRUD + 落地页 + 二维码
    share_media.router,   # 共享内容/下载/密码校验/视频流（A2 拆出）
    share_log.router,     # 访问信息/日志/看板统计（A2 拆出）
    media.router,
    settings.router,
    upload_links.router,
    zip_tasks.router,
):
    app.include_router(router)


# ==================== 管理 API 鉴权中间件 ====================
# 受保护路径（管理/数据操作类）：命中且无有效 token → 401。
# 公开路径（登录、分享页、访客上传、媒体、二维码）保持开放。

_PROTECTED_PREFIXES = (
    "/api/files",           # 浏览/树/上传/下载/删除/建目录/重命名/权限
    "/api/delete",
    "/api/download",
    "/api/folders",
    "/api/create-folder",
    "/api/rename",
    "/api/folder-permissions",
    "/api/folder-zip-task",  # 管理面板打包（共享页走 /api/share-folder-zip-task，公开）
    "/api/users",
    "/api/change-password",
    "/api/add-user",
    "/api/delete-user",
    "/api/links",
    "/api/delete-link",
    "/api/delete-share",
    "/api/create-upload-link",
    "/api/share-access-log",
    "/api/share-stats",      # 访问数据看板（管理侧，需 token）
)

# 精确匹配（前缀会与公开路由 /api/share-*、/api/upload-* 冲突，故用整段匹配）
_PROTECTED_EXACT = (
    "/api/upload",    # POST 上传文件（公开的是 /api/upload-info、/api/guest-upload）
    "/api/share",     # POST 创建共享（公开的是 /api/share-info 等 /api/share-*）
    "/api/shares",    # 列全部共享记录（含密码哈希，管理侧；兼容旧接口）
)


def _is_protected(path: str, method: str = "") -> bool:
    """判断请求是否需 token。

    大部分按路径前缀/整段匹配；对 /api/settings 做"方法感知"：
    GET 开放（各页面取站点名/根目录），POST（改设置）需 token。
    """
    if path == "/api/settings" and method.upper() == "POST":
        return True
    return any(path == p for p in _PROTECTED_EXACT) or any(
        path.startswith(p + "/") or path == p for p in _PROTECTED_PREFIXES
    )


def _extract_token(request) -> str:
    """从 Authorization 头（Bearer）或 token 查询参数取令牌。"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return request.query_params.get("token", "")


@app.middleware("http")
async def require_token(request, call_next):
    if _is_protected(request.url.path, request.method):
        token = _extract_token(request)
        if not security.verify_token(token):
            return JSONResponse(
                {"success": False, "message": "未授权或登录已过期"},
                status_code=401,
            )
    return await call_next(request)


@app.get("/")
def home():
    return FileResponse("static/index.html")


@app.get("/dashboard")
def dashboard_page():
    """访问数据看板独立页（需 token；页面本身公开，数据接口受保护）。"""
    return FileResponse("static/dashboard.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
