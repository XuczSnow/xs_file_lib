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
import shutil
import subprocess
import time

import pytest

from utils import storage, dashtask, updater


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


def test_dash_mpd_hevc_codecs_completed(app_server):
    """HEVC DASH MPD 的裸 codecs=\"hvc1\" 在 serve 时补全为带 profile/level 的完整串。

    根因：ffmpeg 的 DASH muxer 对 HEVC 只写裸 hvc1/hev1 标签，而浏览器
    MediaSource.isTypeSupported('video/mp4; codecs="hvc1"') 返回 false（只对带
    profile/level 的具体串认账）→ dash.js 按 MPD codecs 做能力 gate 时视频轨被丢、
    只剩音频（"有声无画"）。修法：_rewrite_mpd_basurl 顺带把裸串补全。
    覆盖：meta 有精确串 / 存量缓存（meta 无串）从分辨率推 level / 非 HEVC 完整串不动。"""
    import re as _re
    from routers.share_media import _complete_mpd_video_codecs

    client, base = app_server
    token = _login(client)
    up = os.path.join(base, "uploads")

    # --- 单元：_complete_mpd_video_codecs ---
    hevc_mpd = ('<MPD><Representation mimeType="video/mp4" codecs="hvc1" '
                'width="2416" height="1440"/></MPD>')
    assert 'codecs="hvc1.1.6.L93.B0"' in _complete_mpd_video_codecs(hevc_mpd, "hvc1.1.6.L93.B0")
    assert 'codecs="hvc1.1.6.L93.B0"' in _complete_mpd_video_codecs(hevc_mpd, ""), \
        "存量缓存（meta 无串）应从 1440p 推 L93"
    out8k = _complete_mpd_video_codecs(
        '<MPD><Representation codecs="hvc1" width="7680" height="4320"/></MPD>', "")
    assert 'codecs="hvc1.1.6.L183.B0"' in out8k, "8K 应推 L183"
    av1_mpd = '<MPD><Representation mimeType="video/mp4" codecs="av01.0.12M.08"/></MPD>'
    assert _complete_mpd_video_codecs(av1_mpd, "") == av1_mpd, "非 HEVC 完整串应保持原样"

    # --- 端到端：manifest 端点对存量 HEVC 缓存（meta 无 video_codecs）输出补全后 MPD ---
    os.makedirs(os.path.join(up, "hevcshare"), exist_ok=True)
    rel = "hevcshare/clip.mp4"
    srcp = os.path.abspath(os.path.join(up, rel.replace("/", os.sep)))
    with open(srcp, "wb") as f:
        f.write(b"v" * 200)
    key = dashtask._rel_key(rel)
    out_dir = os.path.join(dashtask._dash_root(), key)
    os.makedirs(out_dir, exist_ok=True)
    st = os.stat(srcp)
    with open(os.path.join(out_dir, "manifest-stream0.mp4"), "wb") as f:
        f.write(b"SEG")
    with open(os.path.join(out_dir, "manifest.mpd"), "w", encoding="utf-8") as f:
        f.write('<MPD><BaseURL>manifest-stream0.mp4</BaseURL>'
                '<Representation mimeType="video/mp4" codecs="hvc1" '
                'width="2416" height="1440"/></MPD>')
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"rel": rel, "fingerprint": {"mtime": st.st_mtime, "size": st.st_size},
                   "single_file": True, "manifest": "manifest.mpd",
                   "segments": ["manifest-stream0.mp4"], "source": srcp,
                   "created_by": "make_dash.py"}, f, ensure_ascii=False, indent=2)
    sid = client.post("/api/share", data={
        "filepath": "hevcshare", "share_type": "folder", "days": 1,
    }, headers=_auth(token)).json()["share_id"]
    got = client.get("/api/video-dash/%s/%s" % (sid, "clip.mp4"))
    assert got.status_code == 200, "HEVC 缓存的 manifest 端点应 200"
    assert not _re.search(r'codecs="hvc1"', got.text), "不应残留裸 hvc1"
    assert _re.search(r'codecs="hvc1\.\d\.\d\.L\d+\.B0"', got.text), \
        "manifest 端点应把裸 hvc1 补全为带 profile/level 的完整串"


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


# ---------- 6) 网页转码产物端点（cache/video：列格式 + 取转码文件流）----------

