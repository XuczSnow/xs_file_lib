#!/usr/bin/env python3
"""XS File Library 一键安装脚本（跨平台：Windows / Linux / macOS，幂等可重跑）。

用法：
    python install.py                      # 检测依赖 + 建 venv + 装依赖 + 生成启动器
    python install.py --ipv6               # 监听 IPv4+IPv6 双栈（serve.py）
    python install.py --service            # 额外安装系统服务（开机自启）
    python install.py -p 9000              # 指定端口（默认 8000）

前置依赖（只检测 + 提示，不自动安装）：
    - Python >= 3.10（当前解释器）
    - ffmpeg（视频缩略图 / DASH 分片用；缺则警告不阻断）
    - git（在线更新用；缺则警告不阻断）
    - uv（可选；无 uv 时自动退回标准库 venv + pip）

自动配置（幂等）：
    - git safe.directory 追加项目根（防 dubious ownership 拒绝在线更新的 git fetch；
      路径动态取本目录，已含则跳过）

--service（使能即装，失败再提示）：
    Linux   : 生成 xs-file-lib.service（systemd）；有 sudo → 装系统服务，
              无 sudo → 降级 user unit；再失败 → 打印手动三步
    Windows : 生成 service_run.bat + 计划任务 XsFileLibService；
              管理员 → ONSTART/SYSTEM 开机自启，普通用户 → ONLOGON 登录自启
              安装失败 → 打印 NSSM 备选
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
import textwrap

ROOT = os.path.dirname(os.path.abspath(__file__))
IS_WIN = os.name == "nt"
VENV_DIR = os.path.join(ROOT, ".venv")
VENV_PY = os.path.join(VENV_DIR, "Scripts", "python.exe") if IS_WIN \
    else os.path.join(VENV_DIR, "bin", "python")
RUNTIME_DIRS = ["uploads", "cache", os.path.join("cache", "thumbs"),
                os.path.join("cache", "zips"), os.path.join("cache", "dash"),
                os.path.join("cache", "video"), "data", "config"]


def ok(msg):
    print("  [OK]  " + msg)


def warn(msg):
    print("  [!]   " + msg)


def fail(msg):
    print("  [x]   " + msg)


def step(title):
    print("\n== " + title + " ==")


# ---------------------------------------------------------------- 前置检测

def check_python():
    v = sys.version_info
    if v < (3, 10):
        fail("需要 Python >= 3.10，当前 %d.%d —— 请安装新版 Python 后重试" % (v.major, v.minor))
        sys.exit(1)
    ok("Python %d.%d.%d" % (v.major, v.minor, v.micro))


def check_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        ok("ffmpeg: %s" % p)
        return
    warn("未检测到 ffmpeg（视频缩略图 / DASH 分片不可用，其余功能正常）。安装方法：")
    print(textwrap.indent("Windows: winget install Gyan.FFmpeg\n"
                          "Linux:   sudo apt install ffmpeg   （或 dnf install ffmpeg）\n"
                          "macOS:   brew install ffmpeg", "          "))


def check_git():
    p = shutil.which("git")
    if p:
        ok("git: %s" % p)
        return
    warn("未检测到 git（设置页\"系统更新\"在线自更新不可用，基础功能正常）。")


def ensure_git_safe_dir():
    """把项目根加入 git 全局 safe.directory（幂等），防 dubious ownership 拒绝 fetch。

    仓库目录属主与当前用户不一致（跨账号克隆/网络目录/zip 解压等）时，git 2.35.2+
    会拒绝操作并报 'dubious ownership'。路径用本目录动态算出（不硬编码），
    已存在该值则跳过，避免每次安装堆重复条目。
    注意：git config --get-all 无值时退出码为 1（正常），需按 stdout 判而非退出码。"""
    if not shutil.which("git"):
        return   # 无 git 不配（也不影响基础安装）
    target = ROOT.replace("\\", "/")   # git 报错口径用正斜杠（D:/Work/...）
    r = subprocess.run(["git", "config", "--global", "--get-all", "safe.directory"],
                       capture_output=True, text=True)
    existing = r.stdout.split() if r.returncode in (0, 1) else []
    if target in existing:
        ok("git safe.directory 已含 %s" % target)
        return
    a = subprocess.run(["git", "config", "--global", "--add", "safe.directory", target],
                      capture_output=True, text=True)
    if a.returncode == 0:
        ok("已加入 git safe.directory: %s（在线更新 git fetch 不再报 dubious ownership）" % target)
    else:
        warn("git safe.directory 写入失败（%s），在线更新如遇 dubious ownership 请手动执行："
             % (a.stderr or a.stdout).strip()[:80])
        print("          git config --global --add safe.directory %s" % target)


# ---------------------------------------------------------------- venv

def _ensure_pip_in_venv():
    """venv 缺 pip 时引导安装（幂等）：uv 默认创建的 .venv 不带 pip 模块，
    当前终端又没有 uv 时，'复用 .venv' 分支走 venv 自带 pip 会直接失败。"""
    r = subprocess.run([VENV_PY, "-m", "pip", "--version"],
                      capture_output=True, text=True)
    if r.returncode == 0:
        return
    ok(".venv 无 pip 模块（uv 创建默认不带），用 ensurepip 引导安装 ...")
    e = subprocess.run([VENV_PY, "-m", "ensurepip", "--upgrade"],
                       capture_output=True, text=True)
    if e.returncode != 0:
        fail("ensurepip 引导 pip 失败：%s" % (e.stderr or e.stdout).strip()[:150])
        print("          解决：安装 uv 后重跑（winget install astral-sh.uv），或手动执行：")
        print("          uv pip install --python \"%s\" -r requirements.txt" % VENV_PY)
        sys.exit(1)
    ok("pip 已装入 .venv")


def setup_venv():
    uv = shutil.which("uv")
    if os.path.isfile(VENV_PY):
        ok("复用已有 .venv")
        # uv 创建的 venv 默认无 pip 模块 → 有 uv 时优先 uv pip（--python 指定目标 venv）；
        # 无 uv 时先 ensurepip 引导（uv venv 的 venv 里没有 pip 但标准库 ensurepip 可用）
        if uv:
            install_deps([uv, "pip", "install", "--python", VENV_PY,
                          "-r", os.path.join(ROOT, "requirements.txt")])
        else:
            _ensure_pip_in_venv()
            install_deps([VENV_PY, "-m", "pip", "install", "-r",
                          os.path.join(ROOT, "requirements.txt")])
    else:
        if uv:
            ok("检测到 uv，用 uv 创建 .venv")
            subprocess.run([uv, "venv", VENV_DIR], check=True, cwd=ROOT)
            install_deps([uv, "pip", "install", "--python", VENV_PY,
                          "-r", os.path.join(ROOT, "requirements.txt")])
            return
        warn("未检测到 uv，退回标准库 venv + pip（建议安装 uv 提速：\n"
             "          Windows: winget install astral-sh.uv\n"
             "          其它:    curl -LsSf https://astral.sh/uv/install.sh | sh")
        subprocess.run([sys.executable, "-m", "venv", VENV_DIR], check=True, cwd=ROOT)
        _ensure_pip_in_venv()
        install_deps([VENV_PY, "-m", "pip", "install", "-r",
                     os.path.join(ROOT, "requirements.txt")])


def install_deps(cmd):
    print("        安装/更新依赖（requirements.txt，幂等）...")
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        fail("依赖安装失败（exit=%d）" % r.returncode)
        sys.exit(1)
    ok("依赖安装完成")


# ---------------------------------------------------------------- 目录

def ensure_dirs():
    for d in RUNTIME_DIRS:
        p = os.path.join(ROOT, d)
        if not os.path.isdir(p):
            os.makedirs(p)
    ok("运行时目录就绪（uploads/cache/data/config）")


# ---------------------------------------------------------------- 启动命令

def launch_cmd(port, ipv6):
    """服务/启动器用的完整启动命令（双栈走 serve.py，显式 V6ONLY=0）。"""
    if ipv6:
        return [VENV_PY, "serve.py", "--host", "::", "--port", str(port)]
    return [VENV_PY, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", str(port)]


def launch_cmd_shell(port, ipv6):
    """写进 bat/sh 文本里的启动命令：python 路径加双引号（防目录含空格），
    路径用原生分隔符（Windows 反斜杠 / POSIX 正斜杠）——bat/sh 都无需转义。"""
    py = VENV_PY
    if ipv6:
        return '"%s" serve.py --host "::" --port %d' % (py, port)
    return '"%s" -m uvicorn main:app --host 0.0.0.0 --port %d' % (py, port)


def write_runners(port, ipv6):
    if IS_WIN:
        # launch_cmd_shell 已带引号 + 原生反斜杠路径，bat 直接用（不需再转义）
        bat = "@echo off\r\n" + \
              "rem XS File Library launcher (generated by install.py, idempotent)\r\n" + \
              "cd /d \"%s\"\r\n" % ROOT + \
              launch_cmd_shell(port, ipv6) + "\r\n" + \
              "pause\r\n"
        p = os.path.join(ROOT, "run.bat")
        with open(p, "w", encoding="ascii", errors="replace") as f:
            f.write(bat)
        ok("已生成 run.bat")
    else:
        sh = "#!/bin/sh\n# XS File Library 启动器（install.py 生成，幂等）\n" + \
             "cd \"%s\"\nexec %s\n" % (ROOT, launch_cmd_shell(port, ipv6))
        p = os.path.join(ROOT, "run.sh")
        with open(p, "w", encoding="utf-8") as f:
            f.write(sh)
        os.chmod(p, 0o755)
        ok("已生成 run.sh")


def write_service_runner(port, ipv6):
    """service_run.bat：Windows 计划任务 / NSSM 的服务动作体（Linux 用 venv 直接跑）。"""
    if IS_WIN:
        bat = "@echo off\r\ncd /d \"%s\"\r\n%s\r\n" % (ROOT, launch_cmd_shell(port, ipv6))
        p = os.path.join(ROOT, "service_run.bat")
        with open(p, "w", encoding="ascii", errors="replace") as f:
            f.write(bat)
        return p
    # POSIX：服务动作 = venv python 直接跑（systemd 生成 service 文件，无需额外 runner）
    return None


# ---------------------------------------------------------------- restart_command 联动
# install 按实际装出的服务类型，把对应重启命令预填进 config/config.json 的
# restart_command（设置页"系统更新"卡片 loadSettings 自动回显，前端零改动）。
# 覆盖策略：空 → 写入；等于已知默认值 → 覆盖；用户自定义值 → 保留不动。

KNOWN_RESTART_CMDS = [
    "systemctl restart xs-file-lib",
    "systemctl --user restart xs-file-lib",
    'nssm restart XsFileLib',
    'schtasks /End /TN "XsFileLibService" && schtasks /Run /TN "XsFileLibService"',
]


def _config_path():
    return os.path.join(ROOT, "config", "config.json")


def _load_config_file():
    p = _config_path()
    if not os.path.isfile(p):
        return None
    import json
    try:
        data = json.loads(open(p, "r", encoding="utf-8").read())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _save_config_file(data: dict):
    """原子写 config.json（.tmp + os.replace，与 storage.save_json 同模式，防半截文件）。"""
    import json
    p = _config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def find_nssm():
    """找 nssm 可执行文件：用户 PATH 优先，其次项目根目录（便携版放根目录也能用）。"""
    p = shutil.which("nssm")
    if p:
        return p
    for name in ("nssm.exe", "nssm"):
        cand = os.path.join(ROOT, name)
        if os.path.isfile(cand):
            return cand
    return None


def sync_restart_command(cmd: str, action: str = "pre-fill"):
    """按覆盖策略把 cmd 写进 config.json 的 restart_command。返回实际动作。

    - 现值为空                    → 写入
    - 现值属于 KNOWN_RESTART_CMDS → 覆盖（重装/换平台场景）
    - 现值为用户自定义            → 保留不动（防误伤），打印提示"""
    cfg = _load_config_file() or {}
    cur = (cfg.get("restart_command") or "").strip()
    if cur == cmd:
        return "unchanged"
    if cur and cur not in KNOWN_RESTART_CMDS:
        print("          [i]   检测到自定义 restart_command（%s），未覆盖；"
              "如需改用安装默认请手动清空设置页输入框" % cur)
        return "kept-custom"
    cfg["restart_command"] = cmd
    _save_config_file(cfg)
    print("          [OK]  已%s restart_command = %s" % (action, cmd))
    return "written"


def clear_restart_command_if_known():
    """uninstall 用：restart_command 是已知默认值（服务已不存在）→ 清空；自定义保留。"""
    cfg = _load_config_file()
    if not cfg:
        return
    cur = (cfg.get("restart_command") or "").strip()
    if cur and cur not in KNOWN_RESTART_CMDS:
        print("          [i]   自定义 restart_command（%s）保留未动" % cur)
        return
    if cur:
        cfg["restart_command"] = ""
        _save_config_file(cfg)
        print("          [OK]  已清空 restart_command（对应服务已移除）")


# ---------------------------------------------------------------- 服务

def linux_service(port, ipv6):
    unit = "xs-file-lib"
    cmd = " ".join(launch_cmd(port, ipv6))
    svc_text = textwrap.dedent("""
        [Unit]
        Description=XS File Library
        After=network.target

        [Service]
        Type=simple
        WorkingDirectory=%s
        ExecStart=%s
        Restart=on-failure
        Environment=PATH=%s

        [Install]
        WantedBy=multi-user.target
        """) % (ROOT, cmd, os.pathsep.join(
            [os.path.dirname(shutil.which("ffmpeg")) or ""] + ["/usr/bin", "/bin"]))
    svc_text = svc_text.strip() + "\n"

    # 1) 有 sudo → 系统服务
    if shutil.which("sudo"):
        target = "/etc/systemd/system/%s.service" % unit
        try:
            with open(os.path.join(ROOT, "%s.service" % unit), "w", encoding="utf-8") as f:
                f.write(svc_text)
            subprocess.run(["sudo", "cp", os.path.join(ROOT, "%s.service" % unit), target], check=True)
            subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
            subprocess.run(["sudo", "systemctl", "enable", "--now", unit], check=True)
            ok("systemd 系统服务已安装并启动（%s）" % target)
            print("          管理: systemctl status/restart %s" % unit)
            sync_restart_command("systemctl restart xs-file-lib")
            return True
        except (subprocess.CalledProcessError, OSError) as e:
            warn("sudo 安装系统服务失败（%s），降级 user unit" % e)
    # 2) 无 sudo / 失败 → user unit
    try:
        import pathlib
        udir = pathlib.Path.home() / ".config" / "systemd" / "user"
        udir.mkdir(parents=True, exist_ok=True)
        upath = udir / ("%s.service" % unit)
        upath.write_text(svc_text, encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", unit], check=True)
        ok("systemd user 服务已安装并启动（登录会话内运行）")
        print("          提示: user unit 会随登出停止；长期驻留请执行 loginctl enable-linger $USER")
        print("          管理: systemctl --user status/restart %s" % unit)
        sync_restart_command("systemctl --user restart xs-file-lib")
        return True
    except (subprocess.CalledProcessError, OSError) as e:
        fail("user 服务安装失败（%s）。请手动执行：" % e)
        print(textwrap.indent("sudo tee /etc/systemd/system/%s.service < %s/%s.service\n"
                              "sudo systemctl daemon-reload && sudo systemctl enable --now %s"
                              % (unit, ROOT, unit, unit), "          "))
        print("          （service 文件已生成在 %s/%s.service）" % (ROOT, unit))
        return False


def _install_nssm(nssm, port, ipv6):
    """用 NSSM 建真 Windows 服务（XsFileLib：开机自启 + 崩溃守护）。成功返回 True。

    步骤：已存在先 remove（幂等）→ install → 设 AppDirectory（main.py 用相对路径）
    → AppStdout/Stderr 重定向 logs/（防 NSSM 失败弹窗）→ start（start 失败但服务
    已建成仍算成功，可手动重试）。"""
    svc = "XsFileLib"
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    if ipv6:
        app_args = "serve.py --host :: --port %d" % port
    else:
        app_args = "-m uvicorn main:app --host 0.0.0.0 --port %d" % port
    if subprocess.run(["sc", "query", svc], capture_output=True).returncode == 0:
        subprocess.run([nssm, "remove", svc, "confirm"], capture_output=True)
    r = subprocess.run([nssm, "install", svc, VENV_PY, app_args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        warn("nssm install 失败（普通用户建服务需管理员权限）：%s"
             % (r.stderr or r.stdout).strip()[:150])
        return False
    subprocess.run([nssm, "set", svc, "AppDirectory", ROOT], capture_output=True)
    subprocess.run([nssm, "set", svc, "AppStdout", os.path.join(ROOT, "logs", "service.log")],
                   capture_output=True)
    subprocess.run([nssm, "set", svc, "AppStderr", os.path.join(ROOT, "logs", "service.log")],
                   capture_output=True)
    st = subprocess.run([nssm, "start", svc], capture_output=True, text=True)
    if st.returncode != 0:
        warn("nssm start 失败（服务已建成，可稍后手动 nssm start %s）：%s"
             % (svc, (st.stderr or st.stdout).strip()[:150]))
    ok("NSSM 服务 %s 已创建（开机自启 + 崩溃守护）" % svc)
    print("          管理: nssm status/restart/stop %s" % svc)
    print("          日志: %s\\logs\\service.log" % ROOT.replace("/", "\\"))
    sync_restart_command("nssm restart XsFileLib")
    return True


def windows_service(port, ipv6):
    """返回 (成功?, 类型描述)。NSSM 优先，失败回落计划任务。"""
    # NSSM 优先（PATH 或项目根目录检测到即走真服务 + 崩溃守护；失败回落计划任务）
    nssm = find_nssm()
    if nssm:
        print("          检测到 NSSM: %s" % nssm)
        if _install_nssm(nssm, port, ipv6):
            return True, "NSSM 服务 XsFileLib（开机自启 + 崩溃守护）"
        warn("NSSM 路径失败，回落计划任务（schtasks）")
    task = "XsFileLibService"
    runner = write_service_runner(port, ipv6)   # 生成 service_run.bat
    if not runner:
        fail("未生成服务动作脚本")
        return False, ""
    runner_arg = '"%s"' % runner      # bat 路径加引号（防含空格目录），原生反斜杠
    # 幂等：已存在先删（不存在则忽略）
    subprocess.run(["schtasks", "/Delete", "/TN", task, "/F"],
                   capture_output=True)
    # 管理员判断：net session 成功 = 有权限
    is_admin = subprocess.run(["net", "session"], capture_output=True).returncode == 0
    if is_admin:
        args = ["schtasks", "/Create", "/TN", task, "/SC", "ONSTART",
                "/RU", "SYSTEM", "/RL", "HIGHEST", "/F", "/TR", runner_arg]
    else:
        warn("非管理员权限：尝试创建\"登录时触发\"任务（当前用户，不守护崩溃）")
        args = ["schtasks", "/Create", "/TN", task, "/SC", "ONLOGON",
                "/F", "/TR", runner_arg]
    print("          执行: %s" % " ".join(args))
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode == 0:
        trigger = "开机自启（SYSTEM）" if is_admin else "登录自启（当前用户）"
        ok("计划任务 %s 已创建（%s）" % (task, trigger))
        print("          手动触发: schtasks /Run /TN \"%s\"" % task)
        print("          停止:     schtasks /End /TN \"%s\"" % task)
        sync_restart_command('schtasks /End /TN "XsFileLibService" && schtasks /Run /TN "XsFileLibService"')
        if not is_admin:
            print("          提示: 如需开机自启 + 崩溃守护，请以管理员运行本脚本，"
                  "或在 PATH/项目根放 nssm.exe（https://nssm.cc/）后重跑")
        return True, "计划任务 XsFileLibService（%s）" % trigger
    fail("计划任务创建失败：%s" % (r.stderr or r.stdout).strip()[:200])
    print("          备选 1：以管理员身份重新运行  python install.py --service")
    print("          备选 2（NSSM，https://nssm.cc/，放项目根或 PATH 后重跑即自动使用）：")
    print(textwrap.indent("nssm install XsFileLib \"%s\" \"%s\""
                          % (VENV_PY, "serve.py --host :: --port %d" % port if ipv6
                             else "-m uvicorn main:app --host 0.0.0.0 --port %d" % port),
                          "          "))
    print("          当前可先用 run.bat 手动启动，不影响基础功能")
    return False, ""


def install_service(port, ipv6):
    """安装系统服务；返回 (成功?, 类型描述) 供收尾文案区分。"""
    if platform.system() == "Windows":
        return windows_service(port, ipv6)
    ok = linux_service(port, ipv6)
    return ok, ("systemd unit xs-file-lib" if ok else "")


# ---------------------------------------------------------------- 收尾

def print_next_steps(port, ipv6, with_service, service_ok=True, service_kind=""):
    listen = "::" if ipv6 else "0.0.0.0"
    print("\n== 完成 ==")
    print("  监听: %s:%d%s" % (listen, port, "（IPv4+IPv6 双栈）" if ipv6 else ""))
    if with_service:
        if service_ok:
            print("  服务: %s" % (service_kind or "已启用"))
            print("  restart_command: 已按安装类型预填到 config.json"
                  "（设置页\"系统更新\"卡片自动回显；自定义值不会被覆盖）")
        else:
            print("  服务: 安装失败（见上方提示），请手动处理或稍后重试")
            print("  手动启动: %s" % ("run.bat" if IS_WIN else "./run.sh"))
    else:
        print("  手动启动: %s" % ("run.bat" if IS_WIN else "./run.sh"))
    print("  默认账号: admin / admin123（首次启动生成，登录后请立即改密码）")
    if not (with_service and service_ok):
        print("  在线更新: 设置页\"系统更新\"卡片 restart_command 可填")
        if platform.system() == "Windows":
            print('    计划任务: schtasks /End /TN "XsFileLibService" && schtasks /Run /TN "XsFileLibService"')
            print("    NSSM:     nssm restart XsFileLib")
        else:
            print("    systemctl restart xs-file-lib（或 --user 版）")
    print("  可选环境变量（不设用默认）: XS_TOKEN_SECRET / XS_TOKEN_TTL /")
    print("    XS_SHARE_ACCESS_TTL / XS_MAX_UPLOAD_MB")


# ---------------------------------------------------------------- 卸载

def uninstall_service():
    """删除 install --service 安装的服务对象（全部幂等，失败只警告不中断）。

    Windows: 计划任务 XsFileLibService + NSSM 服务 XsFileLib（若装了）+ service_run.bat
    Linux:   systemd user unit + 系统 unit（sudo）+ 生成的 xs-file-lib.service 文本
    只删服务，不碰 .venv / run.* / 业务数据（uploads/data/config/cache）。"""
    step("卸载服务")
    removed = []
    if IS_WIN:
        r = subprocess.run(["schtasks", "/Query", "/TN", "XsFileLibService"],
                           capture_output=True)
        if r.returncode == 0:
            d = subprocess.run(["schtasks", "/Delete", "/TN", "XsFileLibService", "/F"],
                               capture_output=True, text=True)
            if d.returncode == 0:
                ok("已删除计划任务 XsFileLibService")
                removed.append("计划任务 XsFileLibService")
            else:
                warn("计划任务删除失败：%s" % (d.stderr or d.stdout).strip()[:120])
        else:
            warn("计划任务 XsFileLibService 未找到，跳过")
        # NSSM 服务（install 成功装过才有）：sc query 到才移除；nssm 可执行文件
        # 从 PATH / 项目根探测（find_nssm），都没有才提示手动 sc delete
        q = subprocess.run(["sc", "query", "XsFileLib"], capture_output=True, text=True)
        if q.returncode == 0:
            nssm = find_nssm()
            if nssm:
                r2 = subprocess.run([nssm, "remove", "XsFileLib", "confirm"],
                                     capture_output=True, text=True)
                if r2.returncode == 0:
                    ok("已移除 NSSM 服务 XsFileLib")
                    removed.append("NSSM 服务 XsFileLib")
                else:
                    warn("NSSM 移除失败，请手动: sc delete XsFileLib")
            else:
                warn("检测到 NSSM 服务 XsFileLib，但 PATH/项目根均无 nssm，请手动: sc delete XsFileLib")
        else:
            warn("NSSM 服务 XsFileLib 未找到，跳过")
        bat = os.path.join(ROOT, "service_run.bat")
        if os.path.isfile(bat):
            os.remove(bat)
            ok("已删除 service_run.bat")
            removed.append("service_run.bat")
    else:
        unit = "xs-file-lib"
        # 1) user unit
        q = subprocess.run(["systemctl", "--user", "status", unit],
                           capture_output=True, text=True)
        if "could not be found" not in q.stdout and q.returncode == 0:
            subprocess.run(["systemctl", "--user", "disable", "--now", unit], capture_output=True)
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            p = os.path.expanduser("~/.config/systemd/user/%s.service" % unit)
            if os.path.isfile(p):
                os.remove(p)
            ok("已移除 systemd user 服务 %s" % unit)
            removed.append("systemd user unit %s" % unit)
        # 2) 系统 unit（需 sudo）
        sp = "/etc/systemd/system/%s.service" % unit
        if os.path.isfile(sp) and shutil.which("sudo"):
            r2 = subprocess.run(["sudo", "systemctl", "disable", "--now", unit],
                                 capture_output=True, text=True)
            if r2.returncode == 0:
                subprocess.run(["sudo", "rm", "-f", sp], capture_output=True)
                subprocess.run(["sudo", "systemctl", "daemon-reload"], capture_output=True)
                ok("已移除 systemd 系统服务 %s" % unit)
                removed.append("systemd 系统 unit %s" % unit)
            else:
                warn("系统服务移除失败，请手动: sudo systemctl disable --now %s && sudo rm %s" % (unit, sp))
        elif os.path.isfile(sp):
            warn("系统 unit 存在但无 sudo，请手动: sudo systemctl disable --now %s && sudo rm %s" % (unit, sp))
        gen = os.path.join(ROOT, "%s.service" % unit)
        if os.path.isfile(gen):
            os.remove(gen)
            ok("已删除生成的 %s.service" % unit)
            removed.append("%s.service（生成文件）" % unit)
    # 服务移除后，restart_command 里是安装预填的已知默认值 → 清空；自定义保留
    clear_restart_command_if_known()
    return removed


def print_leftover_files():
    """卸载后打印"剩余文件"清单（只提示，不删除；业务数据绝不碰）。"""
    step("剩余文件（未删除，按需自行清理）")
    leftovers = []
    if os.path.isdir(VENV_DIR):
        leftovers.append(".venv/（虚拟环境）")
    for f in ("run.bat", "run.sh"):
        if os.path.isfile(os.path.join(ROOT, f)):
            leftovers.append(f)
    if not leftovers:
        ok("无剩余安装产物")
    else:
        for item in leftovers:
            warn("剩余: " + item)
        print("          业务数据与配置未删除：uploads/ data/ config/ cache/（含账号、共享链接、")
        print("          HMAC 密钥、缩略图/分片/转码缓存）——如需彻底清理请自行删除整个目录")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="XS File Library 一键安装/卸载")
    ap.add_argument("--service", action="store_true", help="安装系统服务（开机自启）")
    ap.add_argument("--ipv6", action="store_true",
                    help="同时监听 IPv4+IPv6（双栈，走 serve.py）")
    ap.add_argument("-p", "--port", type=int, default=8000, help="监听端口（默认 8000）")
    ap.add_argument("--uninstall", action="store_true",
                    help="仅卸载服务（计划任务/systemd/NSSM + 生成文件），不删业务数据")
    args = ap.parse_args()

    if args.uninstall:
        removed = uninstall_service()
        print("\n== 卸载完成（仅移除服务）==")
        print("  已移除: " + ("、".join(removed) if removed else "无（未检测到已安装服务）"))
        print_leftover_files()
        return

    step("前置检测")
    check_python()
    check_ffmpeg()
    check_git()
    ensure_git_safe_dir()

    step("虚拟环境")
    setup_venv()

    step("运行时目录")
    ensure_dirs()

    step("启动器")
    write_runners(args.port, args.ipv6)

    service_ok = True
    service_kind = ""
    if args.service:
        step("系统服务")
        service_ok, service_kind = install_service(args.port, args.ipv6)

    print_next_steps(args.port, args.ipv6, args.service, service_ok, service_kind)


if __name__ == "__main__":
    main()
