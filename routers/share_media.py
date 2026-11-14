"""共享媒体访问：共享内容（树/扁平）、单文件与文件夹下载、密码校验、视频流。

从原 routers/shares.py 拆出（A2），路由路径与前端调用保持不变；
共享 helper（find_share / share_unlocked / video_content_type）位于
utils/share.py，避免跨路由循环依赖。
"""
import hashlib
import os
import re
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Form, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from utils import storage, filetree, security, dashtask
from utils import share as share_util

router = APIRouter(tags=["share-media"])


@router.get("/api/share-content/{share_id}")
def share_content(share_id: str, password: str = "", access: str = ""):
    share = share_util.find_share(share_id)
    if share is None:
        return {"success": False}

    # 密码闸门：设了密码的共享需携带 access 令牌（/api/share-check 校验后签发）或正确密码
    if not share_util.share_unlocked(share, share_id, password, access):
        return {"success": False, "message": "密码错误"}

    # 过期校验（与 /api/share-download 一致）
    expire_time = datetime.strptime(share["expire"], "%Y-%m-%d %H:%M:%S")
    if datetime.now() > expire_time:
        return {"success": False, "message": "共享已过期"}

    # 存储的共享路径本身也做一次 safe_join，防止历史数据里的脏路径越界
    base = os.path.abspath(storage.UPLOAD_DIR)
    shared_dir = security.safe_join(base, share["path"]) if share.get("path") else base

    if share["type"] == "file":
        # 文件类型：拼 UPLOAD_DIR 前缀取 size；路径守卫 + 存在性校验
        full_path = shared_dir if os.path.isfile(shared_dir) else None
        if full_path is None:
            return {"success": False, "message": "文件不存在"}
        return {
            "success": True,
            "type": "file",
            "share_id": share_id,
            "path": share["path"],
            "name": os.path.basename(share["path"]),
            # 与文件夹分支同口径（path/extension/rel），前端播放页/共享页统一按 f.path 消费；
            # 缺 path 曾致 share_video.js onload 直接 TypeError 崩溃
            "files": [{
                "name": os.path.basename(share["path"]),
                "path": share["path"],
                "extension": os.path.splitext(share["path"])[1].lstrip(".").lower(),
                "rel": os.path.basename(share["path"]),
                "size": os.path.getsize(full_path),
            }],
        }

    if shared_dir is None or not os.path.isdir(shared_dir):
        return {"success": False, "message": "目录不存在"}

    # 树状结构：共享子目录为遍历起点，文件 path 仍相对 UPLOAD_DIR
    tree = filetree.build_file_tree(storage.UPLOAD_DIR, start_dir=shared_dir)
    # 全层级扁平文件列表（相册模式收集所有图片/视频用）
    all_files = filetree.flatten_files(tree)

    return {
        "success": True,
        "type": "folder",
        "share_mode": share.get("share_mode", "normal"),
        "share_id": share_id,
        "path": share["path"],
        "name": share["path"],
        "tree": tree,
        "files": all_files,
    }


@router.get("/api/share-folder-download/{share_id}/{filename:path}")
def share_folder_download(share_id: str, filename: str, password: str = "", access: str = ""):
    """下载共享文件夹内的单个文件。

    安全加固：
    - 过期共享直接拒绝（与 /api/share-download 保持一致）；
    - 密码闸门：设了密码需携带 access 令牌或正确密码；
    - 用 safe_join 校验 filename 不逃出共享目录（阻止 `../` 路径穿越）。
    """
    share = share_util.find_share(share_id)
    if share is None:
        return {"success": False, "message": "共享不存在"}

    if not share_util.share_unlocked(share, share_id, password, access):
        return {"success": False, "message": "密码错误"}

    # 过期校验：与原 share_download 一致
    expire_time = datetime.strptime(share["expire"], "%Y-%m-%d %H:%M:%S")
    if datetime.now() > expire_time:
        return {"success": False, "message": "共享已过期"}

    share_dir = security.safe_join(storage.UPLOAD_DIR, share["path"])
    file_path = security.safe_join(share_dir, filename)
    if file_path is None or not os.path.isfile(file_path):
        return {"success": False, "message": "文件不存在"}
    return FileResponse(file_path)


