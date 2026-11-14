#!/usr/bin/env python3
"""fMP4 + DASH 单文件切片脚本（标准库 + 系统 ffmpeg，不重编码视频）。

配合"路线 B"（fMP4 + DASH + dash.js）使用：
  - 输入一个网页视频（.mp4/.m4v/.webm/.mov）
  - 用 ffmpeg -c:v copy（视频零重编，秒级）+ -f dash -dash_segment_type mp4
    -single_file 1 切成「视频单文件 stream0.mp4 + 音频单文件 stream1.mp4 + manifest.mpd」
  - fMP4 容器对 H.264 / HEVC / (AV1-in-fMP4) 通吃；音频若源不是 AAC（如 WebM 的
    Opus 进不了 fMP4）则自动转 AAC，视频仍 copy 不重编
  - 产物写 cache/dash/{源指纹}/，附 meta.json（源指纹 + 时长 + 编码），供后端
    dashtask.py 做"源变更即重切 + 容量裁剪"的生命周期（复用 ziptask 那套）

single_file 模式：每个 Representation 落在一个 .mp4，MPD 用 mediaRange 以
字节 Range 指向文件内偏移——前端 dash.js 对同一文件发 Range 请求即可，
后端媒体端点复用现有视频流的 Range/ETag 能力。

用法：
  python tools/make_dash.py uploads/clip.mp4                 # 单文件
  python tools/make_dash.py uploads/ --out cache/dash        # 递归整目录
  python tools/make_dash.py uploads/clip.mp4 --seg-duration 4
  python tools/make_dash.py uploads/clip.mp4 --ffmpeg C:/tools/ffmpeg/bin/ffmpeg.exe
  python tools/make_dash.py uploads/clip.mp4 --json          # 机器可读

说明：
  - 纯离线工具，不随 Web 服务运行；服务内的惰性生成走 utils/dashtask.py。
  - 视频始终 -c copy（不重算像素）；只有音频在不兼容 fMP4 时才转 AAC。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

VIDEO_EXTS = (".mp4", ".m4v", ".webm", ".mov", ".mkv")


def source_fingerprint(path: Path) -> dict:
    """源文件指纹（mtime + size + md5），用于失效判断（源一变即需重切）。"""
    st = path.stat()
    h = hashlib.md5()
    h.update(str(st.st_mtime).encode())
    h.update(str(st.st_size).encode())
    return {"mtime": st.st_mtime, "size": st.st_size, "md5": h.hexdigest()}


def source_key(rel_path: str) -> str:
    """用源文件相对 base 的路径（归一化正斜杠、含文件名）作 cache/dash 子目录名。

    与 utils/dashtask._rel_key 同口径（md5 归一化后的 UPLOAD_DIR 相对路径），
    使离线预生成与运行时惰性切片命中同一目录。内容指纹（mtime+size）另存
    meta.json 用于"源变更即重切"。"""
    norm = (rel_path or "").replace("\\", "/").strip().lstrip("/")
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:16]


def probe_audio_codec(ffmpeg: str, src: Path) -> str:
    """ffprobe 源音频编码（读不到返回 'aac' 兜底为 copy 安全）。"""
    ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
    if not os.path.exists(ffprobe):
        # 同目录没有 ffprobe 时，用 ffmpeg 自己的 -show 探测成本高，直接保守转 AAC
        return "unknown"
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, timeout=60)
        codec = (r.stdout.strip().splitlines() or [""])[0].strip().lower()
        return codec
    except Exception:
        return "unknown"


def probe_video_codecs(ffmpeg: str, src: Path) -> str:
    """ffprobe 探测视频轨，返回完整 codec 串（HEVC 补 profile/level，如 hvc1.1.6.L93.B0）。

    与 utils/dashtask._probe_video_codecs 同口径（保持脚本独立，内联一份）：
    仅对 HEVC 探测（ffmpeg DASH muxer 对 HEVC 只写裸 hvc1/hev1 标签，浏览器 MSE
    能力 gate 不认裸串）；非 HEVC / 读不到 ffprobe → 返回 ""（serve 侧兜底补全）。"""
    ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
    if not os.path.exists(ffprobe):
        return ""
    _profile_idc = {"main": (1, 6), "main10": (2, 4)}   # general_profile_idc + 约束字节
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,profile,width,height",
             "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, timeout=60)
        row = (r.stdout.strip().splitlines() or [""])[0]
        parts = row.split(",")
        if len(parts) < 4 or parts[0].strip().lower() != "hevc":
            return ""
        profile = parts[1].strip().lower()
        w, h = int(parts[2]), int(parts[3])
        level = ("L93" if (w <= 2560 and h <= 1440)
                 else ("L123" if (w <= 3840 and h <= 2160) else "L183"))
        if profile in _profile_idc:
            pidc, constr = _profile_idc[profile]
            return "hvc1.%d.%d.%s.B0" % (pidc, constr, level)
        return "hvc1.1.6.%s.B0" % level   # profile 读不出时按 Main 8-bit 保守声明
    except Exception:
        return ""


def build_command(ffmpeg: str, src: Path, out_dir: Path, seg_duration: int) -> list:
    """构造 ffmpeg 命令：视频 copy + fMP4 DASH 单文件切片。

    音频策略：源音频若已是 aac 则 copy；否则（opus/vorbis/unknown）转 aac
    （fMP4 音频只认 AAC/EC-3，Opus 进不去 → 必须转）。视频恒 copy。
    """
    abs_src = Path(src).resolve()   # ffmpeg 带 cwd=out_dir 跑，输入须绝对路径
    audio_codec = probe_audio_codec(ffmpeg, abs_src)
    # 视频恒 copy；音频 aac 用 copy，其余转 aac（-c:a aac -b:a 128k）
    audio_flags = ["-c:a", "copy"] if audio_codec in ("aac",) else ["-c:a", "aac", "-b:a", "128k"]
    cmd = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(abs_src),
        "-map", "0:v:0", "-map", "0:a:0?",   # 视频必取，音频可选
        "-c:v", "copy",                       # 视频零重编
        *audio_flags,
        "-f", "dash",
        "-dash_segment_type", "mp4",          # fMP4 分片（HEVC/AV1-in-fMP4 通吃）
        "-seg_duration", str(seg_duration),
        "-single_file", "1",                  # 每 Representation 一个 .mp4，走 Range
    ]
    # 输出用 basename（配 run_dash 的 cwd=out_dir）：MPD 内 <BaseURL> 即纯分片名
    # （如 manifest-stream0.mp4），不带目录/反斜杠，便于后端改写为端点前缀
    return cmd + ["-y", "manifest.mpd"]


def run_dash(ffmpeg: str, src: Path, out_dir: Path, seg_duration: int, rel: str = "") -> dict:
    """切一个源 → out_dir/{manifest.mpd, manifest-streamN.mp4}。成功返回 meta。

    meta 记录实际分片文件名清单（MPD 里 BaseURL 用的相对名），供后端端点按名服务。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = build_command(ffmpeg, src, out_dir, seg_duration)
    proc = subprocess.run(cmd, cwd=out_dir, capture_output=True, text=True, timeout=7200)
    manifest = out_dir / "manifest.mpd"
    if proc.returncode != 0 or not manifest.is_file() or manifest.stat().st_size == 0:
        # 清理半成品
        for f in out_dir.glob("*.mp4"):
            f.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        raise RuntimeError(
            f"ffmpeg 切片失败: {proc.stderr.strip().splitlines()[-1] if proc.stderr else 'unknown'}")

    segments = sorted(f.name for f in out_dir.glob("*.mp4"))
    meta = {
        "source": str(src),
        "rel": rel,
        "fingerprint": source_fingerprint(src),
        "segment_duration": seg_duration,
        "single_file": True,
        "manifest": "manifest.mpd",
        "segments": segments,
        "audio_reencoded": probe_audio_codec(ffmpeg, src) not in ("aac",),
        # 完整 HEVC codec 串（补全裸 hvc1 用）；非 HEVC / 无 ffprobe → ""
        "video_codecs": probe_video_codecs(ffmpeg, src),
        "created_by": "make_dash.py",
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def main():
    ap = argparse.ArgumentParser(
        description="fMP4 + DASH 单文件切片（视频零重编，音频按需转 AAC）")
    ap.add_argument("source", help="视频文件或目录（目录则递归批处理）")
    ap.add_argument("--out", default="cache/dash", help="输出根目录（默认 cache/dash）")
    ap.add_argument("--base", default="uploads",
                    help="计算相对路径的基准目录（默认 uploads=UPLOAD_DIR，与服务端 dashtask 同口径）")
    ap.add_argument("--seg-duration", type=int, default=2,
                    help="分片时长秒（默认 2；首片小利于秒起播）")
    ap.add_argument("--ffmpeg", default="", help="ffmpeg 路径（默认取 PATH）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    ffmpeg = args.ffmpeg or shutil.which("ffmpeg") or ""
    if not ffmpeg:
        print("未找到 ffmpeg（可用 --ffmpeg 指定路径）")
        return 1

    base = Path(args.base).resolve()

    def _rel_to_base(f: Path) -> str:
        """f 相对 base 的路径（正斜杠、含文件名）；不在 base 内则退回文件名。"""
        try:
            return f.resolve().relative_to(base).as_posix()
        except ValueError:
            return f.name

    root = Path(args.source)
    if root.is_file():
        targets = [(root, _rel_to_base(root))]
    elif root.is_dir():
        targets = [(Path(d) / n, _rel_to_base(Path(d) / n))
                   for d, _, fs in os.walk(root) for n in sorted(fs)
                   if n.lower().endswith(VIDEO_EXTS)]
    else:
        print(f"路径不存在: {root}")
        return 1

    ok, fail = 0, 0
    out_root = Path(args.out)
    for src, rel in targets:
        rel_key = rel if rel else src.name   # 单文件兜底文件名
        key = source_key(rel_key)
        out_dir = out_root / key
        tag = f"[{rel or 'single'}] {src.name}"
        try:
            meta = run_dash(ffmpeg, src, out_dir, args.seg_duration, rel_key)
            ok += 1
            if not args.json:
                extra = "（音频转 AAC）" if meta["audio_reencoded"] else ""
                print(f"  [ok] {tag} → {out_dir} {extra}")
        except Exception as e:
            fail += 1
            if not args.json:
                print(f"  [fail] {tag}: {e}")
    if args.json:
        print(json.dumps({"ok": ok, "failed": fail, "out_root": str(out_root)},
                         ensure_ascii=False))
    else:
        print(f"--- 共 {len(targets)}：成功 {ok}，失败 {fail} ---")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
