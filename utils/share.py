"""共享链接公共工具：查记录、密码/令牌闸门、视频 Content-Type。

供 routers/shares.py、routers/share_media.py、routers/share_log.py
三个路由模块共用，避免跨路由互相 import 造成循环依赖。
"""
import os

from utils import storage, security


def find_share(share_id: str):
    """按 ID 查共享记录（kind != upload）；不存在返回 None。

    用 .get 避免旧实现直接下标取值的 KeyError。
    """
    return storage.load_shares().get(share_id)


def share_unlocked(share: dict, share_id: str, password: str = "", access: str = "") -> bool:
    """共享访问闸门：

    - 未设密码：放行；
    - 设置了密码：需携带正确的短期访问令牌（access，由 /api/share-check
      校验密码后签发），或本次请求带上正确明文密码。
      用令牌而非把明文密码写进下载 URL，避免密码进入访问日志/分享链接。
    """
    if not share.get("password"):
        return True
    if access and security.verify_share_access(access, share_id):
        return True
    return security.verify_password(share["password"], password or "")


# 视频 Content-Type 按扩展名推断（弱服务器 + 浏览器解码正确性）；未知类型给通用值
_VIDEO_CONTENT_TYPES = {
    "mp4": "video/mp4", "m4v": "video/mp4",
    "webm": "video/webm", "ogv": "video/ogg",
    "mov": "video/quicktime", "mkv": "video/x-matroska",
    "avi": "video/x-msvideo", "ts": "video/mp2t",
}


def video_content_type(filename: str) -> str:
    """按扩展名推断视频 Content-Type；未知类型返回通用二进制。"""
    ext = os.path.splitext(filename)[1].lstrip(".").lower()
    return _VIDEO_CONTENT_TYPES.get(ext, "application/octet-stream")
