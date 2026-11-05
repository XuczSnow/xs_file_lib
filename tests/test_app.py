"""核心安全 / 功能回归（E1）：登录鉴权、路径穿越、上传限制、二维码、
密码共享、zip 打包、访问看板、密钥随机化。

运行：python -m pytest tests/ -q
（依赖 pytest + httpx；见 requirements.txt）
"""
import io
import json
import os

import pytest


def _login(client, username="admin", password="admin123"):
    r = client.post("/api/login", data={"username": username, "password": password})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"], body
    return body["token"]


def _auth(token):
    return {"Authorization": "Bearer " + token}


# ---------------- 登录 / 鉴权 ----------------

def test_login_ok_and_token(app_server):
    client, _ = app_server
    token = _login(client)
    assert token and "." in token


def test_protected_requires_token(app_server):
    client, _ = app_server
    r = client.get("/api/links")
    assert r.status_code == 401, "无 token 访问管理 API 应 401"


def test_protected_with_token(app_server):
    client, _ = app_server
    token = _login(client)
    assert client.get("/api/links", headers=_auth(token)).status_code == 200


def test_public_endpoints_open(app_server):
    """公开页/接口保持开放（无 token 也应 200）。"""
    client, _ = app_server
    assert client.get("/api/settings").status_code == 200


def test_wrong_password_rejected(app_server):
    client, _ = app_server
    r = client.post("/api/login", data={"username": "admin", "password": "nope"})
    assert r.json()["success"] is False


def test_sha256_legacy_hash_transparent_upgrade(app_server):
    """旧 SHA-256 用户登录成功且哈希被透明升级为 bcrypt。"""
    client, base = app_server
    import utils.storage as storage
    import hashlib

    users = storage.load_users()
    users["legacy"] = {
        "password": hashlib.sha256("pw".encode()).hexdigest(),
        "permissions": ["upload", "download"],
    }
    storage.save_users(users)

    r = client.post("/api/login", data={"username": "legacy", "password": "pw"})
    assert r.json()["success"] is True

    after = storage.load_users()["legacy"]["password"]
    assert after.startswith("$2"), "登录成功后应升级为 bcrypt"


# ---------------- 路径穿越 ----------------

def test_download_path_traversal_blocked(app_server):
    client, _ = app_server
    token = _login(client)
    r = client.get("/api/download/..%2F..%2Fetc%2Fpasswd", headers=_auth(token))
    assert r.status_code in (404, 400), "路径穿越下载应被拒"


def test_delete_root_dir_blocked(app_server):
    client, _ = app_server
    token = _login(client)
    r = client.delete("/api/delete/", headers=_auth(token))
    assert r.status_code == 400, "删除上传根目录应被拒"


# ---------------- 上传限制 ----------------

def _upload(client, token, filename, content, folder=""):
    files = {"file": (filename, io.BytesIO(content), "application/octet-stream")}
    return client.post(
        "/api/upload",
        files=files,
        data={"folder": folder},
        headers=_auth(token),
    )


def test_upload_allowed_type(app_server):
    client, _ = app_server
    token = _login(client)
    r = _upload(client, token, "a.txt", b"hello")
    assert r.status_code == 200 and r.json()["success"] is True


def test_upload_bad_type_rejected(app_server):
    client, _ = app_server
    token = _login(client)
    r = _upload(client, token, "evil.exe", b"")
    assert r.status_code == 400, ".exe 不在白名单应 400"


# ---------------- 二维码 ----------------

def test_qr_returns_png(app_server):
    client, _ = app_server
    token = _login(client)
    # 先建一个共享
    share = client.post(
        "/api/share",
        data={"filepath": "a.txt", "share_type": "file", "days": 1},
        headers=_auth(token),
    ).json()
    assert share["success"], share
    r = client.get("/api/qr/" + share["share_id"])
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content[:4] == b"\x89PNG"


def test_qr_missing_404(app_server):
    client, _ = app_server
    assert client.get("/api/qr/nope1234").status_code == 404


# ---------------- 密码共享 ----------------

