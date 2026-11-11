"""回归用例固化（第 14 点）：本轮修复的 4 条链路纳入 pytest，防回归。

覆盖：
- users 防锁死（最后一位 user_manage 管理员不可删）+ add-user 入参校验
- media 兜底（非视频/损坏源请求缩略图 → 404 而非 500）
- DASH 幂等指纹口径兼容（离线 make_dash 的 md5 键）+ dash_auto_slice 开关两种行为
- CORS 跨源放行（ACAO=* + OPTIONS 预检允许 Range）

全部跑在 conftest.py 的隔离临时目录里（storage.DASH_DIR 已重定向），
不碰真实部署数据；临时目录用完即弃。
"""
import json
import os

import pytest

from utils import storage, dashtask


# ---------- helpers（与 test_app.py 同口径）----------

def _login(client):
    r = client.post("/api/login", data={"username": "admin", "password": "admin123"})
    return r.json()["token"]


def _auth(token):
    return {"Authorization": "Bearer " + token}


def _set_dash_auto_slice(value):
    """临时切换 dash_auto_slice 设置项（config 落盘，ensure_dash 每次请求读）。"""
    cfg = storage.load_config()
    cfg["dash_auto_slice"] = value
    storage.save_json(storage.SETTING_CONF, cfg)
    return cfg


# ---------- 1) users 防锁死 + 入参校验 ----------

def test_delete_last_admin_blocked(app_server):
    client, _ = app_server
    token = _login(client)

    # 加第二位管理员（user_manage）
    r = client.post("/api/add-user", data={
        "username": "u_admin2", "password": "pw",
        "permissions": json.dumps(["user_manage"]),
    }, headers=_auth(token))
    assert r.json().get("success") is True

    # 此时有两位管理员：删 admin 允许
    r = client.post("/api/delete-user", data={"username": "admin"}, headers=_auth(token))
    assert r.json().get("success") is True, "存在其他管理员时应可删除 admin"
    assert "admin" not in storage.load_users()

    # 现在只剩 u_admin2 一位 user_manage：删除应被拒绝
    r = client.post("/api/delete-user", data={"username": "u_admin2"}, headers=_auth(token))
    assert r.json().get("success") is False, "最后一位管理员不可删除"
    assert "u_admin2" in storage.load_users()

    # 清理：删掉 u_admin2 前需先把 admin 加回来（保证后续用例不受影响）
    client.post("/api/add-user", data={
        "username": "admin", "password": "admin123",
        "permissions": json.dumps(["upload", "download", "delete",
                                   "share", "share_manage", "user_manage"]),
    }, headers=_auth(token))
    client.post("/api/delete-user", data={"username": "u_admin2"}, headers=_auth(token))
    assert "admin" in storage.load_users() and "u_admin2" not in storage.load_users()


def test_add_user_input_validation(app_server):
    client, _ = app_server
    token = _login(client)

    # 空用户名：FastAPI Form 必填 → 422
    r = client.post("/api/add-user", data={"username": "", "password": "x"},
                    headers=_auth(token))
    assert r.status_code == 422

    # 非法用户名（含路径分隔）→ 拒绝，且不写入脏键
    r = client.post("/api/add-user", data={
        "username": "a/b", "password": "x", "permissions": "[]",
    }, headers=_auth(token))
    assert r.json().get("success") is False
    assert "a/b" not in storage.load_users()

    # 非法权限 JSON → 拒绝（原实现会直接 500）
    r = client.post("/api/add-user", data={
        "username": "tmp_bad_perm", "password": "x", "permissions": "not-json",
    }, headers=_auth(token))
    assert r.json().get("success") is False
    assert "tmp_bad_perm" not in storage.load_users()

    # 正常新增 + 删除闭环
    r = client.post("/api/add-user", data={
        "username": "tmp_user", "password": "x", "permissions": "[]",
    }, headers=_auth(token))
    assert r.json().get("success") is True
    assert client.post("/api/delete-user", data={"username": "tmp_user"},
                       headers=_auth(token)).json().get("success") is True


# ---------- 2) media 兜底 404（非视频 / 损坏源）----------

