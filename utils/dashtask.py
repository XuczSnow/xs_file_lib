"""fMP4 + DASH 分片缓存：服务端惰性生成 + 源变更重切 + 容量裁剪。

与 ziptask.py 同一套生命周期思想（指纹失效 + cache_max_gb 裁剪），但对象是
视频 DASH 分片（fMP4 单文件 + MPD）：

- 给定共享内视频的 rel 路径（相对 UPLOAD_DIR，如 1111/1234/clip_h264.mp4），
  cache/dash/{md5(rel)}/ 下放 manifest.mpd + manifest-streamN.mp4 + meta.json。
- ensure_dash(rel)：
    1. 源指纹（mtime+size）与 meta.json 记录一致且产物在 → 直接返回目录（幂等）；
    2. 源变了 / 首次 → ffmpeg 无重编码切片（视频 -c copy，音频按需转 AAC），
       成功后写 meta.json + 容量裁剪；
    3. 无 ffmpeg 或失败 → 返回 None，端点据此回退到普通渐进式视频流（零风险）。
- 前端 dash.js 缺失时也走渐进式（见 share_video.js 探测回退）。

容量：cache/dash 纳入 cache_max_gb 统一裁剪（与 zips 同策略，最旧优先）。
"""
import hashlib
import json
import os
import shutil
import subprocess
import threading

from utils import storage

# 默认目录（供模块导入期兜底）；实际路径按调用动态读 storage.DASH_DIR（_dash_root），
# 使测试 / 部署重定向 storage.DASH_DIR 后立即生效，不被导入期固化值绕过
DASH_DIR = getattr(storage, "DASH_DIR", "./cache/dash")


def _dash_root() -> str:
    """动态取 DASH 分片缓存根目录（读 storage.DASH_DIR，随部署/测试重定向即时生效）。"""
    return getattr(storage, "DASH_DIR", DASH_DIR)


def _auto_slice_enabled() -> bool:
    """设置项 dash_auto_slice（默认 False = 不自动分片）。

    关闭时仅阻止"未命中 → 自动重切"：ensure_dash 命中已有缓存（含离线
    make_dash.py 产物）仍直接复用，开关关闭时**不会**把合法缓存一刀切掉
    （否则离线预切的服务端会始终 manifest 404，播放页恒回退渐进式）。
    即：开关只控制"要不要现在自动切"，不影响"复用已切好的"。"""
    try:
        return bool((storage.load_config() or {}).get("dash_auto_slice", False))
    except Exception:
        return False

# 与 ziptask 共享容量上限（cache_max_gb）；这里单独统计 dash 子目录
_lock = threading.Lock()
_ffmpeg_cache = {"path": None, "checked": False}


def _norm_rel(rel: str) -> str:
    """路径分隔符归一化为 "/"（Windows 下 os.path.relpath 给反斜杠），
    保证离线 make_dash.py 与运行时端点传入的 rel 映射到同一 key。"""
    return (rel or "").replace("\\", "/").strip().lstrip("/")


def _rel_key(rel: str) -> str:
    """rel（相对 UPLOAD_DIR 的视频路径）→ 稳定的 dash 子目录名（归一化后算 md5）。"""
    return hashlib.md5(_norm_rel(rel).encode("utf-8")).hexdigest()[:16]


def get_ffmpeg() -> str:
    """取 ffmpeg 路径：config.json 的 ffmpeg_path（可配）→ PATH。找不到返回 ''。"""
    if not _ffmpeg_cache["checked"]:
        try:
            cfg = storage.load_config() or {}
        except Exception:
            cfg = {}
        p = (cfg.get("ffmpeg_path") or "").strip()
        if p and os.path.exists(p):
            _ffmpeg_cache["path"] = p
        else:
            _ffmpeg_cache["path"] = shutil.which("ffmpeg") or ""
        _ffmpeg_cache["checked"] = True
    return _ffmpeg_cache["path"]


def _source_fingerprint(path) -> dict:
    st = os.stat(path)
    return {"mtime": st.st_mtime, "size": st.st_size}


def _probe_audio_codec(ffmpeg: str, src: str) -> str:
    ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
    if not os.path.exists(ffprobe):
        return "unknown"
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", src],
            capture_output=True, text=True, timeout=60)
        return (r.stdout.strip().splitlines() or [""])[0].strip().lower()
    except Exception:
        return "unknown"