@router.post("/api/share-check")
def share_check(share_id: str = Form(...), password: str = Form("")):
    """校验共享密码。成功后签发短期访问令牌，后续取内容/下载/播放都带它。

    兼容 bcrypt 与旧 SHA-256 存量哈希（security.verify_password）。
    """
    share = share_util.find_share(share_id)
    if share is None:
        return {"success": False, "message": "共享不存在"}

    if share.get("password", "") == "":
        return {"success": True, "access_token": security.issue_share_access(share_id)}

    if not security.verify_password(share["password"], password):
        return {"success": False, "message": "密码错误"}

    return {"success": True, "access_token": security.issue_share_access(share_id)}


@router.get("/api/share-download/{share_id}")
def share_download(share_id: str, password: str = "", access: str = ""):
    """下载单文件共享。设置了密码的需携带 access 令牌或正确密码。"""
    share = share_util.find_share(share_id)
    if share is None:
        return {"success": False, "message": "共享不存在"}

    if not share_util.share_unlocked(share, share_id, password, access):
        return {"success": False, "message": "密码错误"}

    expire_time = datetime.strptime(
        share["expire"], "%Y-%m-%d %H:%M:%S"
    )
    if datetime.now() > expire_time:
        return {"success": False, "message": "共享已过期"}

    # 路径守卫 + 存在性校验
    file_path = security.safe_join(storage.UPLOAD_DIR, share["path"])
    if file_path is None or not os.path.isfile(file_path):
        return {"success": False, "message": "文件不存在"}

    return FileResponse(
        file_path,
        filename=os.path.basename(share["path"]),
    )


# ---------------------------------------------------------------------------
# 网页转码产物（transcode.py 默认输出到 cache/video，按 uploads 相对结构存放）。
# 共享目录树只见原始视频；播放页切选集时用 /api/video-transcodes 查该视频
# 有哪些转码格式，选中后用 /api/video-stream-fmt 取对应文件流。
# 格式表须与 tools/transcode.py 的 FORMATS 及前端 share_video.js FMT_LABELS 同口径。
# ---------------------------------------------------------------------------
TC_FORMATS = [
    # (fmt key, 文件名后缀(含扩展名), 前端标签)
    ("h264", "_h264.mp4", "H.264"),
    ("hevc", "_hevc.mp4", "HEVC"),
    ("av1", "_av1.webm", "AV1"),
    ("vp9", "_vp9.webm", "VP9"),
]


def _tc_root() -> str:
    """转码产物根目录（cache/video），按调用动态读取，便于测试重定向。"""
    return os.path.abspath(storage.TC_VIDEO_DIR)


def _tc_candidate_paths(rel_video: str) -> list:
    """给定原始视频的 UPLOAD_DIR 相对路径，定位其各转码产物在 cache/video 下的位置。

    cache/video 按 uploads 相对结构存放（transcode.py 默认输出），故同路径 + 格式后缀。
    返回 [(fmt_key, label, cache_rel, abs_path)]；cache_rel 为相对 cache/video 的路径
    （= 端点 B 定位 / 返回给前端用）；abs_path 可能不存在（调用方 isfile 过滤）。
    """
    base = _tc_root()
    rel_video = (rel_video or "").replace("\\", "/").lstrip("/")
    stem_dir = os.path.dirname(rel_video)                        # 1111/1234（或空）
    stem = os.path.splitext(os.path.basename(rel_video))[0]      # clip
    out = []
    for key, suffix, label in TC_FORMATS:
        fn = stem + suffix                                        # clip_h264.mp4
        cache_rel = (stem_dir + "/" + fn) if stem_dir else fn
        out.append((key, label, cache_rel, os.path.join(base, cache_rel)))
    return out