def _tc_root_abs(base):
    """conftest 重定向后的 cache/video 绝对路径（与运行时 storage.TC_VIDEO_DIR 一致）。"""
    return storage.TC_VIDEO_DIR


def _put_transcode(base, rel_video, suffix, content=b"fake"):
    """在 cache/video 下按 uploads 相对结构放一个转码产物，返回其绝对路径。

    rel_video 形如 'tcvid/clip.mp4'；产物 = 同目录 + stem + suffix
    （suffix 含扩展名，如 '_h264.mp4'），与后端 _tc_candidate_paths 口径一致。"""
    tc = storage.TC_VIDEO_DIR
    absp = os.path.abspath(os.path.join(tc, rel_video.replace("/", os.sep)))
    target = os.path.join(
        os.path.dirname(absp),
        os.path.splitext(os.path.basename(absp))[0] + suffix)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as f:
        f.write(content)
    return target


def _folder_share(client, base, token, dir_rel, files):
    """造文件夹共享：在 uploads/dir_rel/ 下写 files（{rel: bytes}），返回 share_id。"""
    up = os.path.join(base, "uploads")
    for rel, data in files.items():
        p = os.path.abspath(os.path.join(up, *dir_rel.split("/"), rel))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    r = client.post("/api/share", data={
        "filepath": dir_rel, "share_type": "folder", "days": 1,
    }, headers=_auth(token))
    assert r.json().get("success") is True
    return r.json()["share_id"]


def test_video_transcodes_lists_available(app_server):
    """/api/video-transcodes：无转码产物 → available=[]；放了 _h264 产物 → 返回该格式。
    filepath 为 UPLOAD 相对全路径（前端 ep.path，如 tcvid/clip.mp4），须落在共享目录下。"""
    client, base = app_server
    token = _login(client)
    sid = _folder_share(client, base, token, "tcvid", {"clip.mp4": b"a" * 300})
    # 无产物
    r = client.get("/api/video-transcodes/" + sid + "/tcvid/clip.mp4")
    assert r.json().get("success") is True
    assert r.json()["available"] == [], "无转码产物时应返回空 available"
    # 放一个 h264 产物（cache/video/tcvid/clip_h264.mp4）
    _put_transcode(base, "tcvid/clip.mp4", "_h264.mp4", b"h264bytes")
    r2 = client.get("/api/video-transcodes/" + sid + "/tcvid/clip.mp4")
    av = r2.json()["available"]
    assert any(x["fmt"] == "h264" for x in av), "存在 clip_h264.mp4 时应列出 h264"
    h264 = [x for x in av if x["fmt"] == "h264"][0]
    assert h264["path"].replace("\\", "/").endswith("clip_h264.mp4"), "path 应为相对 cache/video 的产物路径"


def test_video_stream_fmt_range_and_download(app_server):
    """/api/video-stream-fmt：Range 206 + ETag；?dl=1 带 Content-Disposition 附件；
    未知 fmt 或无产物 → 404。"""
    client, base = app_server
    token = _login(client)
    sid = _folder_share(client, base, token, "tcvid2", {"m.mp4": b"m" * 300})
    _put_transcode(base, "tcvid2/m.mp4", "_h264.mp4", b"transcoded-bytes-0123")
    base_url = "/api/video-stream-fmt/" + sid + "/tcvid2/m.mp4/h264"
    # Range 206
    r = client.get(base_url, headers={"Range": "bytes=0-4"})
    assert r.status_code == 206, "Range 请求应 206"
    assert r.content == b"trans"
    assert "Content-Range" in r.headers
    assert "ETag" in r.headers
    # 整文件 200
    r0 = client.get(base_url)
    assert r0.status_code == 200
    assert r0.content == b"transcoded-bytes-0123"
    assert "Content-Disposition" not in r0.headers, "非下载不应带 attachment"
    # ?dl=1 附件
    rd = client.get(base_url + "?dl=1")
    assert rd.status_code == 200
    cd = rd.headers.get("content-disposition", "")
    assert "attachment" in cd, "?dl=1 应带 Content-Disposition: attachment"
    # 未知 fmt / 无产物 → 404（filepath 正确，分别测 fmt 枚举校验与产物不存在）
    rbad = client.get("/api/video-stream-fmt/" + sid + "/tcvid2/m.mp4/nope")
    assert rbad.status_code == 404
    rmissing = client.get("/api/video-stream-fmt/" + sid + "/tcvid2/m.mp4/av1")
    assert rmissing.status_code == 404, "未转码的 av1 应 404"