def test_video_thumbnail_non_video_returns_404(app_server):
    client, base = app_server
    token = _login(client)
    up = os.path.join(base, "uploads")
    os.makedirs(up, exist_ok=True)
    txt = os.path.join(up, "notes.txt")
    with open(txt, "wb") as f:
        f.write(b"hello")
    rel = "notes.txt"
    r = client.get("/api/video-thumbnail/" + rel)
    assert r.status_code == 404, "纯文本请求视频缩略图应 404（非 500）"


def test_video_thumbnail_garbage_video_returns_404(app_server):
    client, base = app_server
    up = os.path.join(base, "uploads")
    os.makedirs(up, exist_ok=True)
    bad = os.path.join(up, "bad.mp4")
    with open(bad, "wb") as f:
        f.write(b"\x00\x01garbage-not-mp4")
    r = client.get("/api/video-thumbnail/bad.mp4")
    assert r.status_code == 404, "损坏视频请求首帧应 404（前端占位兜底），非 500"


def test_image_thumbnail_corrupt_returns_404(app_server):
    client, base = app_server
    up = os.path.join(base, "uploads")
    os.makedirs(up, exist_ok=True)
    bad = os.path.join(up, "corrupt.png")
    with open(bad, "wb") as f:
        f.write(b"\x89PNG not a real png")
    r = client.get("/api/thumbnail/corrupt.png")
    assert r.status_code == 404, "损坏图片生成缩略图失败应 404，非 500"


# ---------- 3) DASH 幂等指纹口径 + dash_auto_slice 开关 ----------

