"""在线自动更新：git 拉取 origin/main + 冒烟自检 + 可选 detached 重启。

设计（与部署形态配套：单实例 FastAPI + GitHub 远程仓库 + public 匿名 fetch）：

- check_update()：git fetch origin 后比较 HEAD 与 origin/<branch>，
  返回 {behind, current, latest, commits[], dirty[]}——只读操作，不碰工作区。
- apply_update()：
  1. update_enabled 关闭 / 已是最新 / 工作区脏（未提交改动）→ 拒绝；
  2. 旧 HEAD 记入 data/update_log.json（回滚依据，全程留痕）；
  3. git merge --ff-only origin/<branch>（public 仓库匿名 HTTPS，无 token 配置面）；
  4. 本次 diff 触及 requirements.txt 才 pip install（用当前解释器，不动系统环境）；
  5. 冒烟：py_compile 全量 + import main，失败 → 自动 reset 回旧 HEAD（服务保持旧版）；
  6. 配置了 restart_command 才调度 detached 重启（sleep 2 后执行，HTTP 响应先返回）；
     留空 = 不自动重启，由操作者手动重启（安全降级，不会把服务搞挂）。
- rollback()：24h 窗口内把代码 reset 回 update_log 最近一条的旧 HEAD（+ 同款重启调度）。

git 目录 / 分支 / 重启命令均动态读 config（与 DASH_DIR 同模式），测试可重定向；
git 目录默认 = 项目根（main.py 所在目录）。uploads/cache/data/config 均在 git
跟踪之外（.gitignore），merge 只动代码文件，数据零影响。
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time

from utils import storage

# 项目根（本文件位于 <root>/utils/）；实际 git 目录按调用动态读（_git_dir）
GIT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 更新日志：与 links/users 同放项目根 data/（业务数据，备份必保）
UPDATE_LOG = os.path.join(GIT_DIR, "data", "update_log.json")

_lock = threading.Lock()
_RL_TIMEOUT = 120          # fetch/merge 等 git 网络操作上限
_ROLLBACK_WINDOW_SEC = 86400   # 回滚仅对 24h 内的更新开放


def _git_dir() -> str:
    """动态取 git 工作目录（测试可重定向 storage 侧配置；默认项目根）。"""
    override = getattr(storage, "UPDATER_GIT_DIR", None)
    return override if override else GIT_DIR


def _branch() -> str:
    return (storage.load_config() or {}).get("update_branch") or "main"


def _restart_command() -> str:
    """重启命令（config 原样执行；空 = 不自动重启，操作者手动重启）。"""
    return ((storage.load_config() or {}).get("restart_command") or "").strip()


def _log_path() -> str:
    return getattr(storage, "UPDATER_LOG_CONF", None) or UPDATE_LOG


def _run_git(args, cwd, timeout=_RL_TIMEOUT):
    """跑一条 git 命令，返回 (rc, stdout, stderr)。

    dubious ownership 对策：服务进程常以 SYSTEM/其它用户身份运行，其全局
    git 配置里可能没有安装者配过的 safe.directory。因此每条 git 命令都带
    一次性的 `-c safe.directory=*`——仅对本次子进程调用生效，不写任何配置
    文件，任何用户身份下都确定性生效（比按仓库路径配值更稳：Windows 路径
    斜杠归一化行为无文档保证，`*` 则与路径形式无关）。"""
    p = subprocess.run(["git", "-c", "safe.directory=*", *args],
                      cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0 and "dubious ownership" in (p.stderr or ""):
        # 理论上不会到这里（-c 已覆盖）；若命中（极老 git 版本），给出手动命令
        p.stderr += ("\n[updater] 仍报 dubious ownership，请升级 git（>=2.35.2）"
                     "或手动执行: git config --global --add safe.directory %s 后重启服务"
                     % os.path.abspath(cwd))
    return p.returncode, p.stdout, p.stderr


def _load_log() -> list:
    if not os.path.isfile(_log_path()):
        return []
    try:
        data = json.loads(open(_log_path(), "r", encoding="utf-8").read())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _append_log(entry: dict):
    """追加一条更新记录（原子写，防并发半截文件）。"""
    entries = _load_log()
    entries.append(entry)
    os.makedirs(os.path.dirname(_log_path()), exist_ok=True)
    tmp = _log_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries[-50:], f, ensure_ascii=False, indent=2)   # 只留最近 50 条
    os.replace(tmp, _log_path())


def check_update() -> dict:
    """git fetch 后比较本地 HEAD 与 origin/<branch>。只读，不动工作区。

    返回 {ok, behind, current, latest, commits[], dirty[], error}。
    behind=0 表示已同步；dirty 非空时 apply 会被拒绝（防误伤未提交改动）。"""
    cwd, branch = _git_dir(), _branch()
    rc, _, err = _run_git(["fetch", "origin"], cwd)
    if rc != 0:
        return {"ok": False, "error": "git fetch 失败：" + (err.strip().splitlines() or [""])[0][:200]}
    rc, out, _ = _run_git(["rev-parse", "HEAD"], cwd)
    if rc != 0:
        return {"ok": False, "error": "当前目录不是 git 仓库（无法更新）"}
    current = out.strip()
    rc, out, _ = _run_git(["rev-parse", "origin/" + branch], cwd)
    if rc != 0:
        return {"ok": False, "error": "远程分支 origin/%s 不存在" % branch}
    latest = out.strip()
    # 落后的提交列表（新→旧）与计数
    rc, out, _ = _run_git(["log", "--oneline", "HEAD..origin/" + branch], cwd)
    commits = [l for l in out.splitlines() if l.strip()]
    rc, out, _ = _run_git(["status", "--porcelain", "--untracked-files=normal"], cwd)
    dirty = [_porcelain_path(l) for l in out.splitlines() if l.strip()]
    return {"ok": True, "behind": len(commits), "current": current[:8],
            "latest": latest[:8], "commits": commits[:20], "dirty": dirty}


def _porcelain_path(line: str) -> str:
    """git status --porcelain 一行 → 归一化路径。

    普通行是 `XY PATH`（XY 含 2 空格）取 l[3:]；rename/copy 行是
    `XY old -> new`（XY='R '/'C '），取"目标"（new）避免把 `old -> new` 整串
    当文件名显示（L3）；带引号路径（git 对特殊字符会引号包裹）原样保留。"""
    body = line[3:]
    if " -> " in body:
        body = body.split(" -> ", 1)[1]
    return body


def _dirty_files() -> list:
    cwd = _git_dir()
    rc, out, _ = _run_git(["status", "--porcelain"], cwd)
    if rc != 0:
        return ["<git status 失败>"]
    return [_porcelain_path(l) for l in out.splitlines() if l.strip()]


def apply_update(actor: str = "?") -> dict:
    """执行更新（管理员端点调用）。失败均安全停止：重启前失败 → 服务保持旧版。"""
    if not (storage.load_config() or {}).get("update_enabled", True):
        return {"ok": False, "error": "在线更新已在设置中禁用"}
    with _lock:
        cwd, branch = _git_dir(), _branch()
        if not os.path.isfile(os.path.join(cwd, ".git")) and not os.path.isdir(os.path.join(cwd, ".git")):
            return {"ok": False, "error": "当前目录不是 git 仓库（无法更新）"}
        dirty = _dirty_files()
        if dirty:
            return {"ok": False, "error": "工作区有未提交改动，已拒绝更新（防误伤）："
                                          + "；".join(dirty[:8])}
        # fetch + 比较
        chk = check_update()
        if not chk.get("ok"):
            return chk
        old_head = _run_git(["rev-parse", "HEAD"], cwd)[1].strip()
        if chk["behind"] == 0:
            return {"ok": True, "noop": True, "current": chk["current"],
                    "message": "已是最新版本（%s）" % chk["current"]}
        # merge --ff-only（public 仓库匿名 fetch；禁止任意 URL 注入）
        rc, _, err = _run_git(["merge", "--ff-only", "origin/" + branch], cwd)
        if rc != 0:
            return {"ok": False, "error": "git merge 失败：" + err.strip()[:300]}
        new_head = _run_git(["rev-parse", "HEAD"], cwd)[1].strip()
        # 依赖变化才装（diff 里含 requirements*.txt）
        rc, out, _ = _run_git(["diff", "--name-only", old_head + ".." + new_head], cwd)
        touched = [f for f in out.splitlines()
                   if f.startswith("requirements") and f.endswith(".txt")]
        pip_done = ""
        if touched:
            p = subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                              cwd=cwd, capture_output=True, text=True, timeout=600)
            pip_done = "ok" if p.returncode == 0 else "failed: " + (p.stderr or p.stdout).strip()[-200:]
        # 冒烟：py_compile 全量 + import main（import 会触发 storage.init_config 等幂等初始化）
        smoke_err = _smoke_check(cwd)
        if smoke_err:
            # 回退到旧 HEAD（代码恢复旧版，服务继续跑旧代码；新依赖残留无害）
            _run_git(["reset", "--hard", old_head], cwd)
            _append_log({"at": time.time(), "actor": actor, "from": old_head[:8],
                        "to": new_head[:8], "result": "smoke-failed-rolled-back",
                        "detail": smoke_err, "pip": pip_done})
            return {"ok": False, "error": "更新后自检失败，已自动回滚到旧版：" + smoke_err}
        _append_log({"at": time.time(), "actor": actor, "from": old_head[:8],
                     "to": new_head[:8], "result": "ok", "pip": pip_done})
        # 重启调度（配置优先：填了才执行，留空则手动重启）
        restarted = _schedule_restart()
        return {"ok": True, "from": old_head[:8], "to": new_head[:8],
                "restarted": restarted, "pip": pip_done,
                "message": ("更新完成（%s → %s），服务即将自动重启" % (old_head[:8], new_head[:8]))
                           if restarted else
                           ("更新完成（%s → %s），未配置重启命令——请手动重启服务" % (old_head[:8], new_head[:8]))}


def rollback(actor: str = "?") -> dict:
    """回滚到最近一次成功更新的旧 HEAD（24h 窗口内）。"""
    if not (storage.load_config() or {}).get("update_enabled", True):
        return {"ok": False, "error": "在线更新已在设置中禁用"}
    entries = [e for e in _load_log() if e.get("result") == "ok"]
    if not entries:
        return {"ok": False, "error": "没有可回滚的更新记录"}
    last = entries[-1]
    if time.time() - last.get("at", 0) > _ROLLBACK_WINDOW_SEC:
        return {"ok": False, "error": "最近一次更新已超过 24h，不再提供一键回滚"
                                      "（可手动 git reset --hard %s）" % last.get("from", "?")}
    with _lock:
        cwd = _git_dir()
        rc, _, err = _run_git(["reset", "--hard", last["from"]], cwd)
        if rc != 0:
            return {"ok": False, "error": "回滚失败：" + err.strip()[:200]}
        _append_log({"at": time.time(), "actor": actor, "from": last["to"],
                     "to": last["from"], "result": "rollback"})
        return {"ok": True, "to": last["from"], "restarted": _schedule_restart(),
                "message": "已回滚到 %s" % last["from"]}


def _smoke_check(cwd: str) -> str:
    """重启前自检：py_compile 全量 .py + import main。返回 "" = 通过。"""
    pyfiles = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (".git", ".tmp_pack_staging", "node_modules",
                                                 "__pycache__", "cache", "uploads", "data")]
        pyfiles += [os.path.join(root, f) for f in files if f.endswith(".py")]
    bad = []
    for p in pyfiles:
        try:
            import py_compile
            py_compile.compile(p, doraise=True)
        except Exception as e:
            bad.append("%s: %s" % (os.path.relpath(p, cwd), str(e)[:120]))
    if bad:
        return "；".join(bad[:3])
    try:
        # 把 cwd 加入 sys.path：真实 uvicorn 进程已导入 main（命中 sys.modules）；
        # 若将来在独立子进程里跑本自检且 cwd 不在 sys.path，仍能保证 import 到。
        if cwd not in sys.path:
            sys.path.insert(0, cwd)
        import main  # noqa: F401  触发路由/中间件导入链（幂等初始化）
    except Exception as e:
        return "import main 失败：" + str(e)[:160]
    return ""


def _schedule_restart() -> bool:
    """detached 调度重启命令（sleep 2 后执行，HTTP 响应先返回）。

    命令取 config.restart_command（操作者配置），原样交给 shell 执行——
    不与任何请求参数拼接，攻击面仅限"能进管理界面配命令的人"。留空返回 False。"""
    cmd = _restart_command()
    if not cmd:
        return False
    try:
        if os.name == "nt":
            # Windows：独立 cmd 延迟 2s 再执行（等 HTTP 响应发完，避免服务被即时重启
            # 掐断请求）；DETACHED_PROCESS | CREATE_NO_WINDOW 脱离本进程且不闪控制台窗。
            flags = 0x00000008 | 0x08000000   # DETACHED_PROCESS | CREATE_NO_WINDOW
            subprocess.Popen('timeout /t 2 /nobreak >nul & %s' % cmd, shell=True,
                             creationflags=flags, start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen("sleep 2 && %s" % cmd, shell=True,
                             start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def restart_now(actor: str = "?") -> dict:
    """手动重启：用当前配置的 restart_command 调度 detached 重启（更新/回滚同款机制）。

    未配置命令时返回明确提示（不猜命令）；调度成功后 HTTP 响应先返回，
    服务 sleep 2 后由 detached 进程重启，并写 update_log 留痕。"""
    if not (storage.load_config() or {}).get("update_enabled", True):
        return {"ok": False, "error": "在线更新已在设置中禁用"}
    cmd = _restart_command()
    if not cmd:
        return {"ok": False, "need_cmd": True,
                "error": "未配置重启命令——请在设置页「系统更新」卡片填写"
                         "（如 systemctl restart xs-file-lib）后重试，或手动重启服务"}
    if _schedule_restart():
        _append_log({"at": time.time(), "actor": actor, "result": "manual-restart",
                     "cmd": cmd})
        return {"ok": True, "message": "重启已调度，服务将在 2 秒内重启"}
    _append_log({"at": time.time(), "actor": actor, "result": "manual-restart-failed",
                 "cmd": cmd})
    return {"ok": False, "error": "重启调度失败（命令无法执行或权限不足）：%s" % cmd}


def latest_version() -> str:
    """读 release_note/ 下最新版本号（文件名去 .md，取 max，字符串排序足够 1.x 段）。"""
    rel = os.path.join(_git_dir(), "release_note")
    if not os.path.isdir(rel):
        return ""
    try:
        names = [f[:-3] for f in os.listdir(rel) if f.endswith(".md")]
        return max(names) if names else ""
    except OSError:
        return ""