def test_video_transcodes_traversal_blocked(app_server):
    """路径穿越：filepath 带 ../ 逃出共享目录 → 404（不读非共享视频）。"""
    client, base = app_server
    token = _login(client)
    sid = _folder_share(client, base, token, "tcvid3", {"ok.mp4": b"o" * 10})
    r = client.get("/api/video-transcodes/" + sid + "/../../outside.mp4")
    assert r.status_code == 404, "穿越共享目录应 404"
    r2 = client.get("/api/video-stream-fmt/" + sid + "/../escape.mp4/h264")
    assert r2.status_code == 404, "取流端点穿越也应 404"


def test_video_transcodes_password_share_requires_access(app_server):
    """密码共享：无 access 令牌 → 403；带有效 access → 200。"""
    client, base = app_server
    token = _login(client)
    up = os.path.join(base, "uploads")
    os.makedirs(os.path.join(up, "pwdshare"), exist_ok=True)
    with open(os.path.join(up, "pwdshare", "p.mp4"), "wb") as f:
        f.write(b"p" * 50)
    r = client.post("/api/share", data={
        "filepath": "pwdshare", "share_type": "folder", "days": 1, "password": "pw123",
    }, headers=_auth(token))
    sid = r.json()["share_id"]
    _put_transcode(base, "pwdshare/p.mp4", "_hevc.mp4", b"hevc-bytes")
    # 无令牌 → 403
    r1 = client.get("/api/video-transcodes/" + sid + "/pwdshare/p.mp4")
    assert r1.status_code == 403, "密码共享无 access 应 403"
    # 用 share-check 换 access 令牌
    chk = client.post("/api/share-check", data={"share_id": sid, "password": "pw123"})
    assert chk.json().get("success") is True
    access = chk.json()["access_token"]
    r2 = client.get("/api/video-transcodes/" + sid + "/pwdshare/p.mp4",
                    params={"access": access})
    assert r2.status_code == 200
    assert any(x["fmt"] == "hevc" for x in r2.json()["available"])


def test_video_stream_fmt_file_share(app_server):
    """单文件型共享：filepath 必须正好等于 share['path']，命中后取流 200。"""
    client, base = app_server
    token = _login(client)
    # 单文件共享 single/clip.mp4
    sid, _ = _make_file_share(client, base, token, "single/clip.mp4")
    _put_transcode(base, "single/clip.mp4", "_h264.mp4", b"fileshare-tc")
    r = client.get("/api/video-transcodes/" + sid + "/single/clip.mp4")
    assert r.status_code == 200, "file 型 filepath=share.path 应 200"
    assert any(x["fmt"] == "h264" for x in r.json()["available"])
    rs = client.get("/api/video-stream-fmt/" + sid + "/single/clip.mp4/h264")
    assert rs.status_code == 200 and rs.content == b"fileshare-tc"
    # 换成别的路径 → 404
    rbad = client.get("/api/video-transcodes/" + sid + "/other/x.mp4")
    assert rbad.status_code == 404, "file 型只允许 share.path 本身"


# ---------- 7) 在线更新（updater：独立临时 git 仓库 + 本地 bare remote）----------
# 全部在 tmp_path 内造仓库，重定向 updater._git_dir/_log_path，并打桩 _smoke_check /
# _schedule_restart（不真跑 py_compile/import main、不真重启服务），绝不碰真实仓库。