# ---- 视频 codec 探测（补全 DASH MPD 的 codecs 串，供 serve 侧改写）----
# ffmpeg 的 DASH muxer 对 HEVC 只写裸 "hvc1"/"hev1"（不带 profile/level），而浏览器
# MediaSource.isTypeSupported() 对裸 hvc1 返回 false（只对带 profile/level 的具体串认账）
# → dash.js 按 MPD 的 codecs 做能力 gate 时视频轨被丢，只剩音频（"有声无画"）。
# 这里用 ffprobe 读 codec_name + profile + 分辨率，拼出完整的 codec 串存进 meta，
# serve 侧 _rewrite_mpd_basurl 用它补全 MPD（存量缓存即时生效，无需重切）。

# HEVC hvc1 的 general_profile_idc：Main=1 / Main10=2（8-bit 与 10-bit 的 4:2:0 约束字节）
_HEVC_PROFILE_IDC = {"main": (1, 6), "main10": (2, 4)}
# HEVC 按分辨率取保守 level 上界（支持该 level 的解码器必然支持更低）：
#   1440p 及以下 → L93（实测 1440p 浏览器对 hvc1.1.6.L93.B0 返回 true）
#   4K 及以下    → L123（4K60 级）；8K 及以上      → L183
def _hevc_level(w: int, h: int) -> str:
    if w <= 2560 and h <= 1440:
        return "L93"
    if w <= 3840 and h <= 2160:
        return "L123"
    return "L183"


def _probe_video_codecs(ffmpeg: str, src: str) -> str:
    """ffprobe 探测视频轨，返回完整 codec 串（如 hvc1.1.6.L93.B0 / av01.0.12M.08 / avc1.4d401f）。

    读不到 ffprobe / 非 HEVC（ffmpeg 本就写全串）→ 返回 ""（serve 侧保留 MPD 原值）。
    仅对 HEVC 补全（这是唯一被 muxer 写裸串、且浏览器 gate 严格的情况；
    H.264/AV1/VP9 的 MPD codecs 串本身完整，无需补）。"""
    ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
    if not os.path.exists(ffprobe):
        return ""
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,profile,width,height",
             "-of", "csv=p=0", src],
            capture_output=True, text=True, timeout=60)
        row = (r.stdout.strip().splitlines() or [""])[0]
        parts = row.split(",")
        if len(parts) < 4 or parts[0].strip().lower() != "hevc":
            return ""
        profile = parts[1].strip().lower()
        w, h = int(parts[2]), int(parts[3])
        if profile in _HEVC_PROFILE_IDC:
            pidc, constr = _HEVC_PROFILE_IDC[profile]
            # hvcC 品牌：ffmpeg 默认打 hvc1（hvcC 配置盒）
            return "hvc1.%d.%d.%s.B0" % (pidc, constr, _hevc_level(w, h))
        return "hvc1.1.6.%s.B0" % _hevc_level(w, h)   # profile 读不出时按 Main 8-bit 保守声明
    except Exception:
        return ""


def _segment(src_abs: str, out_dir: str, ffmpeg: str) -> bool:
    """视频 copy + fMP4 单文件 DASH 切片；成功返回 True。"""
    audio_codec = _probe_audio_codec(ffmpeg, src_abs)
    audio_flags = ["-c:a", "copy"] if audio_codec in ("aac",) else ["-c:a", "aac", "-b:a", "128k"]
    cmd = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", src_abs,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "copy",
        *audio_flags,
        "-f", "dash", "-dash_segment_type", "mp4",
        "-seg_duration", "2", "-single_file", "1",
        "-y", "manifest.mpd",   # basename + cwd=out_dir → MPD 内 BaseURL 为纯分片名
    ]
    proc = subprocess.run(cmd, cwd=out_dir,
                          capture_output=True, text=True, timeout=7200)
    manifest = os.path.join(out_dir, "manifest.mpd")
    if proc.returncode != 0 or not os.path.isfile(manifest) or os.path.getsize(manifest) == 0:
        for f in os.listdir(out_dir):
            if f.endswith(".mp4"):
                try:
                    os.remove(os.path.join(out_dir, f))
                except OSError:
                    pass
        return False
    return True