def _premake_dash_cache(rel, fake_ffmpeg=True):
    """按离线 make_dash.py 口径预置缓存目录（fingerprint 多一个 md5 键）。"""
    src = os.path.abspath(os.path.join(storage.UPLOAD_DIR, *rel.split("/")))
    st = os.stat(src)
    key = dashtask._rel_key(rel)
    out_dir = os.path.join(dashtask._dash_root(), key)
    os.makedirs(out_dir, exist_ok=True)
    seg = os.path.join(out_dir, "manifest-stream0.mp4")
    with open(seg, "wb") as f:
        f.write(b"FAKESEGMENTDATA")
    with open(os.path.join(out_dir, "manifest.mpd"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0"?><MPD><BaseURL>manifest-stream0.mp4</BaseURL></MPD>')
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({
            "rel": rel,
            "fingerprint": {"mtime": st.st_mtime, "size": st.st_size,
                            "md5": "offline-style-digest"},   # 离线脚本口径：多一个 md5 键
            "single_file": True, "manifest": "manifest.mpd",
            "segments": ["manifest-stream0.mp4"],
            "source": src, "created_by": "make_dash.py",
        }, f, ensure_ascii=False, indent=2)
    return out_dir


def test_dash_switch_default_off_blocks_auto_slice(app_server, monkeypatch):
    """dash_auto_slice 默认 False：即使源在、ffmpeg 可用，ensure_dash 也不切片。"""
    _, base = app_server
    monkeypatch.setattr(dashtask, "get_ffmpeg", lambda: "ffmpeg-fake")  # 证明不调 ffmpeg
    up = os.path.join(base, "uploads")
    os.makedirs(os.path.join(up, "regdash"), exist_ok=True)
    rel = "regdash/clip.mp4"
    with open(os.path.join(up, rel.replace("/", os.sep)), "wb") as f:
        f.write(b"x" * 1024)
    cfg = _set_dash_auto_slice(False)
    try:
        assert dashtask.ensure_dash(rel) == "", "开关关闭时 ensure_dash 应返回空（不自动分片）"
    finally:
        storage.save_json(storage.SETTING_CONF, cfg)


def test_dash_cache_hit_with_offline_md5_fingerprint(app_server, monkeypatch):
    """离线 make_dash 口径的 meta（fingerprint 含 md5 键）仍判缓存命中，不重切。"""
    _, base = app_server
    up = os.path.join(base, "uploads")
    os.makedirs(os.path.join(up, "regdash"), exist_ok=True)
    rel = "regdash/clip2.mp4"
    src_path = os.path.join(up, rel.replace("/", os.sep))
    with open(src_path, "wb") as f:
        f.write(b"y" * 2048)
    out_dir = _premake_dash_cache(rel)

    # get_ffmpeg 指向不存在的可执行：若误判"未命中"触发重切必失败 → 返回 ''
    monkeypatch.setattr(dashtask, "get_ffmpeg", lambda: "ffmpeg-not-exist")
    cfg = _set_dash_auto_slice(True)
    try:
        got = dashtask.ensure_dash(rel)
        assert got == out_dir, "离线口径 meta 应命中缓存（mtime+size 子字段比较），不触发重切"
        # manifest / 分片均可定位
        assert os.path.isfile(dashtask.manifest_path(rel))
        assert dashtask.segment_path(rel, "manifest-stream0.mp4")
    finally:
        storage.save_json(storage.SETTING_CONF, cfg)


def test_dash_manifest_endpoint_respects_switch(app_server, monkeypatch):
    """端点行为：开关开 → manifest 200（BaseURL 已改写）；关 → 404（播放页走渐进式）。"""
    client, base = app_server
    up = os.path.join(base, "uploads")
    os.makedirs(os.path.join(up, "regdash"), exist_ok=True)
    rel = "regdash/clip3.mp4"
    with open(os.path.join(up, rel.replace("/", os.sep)), "wb") as f:
        f.write(b"z" * 512)
    _premake_dash_cache(rel)
    monkeypatch.setattr(dashtask, "get_ffmpeg", lambda: "ffmpeg-fake")

    token = _login(client)
    r = client.post("/api/share", data={
        "filepath": "regdash", "share_type": "folder", "days": 1,
    }, headers=_auth(token))
    sid = r.json()["share_id"]
    url = "/api/video-dash/%s/clip3.mp4" % sid

    cfg = _set_dash_auto_slice(True)
    try:
        on = client.get(url, headers={"Origin": "http://127.0.0.1:8000"})
        assert on.status_code == 200, "开关开启且已预切：manifest 应 200"
        assert on.headers.get("content-type", "").startswith("application/dash+xml")
        assert "/api/video-dash-media/" in on.text, "BaseURL 应被改写为分片端点前缀"
    finally:
        storage.save_json(storage.SETTING_CONF, _set_dash_auto_slice(False))
    # 新语义：缓存命中先于开关判断——预切缓存存在时，开关关闭也应 200（复用离线产物）
    off = client.get(url)
    assert off.status_code == 200, "开关关闭但已有缓存（离线 make_dash 产物）：manifest 应 200 复用缓存"
    # 无缓存 + 开关关闭 → 404（不自动切，前端回退渐进式）
    empty_rel = "regdash/clip_nocache.mp4"
    with open(os.path.join(up, empty_rel.replace("/", os.sep)), "wb") as f:
        f.write(b"w" * 128)
    r2 = client.post("/api/share", data={
        "filepath": "regdash", "share_type": "folder", "days": 1,
    }, headers=_auth(token))
    sid2 = r2.json()["share_id"]
    off2 = client.get("/api/video-dash/%s/%s" % (sid2, empty_rel))
    assert off2.status_code == 404, "开关关闭且无缓存：manifest 应 404（不自动切片）"


# ---------- 4) CORS 跨源放行（资源访问地址改道场景）----------

def test_dash_media_cors_headers(app_server):
    """跨源请求（带 Origin）应返回 Access-Control-Allow-Origin: *，分片端点暴露
    Content-Range；Range 触发的 OPTIONS 预检应 200 且允许 range 头。
    用不存在的 share：CORSMiddleware 对全部响应（含 404/预检）加 CORS 头。"""
    client, _ = app_server
    seg_url = "/api/video-dash-media/nope9999/a.mp4/manifest-stream0.mp4"

    r = client.get(seg_url, headers={"Origin": "http://127.0.0.1:8000"})
    assert r.headers.get("access-control-allow-origin") in ("*", "http://127.0.0.1:8000")
    assert "Content-Range" in (r.headers.get("access-control-expose-headers") or "")

    o = client.options(seg_url, headers={
        "Origin": "http://127.0.0.1:8000",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "range",
    })
    assert o.status_code == 200, "Range 预检应被 CORSMiddleware 应答 200"
    assert o.headers.get("access-control-allow-origin") in ("*", "http://127.0.0.1:8000")
    assert "range" in (o.headers.get("access-control-allow-headers") or "").lower()
    # 注：expose-headers 按规范只加在真实响应上（告知浏览器可读取 Content-Range），
    # 预检响应不携带——已由上面 GET 响应的断言覆盖


def test_samedash_endpoint_cors(app_server):
    """manifest 端点同样放行跨源（dash.js 拉 manifest 也是 XHR）。"""
    client, _ = app_server
    r = client.get("/api/video-dash/nope9999/x.mp4",
                   headers={"Origin": "http://example.com"})
    assert r.headers.get("access-control-allow-origin") in ("*", "http://example.com")


# ---------- 5) 单文件共享（B1 字段 + 强制 normal + 单文件视频流端点）----------

def _make_file_share(client, base, token, rel):
    """造一个单文件型共享，返回 (share_id, 源文件绝对路径)。"""
    up = os.path.join(base, "uploads")
    src = os.path.abspath(os.path.join(up, *rel.split("/")))
    os.makedirs(os.path.dirname(src), exist_ok=True)
    with open(src, "wb") as f:
        f.write(b"x" * 512)
    r = client.post("/api/share", data={
        "filepath": rel, "share_type": "file", "days": 1,
    }, headers=_auth(token))
    assert r.json().get("success") is True
    return r.json()["share_id"], src


def test_share_content_file_branch_fields(app_server):
    """B1：share-content 的 file 分支必须带 path/extension/rel 字段
    （缺 path 曾致前端 share_video.js onload 直接 TypeError 崩溃）。"""
    client, base = app_server
    token = _login(client)
    sid, _ = _make_file_share(client, base, token, "single/clip.mp4")
    r = client.get("/api/share-content/" + sid)
    assert r.json().get("success") is True
    files = r.json()["files"]
    assert len(files) == 1
    f = files[0]
    # 与 folder 分支同口径的三字段必须齐全
    assert f.get("path") == "single/clip.mp4", "file 分支缺 path 字段"
    assert f.get("extension") == "mp4", "file 分支缺 extension 字段"
    assert f.get("rel") == "clip.mp4", "file 分支缺 rel 字段"


def test_file_share_forced_normal_mode(app_server):
    """单文件共享后端强制 normal 模式（相册模式仅对文件夹有意义）。"""
    client, base = app_server
    token = _login(client)
    up = os.path.join(base, "uploads")
    src = os.path.abspath(os.path.join(up, "single", "clip2.mp4"))
    os.makedirs(os.path.dirname(src), exist_ok=True)
    with open(src, "wb") as f:
        f.write(b"x" * 512)
    # 即便前端误传 album，后端 create_share 已把 file 型归一为 normal
    r = client.post("/api/share", data={
        "filepath": "single/clip2.mp4", "share_type": "file",
        "share_mode": "album", "days": 1,
    }, headers=_auth(token))
    sid = r.json()["share_id"]
    body = client.get("/api/share-content/" + sid).json()
    assert body.get("share_mode", "normal") == "normal", "file 型共享应为 normal 模式"


def test_file_share_video_stream(app_server):
    """单文件视频共享：/api/video-stream 的 file 型分支直接以 share['path'] 为源，
    不再把文件名段拼进文件路径（原实现恒 404）。"""
    client, base = app_server
    token = _login(client)
    sid, _ = _make_file_share(client, base, token, "single/clip3.mp4")
    # 播放页按 basename 拼流链接
    r = client.get("/api/video-stream/" + sid + "/clip3.mp4")
    assert r.status_code == 200, "单文件视频流应 200（原实现 file 型恒 404）"
    assert r.headers.get("content-type", "").startswith("video/"), "视频流应带 video/* Content-Type"