def _git_repo_env(tmp_path, monkeypatch):
    """造一个本地工作仓库（落后 origin/main 一个提交）。

    序列（关键：先让 work push c1，再 clone，避免历史分叉）：
      1. 建 bare origin + work，work 上 main 分支提交 c1 → push（origin/main = c1）
      2. clone origin 到 pusher（此时含 main=c1）→ 在 main 上提交 c2 → push
      3. origin/main = c2，本地 work 停在 c1 → 落后 1
    - 重定向 storage.UPDATER_GIT_DIR / UPDATER_LOG_CONF 到 tmp_path；
    - 打桩 _smoke_check（恒通过）、_schedule_restart（不重启，返回 False）。
    返回 work 仓库 Path。git init/--bare <path> 会自建目录；clone 后全程用 -C。"""
    work, origin, pusher = tmp_path / "work", tmp_path / "origin.git", tmp_path / "pusher"

    def run(*cmd, cwd=None):
        subprocess.run(list(cmd), cwd=str(cwd) if cwd else None,
                       check=True, capture_output=True, text=True)

    def g(repo, *args):
        run("git", "-C", str(repo), *args)

    # 1) origin + work：main 分支提交 c1 并 push
    run("git", "init", "--bare", str(origin))
    run("git", "init", str(work))
    g(work, "config", "user.email", "t@t")
    g(work, "config", "user.name", "t")
    g(work, "checkout", "-b", "main")
    (work / "a.txt").write_text("v1")
    g(work, "add", ".")
    g(work, "commit", "-m", "c1")
    g(work, "remote", "add", "origin", str(origin))
    g(work, "push", "-q", "origin", "main")           # origin/main = c1

    # 2) clone（含 main=c1）→ main 上提交 c2 → push（origin/main = c2）
    run("git", "clone", "-q", str(origin), str(pusher))
    g(pusher, "config", "user.email", "t@t")
    g(pusher, "config", "user.name", "t")
    g(pusher, "checkout", "-q", "main")                # 对齐 origin/main（c1）
    (pusher / "b.txt").write_text("v2")
    g(pusher, "add", ".")
    g(pusher, "commit", "-m", "c2")
    g(pusher, "push", "-q", "origin", "main")          # origin/main = c2，work 落后 1

    # 重定向 + 打桩（不碰真实仓库、不真跑自检/重启）
    # raising=False：updater 用 getattr(storage, "UPDATER_GIT_DIR", None) 读取，
    # 属性可不存在，允许 setattr 新建
    monkeypatch.setattr(storage, "UPDATER_GIT_DIR", str(work), raising=False)
    monkeypatch.setattr(storage, "UPDATER_LOG_CONF", str(tmp_path / "update_log.json"), raising=False)
    monkeypatch.setattr(updater, "_smoke_check", lambda cwd: "")
    monkeypatch.setattr(updater, "_schedule_restart", lambda: False)
    return work


def _head(work):
    return subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def test_updater_check_update_structure(tmp_path, monkeypatch):
    """check_update 只读比较：本地落后 1 → behind=1、列 commit、current!=latest、工作区干净。"""
    work = _git_repo_env(tmp_path, monkeypatch)
    chk = updater.check_update()
    assert chk["ok"] is True
    assert chk["behind"] == 1, "本地应落后 1 个提交"
    assert chk["commits"], "应列出落后的 commit"
    assert len(chk["current"]) == 8 and len(chk["latest"]) == 8
    assert chk["current"] != chk["latest"]
    assert chk["dirty"] == [], "干净工作区"


def test_updater_apply_updates_head_and_logs(tmp_path, monkeypatch):
    """apply_update：HEAD 前进 + update_log 落一条 ok；二次 apply 命中 noop（已是最新）。"""
    work = _git_repo_env(tmp_path, monkeypatch)
    old = _head(work)
    res = updater.apply_update(actor="tester")
    assert res["ok"] is True and res.get("noop") is not True
    assert res["from"] == old[:8] and res["to"] != res["from"]
    assert _head(work) != old, "HEAD 应前进到 origin/main"
    assert any(e.get("result") == "ok" for e in updater._load_log()), "应记录一条 ok"
    again = updater.apply_update(actor="tester")
    assert again.get("noop") is True, "更新后已是最新，二次 apply 应 noop"


def test_updater_apply_rejects_dirty_worktree(tmp_path, monkeypatch):
    """脏工作区（有未跟踪文件）→ apply 被拒绝，HEAD 不变（防误伤本地改动）。"""
    work = _git_repo_env(tmp_path, monkeypatch)
    old = _head(work)
    (work / "untracked.txt").write_text("dirty")
    res = updater.apply_update(actor="tester")
    assert res["ok"] is False
    assert "未提交" in res["error"] or "untracked.txt" in res["error"]
    assert _head(work) == old, "被拒绝时 HEAD 不应变"


