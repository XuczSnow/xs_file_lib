#!/usr/bin/env python3
"""双栈服务启动器：同端口监听 IPv4 + IPv6。

用法：
    python serve.py --host :: --port 8000        # 双栈（:: 同端口收 IPv4/IPv6）
    python serve.py --host 0.0.0.0 --port 8000   # 纯 IPv4（与 uvicorn CLI 等价）

背景：
    Windows 上 `uvicorn --host ::` 默认 IPV6_V6ONLY=1（只收 IPv6，127.0.0.1 打不开）；
    Linux 默认 V6ONLY=0（真双栈）。为了跨平台行为一致，这里显式
    setsockopt(IPV6_V6ONLY=0) 后再交给 uvicorn（复用 main.app，初始化逻辑不变）。
"""
import argparse
import os
import socket

# 项目根（本文件在根目录）；main.py 用相对路径（uploads/static/…），先切到根
ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)


def create_socket(host: str, port: int) -> socket.socket:
    """按 host 建监听 socket；`::` 走 IPv6 双栈（V6ONLY=0），其余走 IPv4。"""
    is_v6 = host in ("::", "[::]")
    fam = socket.AF_INET6 if is_v6 else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if is_v6:
        try:
            # 关键：同端口也接受 IPv4 连接（Windows 必须显式设；Linux 默认就是 0）
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError as e:
            print("[serve] 警告：设置 IPV6_V6ONLY=0 失败（%s），"
                  "可能只监听 IPv6" % e)
    s.bind((host if not is_v6 else "::", port))
    s.listen(128)
    s.set_inheritable(True)
    return s


def main():
    ap = argparse.ArgumentParser(description="XS File Library 双栈启动器")
    ap.add_argument("--host", default="0.0.0.0",
                    help=":: = IPv4+IPv6 双栈；0.0.0.0 = 仅 IPv4（默认）")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    import asyncio

    import uvicorn
    from main import app

    sock = create_socket(args.host, args.port)
    mode = "双栈 [IPv4+IPv6]" if args.host in ("::", "[::]") else "IPv4"
    print("[serve] 监听 %s 端口 %d（uvicorn + 预建 socket）" % (mode, args.port))
    config = uvicorn.Config(app, host=args.host, port=args.port,
                            log_level="info", lifespan="on")
    # Server.run(sockets=...) 是协程（含 config.load() + serve），必须 await
    server = uvicorn.Server(config)
    asyncio.run(server.run(sockets=[sock]))


if __name__ == "__main__":
    main()