def _serve_media_range(request: Request, file_path: str, media_type: str,
                       etag: str, cache_ctl: str, filename: str = None):
    """通用 Range/206/分块读/ETag/缓存媒体流响应（video-stream 与 video-stream-fmt 共用）。

    - 同步 def + 4MB 分块：弱服务器 / 慢客户端不阻塞事件循环；
    - ETag：If-None-Match 命中直接 304 省带宽；
    - Range：206 局部响应，非法/越界 416；无 Range 则 FileResponse 整文件 200；
    - 可选 filename：?dl=1 下载时加 Content-Disposition: attachment（中文用 RFC5987 编码）。
    """
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": cache_ctl,
        "ETag": etag,
    }
    if filename:
        import urllib.parse
        headers["Content-Disposition"] = (
            "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))

    file_size = os.path.getsize(file_path)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    range_header = request.headers.get("range")
    if range_header:
        try:
            start_s, end_s = range_header.replace("bytes=", "").split("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else file_size - 1
        except ValueError:
            return JSONResponse({"success": False}, status_code=416)
        end = min(end, file_size - 1)
        if start > end or start >= file_size:
            return Response(status_code=416, headers={
                **headers, "Content-Range": f"bytes */{file_size}"})
        length = end - start + 1

        def iter_range():
            # 4MB 分块：弱服务器下减少系统调用
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(4 * 1024 * 1024, remaining))
                    if not chunk:
                        break
                    yield chunk
                    remaining -= len(chunk)

        return StreamingResponse(
            iter_range(), status_code=206, media_type=media_type,
            headers={**headers,
                     "Content-Range": f"bytes {start}-{end}/{file_size}",
                     "Content-Length": str(length)})

    return FileResponse(file_path, media_type=media_type, headers=headers)


@router.get("/api/video-stream/{share_id}/{filename:path}")
def share_video_stream(share_id: str, filename: str, request: Request,
                       password: str = "", access: str = ""):
    """支持 Range 的视频流。

    P0 优化（弱服务器 / Cloudflare 场景）：
    - 同步 def：FastAPI 放线程池执行，慢客户端不再阻塞事件循环；
    - 4MB 分块（原 1MB）：减少系统调用次数；
    - Content-Type 按扩展名推断（原写死 video/mp4，webm/mov 会解码异常）；
    - ETag + Cache-Control：公开流 s-maxage=3600（让 CF 边缘缓存整对象、
      拖动/回看由边缘合成 206）；带 access 密码令牌的流 no-store
      （防共享过期后缓存仍可访问，防令牌进入缓存键）。

    注意：{filename:path} 到达处理器前已被 ASGI 解码，不要再 unquote。
    密码闸门：设了密码的共享需 URL 携带 access 令牌（?access=xxx）。
    """
    share = share_util.find_share(share_id)
    if share is None:
        return JSONResponse({"success": False}, status_code=404)

    # 密码闸门（视频流是直链，无法弹框收密码，故用 share-check 签发的 access 令牌）
    if not share_util.share_unlocked(share, share_id, password, access):
        return JSONResponse({"success": False, "message": "密码错误"}, status_code=403)

    # 过期校验 + 路径穿越守卫（防止 `../` 逃出共享目录）
    expire_time = datetime.strptime(share["expire"], "%Y-%m-%d %H:%M:%S")
    if datetime.now() > expire_time:
        return JSONResponse({"success": False, "message": "共享已过期"}, status_code=410)

    video_path = _resolve_share_video_path(share, filename)
    if video_path is None:
        return JSONResponse({"success": False}, status_code=404)

    file_size = os.path.getsize(video_path)
    mtime = int(os.path.getmtime(video_path))
    media_type = share_util.video_content_type(filename)

    # 缓存头：密码共享（带 access 令牌）不缓存；公开共享边缘缓存 1 小时
    cache_ctl = "no-store" if (access or password) else "public, s-maxage=3600, max-age=3600"
    etag = f'"{share_id}-{file_size}-{mtime}"'
    return _serve_media_range(request, video_path, media_type, etag, cache_ctl)


# ---------------------------------------------------------------------------
# 网页转码产物（cache/video）：列可用格式 + 取转码文件流。
# 共享目录树只显示原始视频（uploads）；播放页切选集时用 video-transcodes
# 查"该视频在 cache/video 下有哪些转码格式"，选中后用 video-stream-fmt 取流。
# 两个端点的鉴权/过期/穿越守卫与视频流端点同口径（share_unlocked + safe_join）。
# ---------------------------------------------------------------------------

def _tc_guard(share_id: str, rel_video: str, password: str, access: str):
    """转码端点前置校验：共享存在 / 解锁 / 未过期。

    返回 (rel_video, error_resp)；rel_video 为【已归一化】的 UPLOAD_DIR 相对路径
    （调用方据此定位 cache/video 下的产物）；失败时 error_resp 非空。
    rel_video 是原始视频的上传相对路径（如 1111/1234/clip.mp4），这里校验它
    确实落在共享可见范围内（防借端点读非共享视频/穿越），并归一化分隔符。
    """
    share = share_util.find_share(share_id)
    if share is None:
        return None, JSONResponse({"success": False}, status_code=404)
    if not share_util.share_unlocked(share, share_id, password, access):
        return None, JSONResponse({"success": False, "message": "密码错误"}, status_code=403)
    expire_time = datetime.strptime(share["expire"], "%Y-%m-%d %H:%M:%S")
    if datetime.now() > expire_time:
        return None, JSONResponse({"success": False, "message": "共享已过期"}, status_code=410)

    # share_content 的 f.path（即本端点的 filepath）对 file / folder 都是
    # UPLOAD_DIR 相对全路径（如 1111/1234/clip.mp4），与 cache/video 的目录结构一致。
    # 归一化分隔符后，校验该路径确实落在【共享可见范围】内（防借端点读非共享视频）：
    #   - file 型：必须正好等于 share["path"]（那个唯一文件）
    #   - folder 型：必须位于 share["path"]（共享目录）之下
    rel = (rel_video or "").replace("\\", "/").lstrip("/")
    share_path = (share.get("path") or "").replace("\\", "/").lstrip("/")
    if not rel:
        return None, JSONResponse({"success": False}, status_code=404)
    if share.get("type") == "file":
        if rel != share_path:
            return None, JSONResponse({"success": False}, status_code=404)
    else:
        if not share_path or not (rel == share_path or rel.startswith(share_path + "/")):
            return None, JSONResponse({"success": False}, status_code=404)
    # 兜底再走一次 safe_join，确保 rel 无法穿越出 UPLOAD_DIR（历史脏数据防御）
    if security.safe_join(storage.UPLOAD_DIR, rel) is None:
        return None, JSONResponse({"success": False}, status_code=404)
    return rel, None


@router.get("/api/video-transcodes/{share_id}/{filepath:path}")
def share_video_transcodes(share_id: str, filepath: str,
                           password: str = "", access: str = ""):
    """列出某原始视频在 cache/video 下可用的转码格式。

    返回 {"success": true, "available": [{"fmt","label","path"}]}；
    available[].path 为【相对 cache/video】的转码文件路径（前端拼 /api/video-stream-fmt
    时直接用它，再带上原视频的 filepath 做鉴权定位）。无产物时 available 为空。
    """
    rel, err = _tc_guard(share_id, filepath, password, access)
    if err:
        return err
    available = []
    for key, label, cache_rel, abs_path in _tc_candidate_paths(rel):
        if os.path.isfile(abs_path):
            available.append({"fmt": key, "label": label, "path": cache_rel})
    return {"success": True, "available": available}


@router.get("/api/video-stream-fmt/{share_id}/{filepath:path}/{fmt}")
def share_video_stream_fmt(share_id: str, filepath: str, fmt: str,
                           request: Request,
                           password: str = "", access: str = "", dl: int = 0):
    """取 cache/video 下某转码格式的文件流（Range/206/分块/ETag，与视频流端点同机制）。

    - filepath = 原始视频的 UPLOAD_DIR 相对路径（鉴权/定位用）；
    - fmt = 转码格式（h264/hevc/av1/vp9，枚举），与 filepath 一起经 _tc_candidate_paths
      精确定位 cache/video 下的产物——fmt 是枚举而非自由路径，天然防穿越；
    - 前端先经 /api/video-transcodes 拿到"该视频有哪些 fmt 可用"，选中后拼本端点；
    - ?dl=1 时按附件下载该转码文件。
    """
    rel, err = _tc_guard(share_id, filepath, password, access)
    if err:
        return err
    fmt = (fmt or "").lower()
    wanted = {k: (label, cache_rel, abs_path)
              for k, label, cache_rel, abs_path in _tc_candidate_paths(rel)}
    if fmt not in wanted:
        return JSONResponse({"success": False}, status_code=404)
    _label, cache_rel, abs_path = wanted[fmt]
    if not os.path.isfile(abs_path):
        return JSONResponse({"success": False}, status_code=404)

    fname = os.path.basename(abs_path)
    media_type = share_util.video_content_type(fname)
    file_size = os.path.getsize(abs_path)
    mtime = int(os.path.getmtime(abs_path))
    cache_ctl = "no-store" if (access or password) else "public, s-maxage=3600, max-age=3600"
    etag = f'"{share_id}-tc-{fmt}-{file_size}-{mtime}"'
    return _serve_media_range(request, abs_path, media_type, etag, cache_ctl,
                              filename=(fname if dl else None))


# ---------------------------------------------------------------------------
# fMP4 + DASH（路线 B）：manifest + 单文件分片。
# 依赖 utils/dashtask（惰性切片 + 指纹失效 + 容量裁剪）；无 ffmpeg 时
# dashtask.ensure_dash 返回 ''，端点 404 → 前端自动回退到上面的渐进式视频流。
# ---------------------------------------------------------------------------

def _resolve_share_video_path(share: dict, filename: str):
    """解析共享视频流端点的目标视频绝对路径；越界/不存在返回 None。

    - 文件夹型：share["path"] 是目录，filename 是相对共享根的子路径，
      两级 safe_join（先锁共享目录，再防 filename 穿越逃出）。
    - 单文件型：share["path"] 本身就是那个视频文件，URL 的 filename 段是
      它的 basename（前端按此拼链接），直接返回文件、忽略 filename（单文件
      唯一，basename 已对应），否则"文件路径/文件名"恒 404。
    """
    base = os.path.abspath(storage.UPLOAD_DIR)
    if share.get("type") == "file":
        p = security.safe_join(base, share["path"])
        return p if p and os.path.isfile(p) else None
    share_dir = security.safe_join(base, share.get("path") or "")
    if share_dir is None:
        return None
    p = security.safe_join(share_dir, filename)
    return p if p and os.path.isfile(p) else None


def _dash_guard(share_id: str, filename: str, password: str, access: str):
    """DASH 端点前置校验：共享存在 / 解锁 / 未过期 / 视频路径合法。

    返回 (rel, error_resp)；rel 为视频相对 UPLOAD_DIR 路径（定位 cache/dash
    分片）；校验失败时 error_resp 非空，调用方直接 return。
    """
    share = share_util.find_share(share_id)
    if share is None:
        return None, JSONResponse({"success": False}, status_code=404)
    if not share_util.share_unlocked(share, share_id, password, access):
        return None, JSONResponse({"success": False, "message": "密码错误"}, status_code=403)
    expire_time = datetime.strptime(share["expire"], "%Y-%m-%d %H:%M:%S")
    if datetime.now() > expire_time:
        return None, JSONResponse({"success": False, "message": "共享已过期"}, status_code=410)
    video_path = _resolve_share_video_path(share, filename)
    if video_path is None:
        return None, JSONResponse({"success": False}, status_code=404)
    rel = os.path.relpath(video_path, os.path.abspath(storage.UPLOAD_DIR))
    return rel, None


def _complete_mpd_video_codecs(mpd_text: str, full_codecs: str) -> str:
    """把 MPD 里裸的 HEVC codec 标签（codecs="hvc1"/"hev1"）补全为带 profile/level 的
    完整串（如 hvc1.1.6.L93.B0）。

    背景：ffmpeg 的 DASH muxer 对 HEVC 只写裸 codec 标签，而浏览器
    MediaSource.isTypeSupported() 对裸串返回 false（只对带 profile/level 的串认账）
    → dash.js 按 MPD codecs 做能力 gate 时视频 Representation 被丢弃，只剩音频
    （"有声无画"）。补全后 gate 通过，视频轨挂上。

    取值策略：
    - meta 的 video_codecs（生成时 ffprobe 探测到的精确串）→ 优先用它；
    - 探测不到（存量缓存/meta 缺键）→ 从 MPD 的 width/height 推一个保守 level
      （L93≈1440p / L123≈4K / L183≈8K，解码器支持高级别必支持低级），
      声明为 8-bit Main（hvc1.1.6 / hev1.1.6）；
    - MPD 里没有裸 HEVC 标签（H.264/AV1/VP9 的串本身完整）→ 原样返回。"""
    # 只处理"裸"HEVC 标签（codecs 后紧跟闭引号，即 4 字符短串）；
    # avc1.xxxx / av01.xxxx 等完整串不会被此正则命中，天然不受影响
    bare = re.search(r'codecs="(hvc1|hev1)"', mpd_text)
    if not bare:
        return mpd_text
    brand = bare.group(1)
    if full_codecs and (full_codecs.startswith("hvc1") or full_codecs.startswith("hev1")):
        target = full_codecs                       # 生成时探测到的精确串
    else:
        # 存量缓存兜底：从 MPD 读 max 分辨率推 level
        wmax = hmax = 0
        for wm in re.finditer(r'width="(\d+)"\s+height="(\d+)"', mpd_text):
            wmax = max(wmax, int(wm.group(1)))
            hmax = max(hmax, int(wm.group(2)))
        if wmax > 3840 or hmax > 2160:
            level = "L183"
        elif wmax > 2560 or hmax > 1440:
            level = "L123"
        else:
            level = "L93"
        target = "%s.1.6.%s.B0" % (brand, level)   # 8-bit Main 保守声明
    return re.sub(r'codecs="%s"' % re.escape(brand),
                  r'codecs="%s"' % target, mpd_text)


def _rewrite_mpd_basurl(mpd_text: str, share_id: str, filename: str, access: str,
                        video_codecs: str = "") -> str:
    """把 MPD 里 `<BaseURL>x-streamN.mp4</BaseURL>` 改写为指向 media 端点的
    绝对/相对 URL（dash.js 据此拉分片）；并补全裸 HEVC codecs 串（video_codecs）。

    分片端点带视频文件名（quote）+ 可选 access 令牌。MPD 里 ffmpeg 写的
    <BaseURL>manifest-streamN.mp4</BaseURL> 是"相对目录 + 分片名"；dash.js 会
    用 BaseURL + 分片名拼完整请求。这里把 BaseURL 改写为 media 端点前缀
    /api/video-dash-media/{share_id}/{filename}/（分片名仍由 MPD 内 Representation
    的 SegmentURL mediaRange + BaseURL 承载，dash.js 据此拉对应 .mp4）。
    """
    # 前缀 = /api/video-dash-media/{share_id}/{filename}/；分片名（MPD 里
    # <BaseURL> 原内容，如 h264-stream0.mp4）保留在尾段；access 令牌追加为
    # 查询串（须落在分片名之后，故这里用占位再回填，不能简单拼在前缀末尾）
    prefix = "/api/video-dash-media/" + quote(share_id) + "/" + quote(filename) + "/"
    qsuffix = ("?access=" + quote(access)) if access else ""

    def _sub(m):
        seg_name = m.group(1)                 # 原 <BaseURL> 内容（分片文件名）
        return "<BaseURL>" + prefix + quote(seg_name, safe="") + qsuffix + "</BaseURL>"

    result = re.sub(r"<BaseURL>([^<]*)</BaseURL>", _sub, mpd_text)
    # 补全裸 HEVC codecs 串（存量缓存即时生效；非 HEVC 源原样返回）
    return _complete_mpd_video_codecs(result, video_codecs)


@router.get("/api/video-dash/{share_id}/{filename:path}")
def share_video_dash(share_id: str, filename: str,
                     password: str = "", access: str = ""):
    """fMP4/DASH manifest 端点（返回改写 BaseURL 的 MPD）。

    - 路由里末尾 `mpd` 段仅作识别用：路径形如
      /api/video-dash/{share_id}/{视频文件名}，返回该视频的 DASH manifest；
    - BaseURL 被改写为 /api/video-dash-media/{share_id}/{视频文件名}/ 前缀，
      dash.js 对每个分片发 `<BaseURL>分片名` 的 Range 请求；
    - 无 ffmpeg / 未切片 → 404，前端回退到渐进式视频流（零风险降级）；
    - 密码共享带 access 令牌时 media 端点 no-store（与视频流缓存策略一致）。
    """
    rel, err = _dash_guard(share_id, filename, password, access)
    if err:
        return err
    if not dashtask.ensure_dash(rel):  # 惰性生成；失败/无 ffmpeg → 回退渐进式
        return JSONResponse({"success": False}, status_code=404)
    mpd_path = dashtask.manifest_path(rel)
    if not mpd_path:
        return JSONResponse({"success": False}, status_code=404)
    mpd = open(mpd_path, "r", encoding="utf-8").read()
    # 读 meta 里探测到的完整 HEVC codec 串（dashtask 生成时写）；
    # 存量缓存 meta 无该键（旧版生成）→ 探测不到则用保守默认（serve 侧兜底）
    meta = dashtask.dash_meta(rel) or {}
    video_codecs = meta.get("video_codecs", "")
    mpd = _rewrite_mpd_basurl(mpd, share_id, filename, access, video_codecs)
    cache_ctl = "no-store" if access else "public, max-age=60, s-maxage=60"
    return Response(mpd, media_type="application/dash+xml",
                    headers={"Cache-Control": cache_ctl})


@router.get("/api/video-dash-media/{share_id}/{filename:path}/{segment:path}")
def share_video_dash_media(share_id: str, filename: str, segment: str,
                           request: Request, password: str = "", access: str = ""):
    """fMP4/DASH 分片端点（单文件 + 字节 Range + ETag）。

    路径尾段 {segment} 是分片名（manifest-streamN.mp4）；按 dashtask 登记的
    meta 分片名单校验（防路径穿越、只服务真实分片）。4MB 分块 + Range/206；
    公开流边缘缓存 1h，密码流 no-store。无分片 / 无 ffmpeg → 404 回退渐进式。
    """
    rel, err = _dash_guard(share_id, filename, password, access)
    if err:
        return err
    seg = dashtask.segment_path(rel, segment)
    if not seg:
        return JSONResponse({"success": False}, status_code=404)
    size = os.path.getsize(seg)
    mtime = int(os.path.getmtime(seg))
    # ETag 只能含 latin-1 安全字符：中文视频文件名走 md5 短摘要（保留 size/mtime 唯一性）
    rel_tag = hashlib.md5(rel.encode("utf-8")).hexdigest()[:8]
    etag = f'"{share_id}-{rel_tag}-{segment}-{size}-{mtime}"'
    cache_ctl = "no-store" if (access or password) else "public, s-maxage=3600, max-age=3600"
    head = {"Accept-Ranges": "bytes", "Cache-Control": cache_ctl, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=head)
    range_header = request.headers.get("range")
    if range_header:
        try:
            s, e = range_header.replace("bytes=", "").split("-")
            start = int(s) if s else 0
            end = int(e) if e else size - 1
        except ValueError:
            return JSONResponse({"success": False}, status_code=416)
        end = min(end, size - 1)
        if start > end or start >= size:
            return Response(status_code=416, headers={**head, "Content-Range": f"bytes */{size}"})
        length = end - start + 1

        def iter_range():
            with open(seg, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(4 * 1024 * 1024, remaining))
                    if not chunk:
                        break
                    yield chunk
                    remaining -= len(chunk)

        return StreamingResponse(
            iter_range(), status_code=206, media_type="video/mp4",
            headers={**head, "Content-Range": f"bytes {start}-{end}/{size}",
                     "Content-Length": str(length)})
    return FileResponse(seg, media_type="video/mp4", headers=head)