def test_updater_rollback_resets_to_old_head(tmp_path, monkeypatch):
    """rollback：apply 后一键回到 apply 前的旧 HEAD（24h 窗口内）。"""
    work = _git_repo_env(tmp_path, monkeypatch)
    old = _head(work)
    assert updater.apply_update(actor="tester")["ok"] is True
    rb = updater.rollback(actor="tester")
    assert rb["ok"] is True
    assert _head(work) == old, "rollback 应复位到 apply 前的旧 HEAD"
    assert any(e.get("result") == "rollback" for e in updater._load_log())


def test_restart_service_requires_command(app_server, monkeypatch):
    """/api/restart-service：未配置 restart_command → need_cmd（不猜命令）；
    配置后 → ok=True + detached 调度成功（打桩，不真重启）+ update_log 留痕；
    system-info 把 manual-restart 折叠成 restart_count，不混入 update_log。"""
    client, _ = app_server
    token = _login(client)
    called = []
    monkeypatch.setattr(updater, "_schedule_restart", lambda: called.append(1) or True)
    cfg = storage.load_config()
    cfg["restart_command"] = ""
    storage.save_json(storage.SETTING_CONF, cfg)
    r = client.post("/api/restart-service", headers=_auth(token))
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["need_cmd"] is True
    assert not called, "未配置命令时不应调度重启"
    cfg["restart_command"] = "echo marker-restart"
    storage.save_json(storage.SETTING_CONF, cfg)
    r2 = client.post("/api/restart-service", headers=_auth(token))
    assert r2.status_code == 200 and r2.json()["ok"] is True
    assert called, "配置命令后应调度重启"
    assert any(e.get("result") == "manual-restart" for e in updater._load_log())
    # system-info：手动重启折叠为 restart_count，update_log 只含版本类记录
    info = client.get("/api/system-info", headers=_auth(token)).json()
    assert info["restart_count"] == 1
    assert not any(str(e.get("result", "")).startswith("manual-restart")
                   for e in info["update_log"]), "manual-restart 不应混入 update_log"
    assert info["restart_last"] > 0
    # 恢复默认
    cfg["restart_command"] = ""
    storage.save_json(storage.SETTING_CONF, cfg)


def test_restart_service_admin_only(app_server):
    """/api/restart-service：无 token → 401；普通用户（无 user_manage）→ 403。"""
    client, _ = app_server
    admin_token = _login(client)
    # 无 token 直接访问受保护端点
    r = client.post("/api/restart-service")
    assert r.status_code == 401, "无 token 应 401"
    # 造一个只有 upload 权限的普通用户
    client.post("/api/add-user", data={
        "username": "u_plain", "password": "pw123",
        "permissions": json.dumps(["upload"]),
    }, headers=_auth(admin_token))
    plain_token = client.post("/api/login", data={
        "username": "u_plain", "password": "pw123"}).json()["token"]
    r2 = client.post("/api/restart-service", headers=_auth(plain_token))
    assert r2.status_code == 403, "无 user_manage 权限应 403"
    # 管理员带有效 token 应通过（200；restart_command 未配置时返回 need_cmd 而非 403）
    r3 = client.post("/api/restart-service", headers=_auth(admin_token))
    assert r3.status_code == 200, "管理员应通过鉴权闸门"


# ---------- 健康检查（U3 A）：公开探活端点 ----------

def test_health_endpoint_public(app_server):
    """/api/health：公开端点（无需 token），返回 ok + version + ts。

    固定三件事：
    - 匿名访问 200（不进 require_token 保护列表）；
    - 字段齐全（ok=True / version 非空 / ts 为 int）；
    - 不误入保护列表（带不带 token 行为一致）。"""
    client, _ = app_server
    # 匿名（不带任何 token）即可访问
    r = client.get("/api/health")
    assert r.status_code == 200, "健康检查应公开可达"
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["ts"], int)
    # version = release_note/ 下最新号；测试隔离临时目录无 release_note 时为 ""（允许），
    # 但字段必须存在
    assert "version" in body

    # 带 token 也 200（行为一致，不因鉴权差异）
    admin_token = _login(client)
    r2 = client.get("/api/health", headers=_auth(admin_token))
    assert r2.status_code == 200