def ensure_dash(rel: str) -> str:
    """确保持有 rel 视频的 DASH 分片，返回目录；不可用（无 ffmpeg/失败）返回 ''。

    惰性 + 幂等：源指纹（mtime+size）与 meta 一致即复用——**缓存命中先于开关判断**，
    因此 tools/make_dash.py 离线预切的产物在 dash_auto_slice 关闭时仍会被复用
    （开关只控制"未命中时是否自动切"，不该把已有的合法缓存一刀切掉，
    否则离线预切的服务端始终 manifest 404，播放页恒回退渐进式）。
    源变更/首次（未命中）且开关关闭 → 返回 ''（不自动切，manifest 404 → 渐进式）；
    未命中且开关开启 → 重切并更新 meta + 容量裁剪。
    """
    rel = _norm_rel(rel)
    if not rel:
        return ""
    src_abs = os.path.abspath(os.path.join(storage.UPLOAD_DIR, *rel.split("/")))
    if not os.path.isfile(src_abs):
        return ""

    key = _rel_key(rel)
    out_dir = os.path.join(_dash_root(), key)

    with _lock:
        meta_path = os.path.join(out_dir, "meta.json")
        fp = _source_fingerprint(src_abs)
        if os.path.isfile(meta_path):
            try:
                meta = json.loads(open(meta_path, "r", encoding="utf-8").read())
                mp = meta.get("fingerprint") or {}
                # 口径兼容：离线 make_dash.py 写的 fingerprint 多一个 md5 键，
                # 直接 dict 相等会误判"未命中"导致白切一次 → 只比 mtime+size 子字段。
                # 命中（含离线产物）直接复用，不受 dash_auto_slice 开关影响。
                if (mp.get("mtime") == fp["mtime"] and mp.get("size") == fp["size"]
                        and os.path.isfile(os.path.join(out_dir, "manifest.mpd"))):
                    return out_dir  # 命中缓存（幂等，含离线 make_dash 产物）
            except (OSError, ValueError):
                pass
        # 未命中 → 需要重切；开关关闭时不自动切（直接放弃，保留任何旧产物但不复用）
        if not _auto_slice_enabled():
            return ""
        ffmpeg = get_ffmpeg()
        if not ffmpeg:
            return ""
        os.makedirs(out_dir, exist_ok=True)
        # 未命中 → 重切（先清旧产物）
        for f in os.listdir(out_dir):
            if f.endswith(".mp4") or f == "manifest.mpd":
                try:
                    os.remove(os.path.join(out_dir, f))
                except OSError:
                    pass
        if not _segment(src_abs, out_dir, ffmpeg):
            return ""
        segments = sorted(n for n in os.listdir(out_dir) if n.endswith(".mp4"))
        meta = {
            "rel": rel, "fingerprint": fp, "single_file": True,
            "manifest": "manifest.mpd", "segments": segments,
            # 完整视频 codec 串（HEVC 补全用；非 HEVC 源为空，serve 侧保留 MPD 原值）
            "video_codecs": _probe_video_codecs(ffmpeg, src_abs),
            "source": src_abs, "created_by": "utils/dashtask.py",
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        enforce_capacity()
    return out_dir


def dash_meta(rel: str):
    """读 rel 视频的 dash meta（不存在返回 None）。"""
    rel = _norm_rel(rel)
    meta_path = os.path.join(_dash_root(), _rel_key(rel), "meta.json")
    if not os.path.isfile(meta_path):
        return None
    try:
        return json.loads(open(meta_path, "r", encoding="utf-8").read())
    except (OSError, ValueError):
        return None


def manifest_path(rel: str):
    p = os.path.join(_dash_root(), _rel_key(rel), "manifest.mpd")
    return p if os.path.isfile(p) else None


def segment_path(rel: str, name: str):
    """按 meta 里的分片文件名服务（防路径穿越：name 只允许 meta 登记过的）。"""
    meta = dash_meta(rel)
    if not meta or name not in meta.get("segments", []):
        return None
    p = os.path.join(_dash_root(), _rel_key(rel), name)
    return p if os.path.isfile(p) else None


def enforce_capacity():
    """cache/dash 纳入 cache_max_gb 统一裁剪（最旧优先）。返回删除的目录数。"""
    limit = storage.cache_max_bytes()
    root = _dash_root()
    if limit <= 0 or not os.path.isdir(root):
        return 0
    entries = []
    total = 0
    for key in os.listdir(root):
        d = os.path.join(root, key)
        if not os.path.isdir(d):
            continue
        try:
            mtime = os.path.getmtime(d)
        except OSError:
            continue
        size = sum(os.path.getsize(os.path.join(d, f))
                   for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)))
        entries.append((mtime, key, size))
        total += size
    removed = 0
    if total > limit:
        for mtime, key, size in sorted(entries, key=lambda t: t[0]):
            if total <= limit:
                break
            shutil.rmtree(os.path.join(root, key), ignore_errors=True)
            total -= size
            removed += 1
    return removed


def init():
    """启动时：建目录 + 一次性容量裁剪（与 ziptask 启动自检一致）。"""
    os.makedirs(_dash_root(), exist_ok=True)
    enforce_capacity()