def test_password_share_gating(app_server):
    client, base = app_server
    token = _login(client)
    # 先创建实际文件，使 with-access 能通过闸门+文件存在性检查
    up_dir = os.path.join(base, "uploads")
    os.makedirs(up_dir, exist_ok=True)
    with open(os.path.join(up_dir, "pw_share.txt"), "wb") as f:
        f.write(b"secret content")
    # 用独立路径 pw_share.txt，避免命中 create_share 的"同路径复用"
    # （早先测试已为 a.txt 建了无密码共享），确保拿到的是带密码记录
    share = client.post(
        "/api/share",
        data={"filepath": "pw_share.txt", "share_type": "file",
              "days": 1, "password": "s3cret"},
        headers=_auth(token),
    ).json()
    assert share["success"], share
    assert not share.get("duplicate"), "密码共享应为新建，而非复用旧记录"
    sid = share["share_id"]

    # 无 access：内容被拒
    r = client.get("/api/share-content/" + sid)
    assert r.json().get("success") is False

    # share-check 签发 access
    chk = client.post("/api/share-check",
                      data={"share_id": sid, "password": "s3cret"}).json()
    assert chk.get("access_token")

    # 带 access：放行
    ok = client.get("/api/share-content/" + sid +
                    "?access=" + chk["access_token"]).json()
    assert ok.get("success") is True

    # 错误密码：拒绝
    bad = client.post("/api/share-check",
                      data={"share_id": sid, "password": "wrong"}).json()
    assert bad.get("success") is False


# ---------------- zip 打包 ----------------

def test_zip_task(app_server):
    client, base = app_server
    token = _login(client)
    # 造一个目录与文件
    import utils.storage as storage
    d = os.path.join(storage.UPLOAD_DIR, "zdir")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "f.txt"), "wb") as f:
        f.write(b"zipdata")

    task = client.post(
        "/api/share-folder-zip-task/" + _make_share(client, token, "zdir"),
        headers=_auth(token),
    ).json()
    assert task.get("task_id")

    # 轮询到完成
    import time
    for _ in range(30):
        st = client.get(
            "/api/share-folder-zip-status/" + task["task_id"],
            headers=_auth(token),
        ).json()
        if st.get("status") == "completed":
            break
        time.sleep(0.2)
    assert st.get("status") == "completed", st
    dl = client.get(
        "/api/share-folder-zip-download/" + task["task_id"],
        headers=_auth(token),
    )
    assert dl.status_code == 200
    assert dl.content[:2] == b"PK"


def test_zip_change_invalidates_old(app_server):
    """文件夹变更后重新打包：旧 zip 被删除，只保留新包（且无 24h 过期）。"""
    client, base = app_server
    token = _login(client)
    import utils.storage as storage
    import utils.ziptask as ziptask
    import time

    d = os.path.join(storage.UPLOAD_DIR, "cdir")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "x.txt"), "wb") as f:
        f.write(b"v1")
    # 直接调内部构建函数（同步执行，便于断言）
    ziptask._build_zip_task("t1", d)
    st = ziptask.get_task("t1")
    assert st["status"] == "completed", st
    first_zip = st["zip_file"]
    assert os.path.isfile(first_zip)

    # 修改文件夹（内容 + mtime 变化）→ 指纹变化
    time.sleep(0.05)
    with open(os.path.join(d, "x.txt"), "wb") as f:
        f.write(b"v2 modified")
    ziptask._build_zip_task("t2", d)
    st2 = ziptask.get_task("t2")
    assert st2["status"] == "completed", st2

    assert st2["zip_file"] != first_zip, "指纹变化应生成新包"
    assert not os.path.isfile(first_zip), "旧 zip 应被删除"
    assert os.path.isfile(st2["zip_file"]), "新 zip 应存在"


def _make_share(client, token, folder):
    r = client.post(
        "/api/share",
        data={"filepath": folder, "share_type": "folder", "days": 1},
        headers=_auth(token),
    ).json()
    assert r["success"], r
    return r["share_id"]


# ---------------- 访问看板 ----------------

def test_share_stats_app(app_server):
    client, _ = app_server
    token = _login(client)
    share = _make_share(client, token, "a.txt")
    # 触发一次访问日志
    client.get("/api/share-info/" + share)
    r = client.get("/api/share-stats", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["total_pv"] >= 1
    assert share in body["per_link"]


# ---------------- 登录限流（放最后，避免污染会话计数）----------------

def test_login_rate_limit(app_server):
    client, _ = app_server
    import utils.security as security
    # 先成功一次，清空该 IP 计数
    security.login_succeeded("testclient")
    for _ in range(5):
        client.post("/api/login", data={"username": "admin", "password": "bad"})
    r = client.post("/api/login", data={"username": "admin", "password": "admin123"})
    assert r.json()["success"] is False, "连续失败 5 次后应进入冷却"
    # 清理，避免影响其他（本测试在最后）
    security.login_succeeded("testclient")
