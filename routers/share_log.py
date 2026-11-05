"""共享访问信息、访问日志与访问数据看板（管理页）。

从原 routers/shares.py 拆出（A2）：
- /api/share-info/{id}：首屏共享信息 + 计数 + 写访问日志
- /api/share-access-log/{id}：单链接访问日志
- /api/share-stats：聚合看板（PV/UV/TOP IP/按链接/按天）
共享 helper（find_share）位于 utils/share.py。
"""
import collections
from datetime import datetime

from fastapi import APIRouter, Request

from utils import storage, hotstore
from utils import share as share_util

router = APIRouter(tags=["share-log"])


@router.get("/api/share-info/{share_id}")
def share_info(share_id: str, request: Request):
    shares = storage.load_shares()
    if share_id not in shares:
        return {"success": False, "message": "共享不存在"}

    share = shares[share_id]

    # 热写优化：view_count 不再逐请求读改写 links.json（整文件 fsync），
    # 改为内存聚合（hotstore.bump_view_count），后台线程每 30s 落盘一次
    hotstore.bump_view_count(share_id)

    # 访问日志同样走内存缓冲（原实现在此逐请求 append + fsync 写回）
    hotstore.buffer_access_log({
        "share_id": share_id,
        "path": share["path"],
        "visit_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "real_ip": request.headers.get("CF-Connecting-IP", ""),
        "ip": request.client.host,
        "user_agent": request.headers.get("User-Agent", ""),
    })

    expired = datetime.now() > datetime.strptime(
        share["expire"], "%Y-%m-%d %H:%M:%S"
    )

    return {
        "success": True,
        "type": share.get("type", "file"),
        "share_mode": share.get("share_mode", "normal"),
        "name": share["path"],
        "expire": share["expire"],
        "expired": expired,
        "has_password": bool(share.get("password", "")),
    }


@router.get("/api/share-access-log/{share_id}")
def get_share_access_log(share_id: str):
    # 磁盘已落盘 + 内存未落盘缓冲 合并，读实时（写批量、读实时）
    logs = storage.load_json(storage.ACCESS_LOG_CONF, [])
    logs.extend(hotstore.pending_logs())
    return [item for item in logs if item.get("share_id") == share_id]


# ---- 访问数据看板（管理页"访问统计"：PV / UV / TOP IP / 按链接 / 按天）----

@router.get("/api/share-stats")
def share_stats():
    """聚合访问日志：总 PV/UV、各链接 PV、TOP IP、按天分布。

    供管理页"访问数据看板"渲染。受 token 中间件保护（管理侧）。
    去重口径：UV 按 real_ip（CF 前优先真实 IP）去重。
    日志 = 磁盘已落盘 + 内存未落盘缓冲（写批量、读实时）。
    """
    logs = storage.load_json(storage.ACCESS_LOG_CONF, [])
    logs.extend(hotstore.pending_logs())

    total_pv = len(logs)
    uv_set = set()
    per_link = collections.Counter()
    top_ip = collections.Counter()
    daily = collections.Counter()

    for item in logs:
        ip = item.get("real_ip") or item.get("ip") or ""
        if ip:
            uv_set.add(ip)
            top_ip[ip] += 1
        sid = item.get("share_id", "")
        if sid:
            per_link[sid] += 1
        # 按天（取 visit_time 前 10 位 YYYY-MM-DD）
        day = (item.get("visit_time") or "")[:10]
        if day:
            daily[day] += 1

    return {
        "total_pv": total_pv,
        "total_uv": len(uv_set),
        "per_link": dict(per_link),
        "top_ips": [
            {"ip": ip, "count": cnt} for ip, cnt in top_ip.most_common(10)
        ],
        "daily": dict(sorted(daily.items())),  # 按日期升序
    }
