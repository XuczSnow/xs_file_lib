"""系统更新端点：检查/执行在线更新 + 回滚（管理员 user_manage 专属）。

鉴权模型（与全项目一致）：
- main.py 的 require_token 中间件已把 /api/system-* 纳入受保护路径（无 token → 401）；
- 本路由端点内再校验 user_manage 权限（非管理员 → 403），双保险。
- 更新/回滚会动 git 工作区与重启服务，仅限管理员；system-info/check-update
  也只对登录用户开放（避免向匿名暴露服务器 git 状态）。
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from utils import security, storage, updater

router = APIRouter(tags=["system"])


def _extract_token(request: Request) -> str:
    """从 Authorization 头（Bearer）或 token 查询参数取令牌（与 main._extract_token 同口径）。"""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query_params.get("token", "")


def _admin_guard(request: Request):
    """返回 (ok, message)：校验 user_manage 权限 + update_enabled 开关。

    校验顺序：token 有效 → 是管理员 → 更新功能未被设置禁用。"""
    verified = security.verify_token(_extract_token(request))
    if not verified:
        return False, "未授权或登录已过期"
    username, perms = verified
    if "user_manage" not in (perms or []):
        return False, "需要管理员（user_manage）权限"
    if not (storage.load_config() or {}).get("update_enabled", True):
        return False, "在线更新已在设置中禁用"
    return True, username


@router.get("/api/system-info")
def system_info(request: Request):
    """当前版本信息（登录即可）：git short hash / 最新版本号 / 最近 3 条版本更新记录。

    手动重启（manual-restart）不进"最近更新"列表（避免挤掉版本记录），
    单独折叠返回 restart_count + restart_last。"""
    if not security.verify_token(_extract_token(request)):
        return JSONResponse({"success": False, "message": "未授权"}, status_code=401)
    cwd = updater._git_dir()
    import subprocess
    rc, out, _ = updater._run_git(["rev-parse", "HEAD"], cwd)
    head = out.strip()[:8] if rc == 0 else ""
    full_log = updater._load_log()
    restarts = [e for e in full_log if str(e.get("result", "")).startswith("manual-restart")]
    version_log = [e for e in full_log if not str(e.get("result", "")).startswith("manual-restart")]
    return {
        "success": True,
        "commit": head,
        "version": updater.latest_version(),
        "update_log": version_log[-3:][::-1],
        "restart_count": len(restarts),
        "restart_last": restarts[-1]["at"] if restarts else 0,
    }


@router.get("/api/check-update")
def check_update(request: Request):
    """检查是否有远程更新（管理员）。只读 fetch + 比较，不动工作区。"""
    ok, username = _admin_guard(request)
    if not ok:
        code = 401 if "未授权" in username else 403
        return JSONResponse({"success": False, "message": username}, status_code=code)
    return updater.check_update()


@router.post("/api/apply-update")
def apply_update(request: Request):
    """执行更新（管理员）：git merge --ff-only + 冒烟自检 + 可选 detached 重启。"""
    ok, username = _admin_guard(request)
    if not ok:
        code = 401 if "未授权" in username else 403
        return JSONResponse({"success": False, "message": username}, status_code=code)
    return updater.apply_update(actor=username)


@router.post("/api/rollback-update")
def rollback(request: Request):
    """回滚到最近一次成功更新的旧 HEAD（管理员，24h 窗口内）。"""
    ok, username = _admin_guard(request)
    if not ok:
        code = 401 if "未授权" in username else 403
        return JSONResponse({"success": False, "message": username}, status_code=code)
    return updater.rollback(actor=username)


@router.post("/api/restart-service")
def restart_service(request: Request):
    """立即重启服务（管理员）：按配置的重启命令调度 detached 重启，HTTP 先返回。"""
    ok, username = _admin_guard(request)
    if not ok:
        code = 401 if "未授权" in username else 403
        return JSONResponse({"success": False, "message": username}, status_code=code)
    return updater.restart_now(actor=username)


# ---- 健康检查（公开，供 NSSM 守护 / 外部 uptime 监控 / 探活）----
# U3 A 基础版：只证明"服务进程活着且能响应"，返回版本号 + 时间戳。
# 刻意公开（不进 main.py 的 require_token 保护列表）——探活就是匿名调的，
# 同媒体缩略图 /api/share-info 的公开口径；不吐 config / 路径等敏感信息。

import time


@router.get("/api/health")
def health():
    """服务探活（公开，无需 token）。

    返回 {ok: true, version, ts}：
      - ok 恒为 true（端点可达即 true，探活只回答"活没活"）；
      - version = 当前 release_note 最新版本号（updater.latest_version，读目录毫秒级）；
      - ts = 服务端 Unix 时间戳（供调用方判断时钟/新鲜度）。
    不缓存（无 Cache-Control），每次实探。"""
    return {
        "ok": True,
        "version": updater.latest_version(),
        "ts": int(time.time()),
    }
