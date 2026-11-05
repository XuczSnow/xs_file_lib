#!/usr/bin/env python3
"""同片源转码工具：从源视频生成多个网页可播版本（输出到源文件所在目录，同 stem）。

输出约定（--formats 选择视频编码格式；多选时文件名带格式后缀，单选保持原名）：
  h264 → xxx_h264.mp4  H.264 + AAC + faststart   兼容性最好（Safari/Chrome/老设备）
  hevc → xxx_hevc.mp4  HEVC + AAC + hvc1 tag     体积最小（Safari 可硬解）
  av1  → xxx_av1.webm  AV1 + Opus（WebM）         新一代高压缩比（Chrome/新设备）
  vp9  → xxx_vp9.webm  VP9 + Opus（WebM）         旧浏览器兜底
  相册页按"片源"去重只显示一张卡；视频播放页同片源并成一条选集，
  胶囊菜单可选"自动 / H.264 / HEVC / AV1 / VP9"分片（自动 = 浏览器 <source> 自选）。

音频（--audio，默认 auto）：
  auto → MP4 容器 AAC / WebM 容器 Opus；可显式 aac / opus / mp3 / copy（源音频直拷）。
  容器与所选音频不兼容时自动落到该容器默认并提示（如 WebM 里 aac → opus）。

编码器策略（GPU 优先，每个目标各持一条回退链；--no-gpu 强制全 CPU）：
  h264: h264_nvenc > h264_qsv > h264_amf → libx264
  hevc: hevc_nvenc > hevc_qsv > hevc_amf → libx265
  av1 : av1_nvenc(新驱动) → libsvtav1 > libaom-av1 > librav1d（全无 AV1 则降 VP9）
  vp9 : libvpx-vp9
  首选失败（如 NVENC 对个别源报 -22 Invalid argument）自动沿链重试，
  成功时打印实际使用的编码器；整条链失败才记失败。

统一质量入口（取代旧版各编码器分散参数）：
  --crf 28    CPU 编码器质量（x264/x265/svtav1/libaom/rav1d/vp9 通用）
  --gpu-cq 28 GPU 编码器恒定质量（NVENC constqp / QSV / AMF）
  --preset    速度档（x264/x265 preset 直传，VP9 映射 cpu-used；默认 medium）

用法示例：
  python transcode.py clip.mov                               # 默认 hevc + av1
  python transcode.py clip.mov --formats h264,hevc,av1,vp9   # 全 4 格式
  python transcode.py clip.mov --formats h264 --audio mp3    # 单格式 + 音频 mp3
  python transcode.py uploads/ --delete-source               # 批处理目录并删源
  python transcode.py uploads/ --out D:/web_export --no-gpu  # 指定输出目录 + 全 CPU

依赖：系统 ffmpeg（常规构建含 libx264/libx265/libvpx；GPU/AV1 视构建与驱动支持）。
纯标准库实现，无 pip 依赖；--ffmpeg 可指定 ffmpeg 路径。

行为说明：
  - 幂等：所选格式的全部输出都已存在且比源新 → 跳过（--force 强制重转）
  - 冲突：目标与源同路径时先把源挪成 xxx.orig 再转
  - 失败：删除该目标的不完整产物，保留源文件（已挪走的 .orig 不动）
"""
import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 可识别的源视频扩展名（网页不能直接播的 mkv/avi/flv 也列入，转出来即可播）
SRC_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".m4v", ".ts", ".webm", ".ogv"}

# 视频编码格式 → 容器扩展名 + 编码器回退链（GPU 项在前、CPU 在后；
# gpu 集合用于 --no-gpu 过滤；extra 为"主链全无可用编码器"时的末位降级）
FORMATS = {
    "h264": {
        "ext": ".mp4",
        "chain": ["h264_nvenc", "h264_qsv", "h264_amf", "libx264"],
        "gpu": {"h264_nvenc", "h264_qsv", "h264_amf"},
    },
    "hevc": {
        "ext": ".mp4",
        "chain": ["hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265"],
        "gpu": {"hevc_nvenc", "hevc_qsv", "hevc_amf"},
    },
    "av1": {
        "ext": ".webm",
        "chain": ["av1_nvenc", "libsvtav1", "libaom-av1", "librav1d"],
        "gpu": {"av1_nvenc"},
        "extra": ["libvpx-vp9"],
    },
    "vp9": {
        "ext": ".webm",
        "chain": ["libvpx-vp9"],
        "gpu": set(),
    },
}

NVENC = {"h264_nvenc", "hevc_nvenc", "av1_nvenc"}
QSV = {"h264_qsv", "hevc_qsv"}
AMF = {"h264_amf", "hevc_amf"}
# --preset → VP9 cpu-used 映射（x264/x265 时 preset 原样透传）
VP9_CPU_USED = {"fast": 8, "medium": 6, "slow": 4, "veryslow": 2}


# ---------------- 编码器探测 / 回退链 ----------------

def detect_encoders(ffmpeg) -> set:
    """解析 `ffmpeg -hide_banner -encoders` 输出，返回可用编码器名集合。"""
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=30,
        ).stdout or ""
    except Exception:
        return set()
    avail = set()
    for line in out.splitlines():
        # 行形如 " V....D hevc_nvenc   NVIDIA NVENC HEVC encoder (D = hardware)"
        parts = line.split()
        if len(parts) >= 2 and parts[0] and parts[0][0] in "VA":
            avail.add(parts[1])
    return avail


def build_chain(fmt: str, avail: set, no_gpu: bool):
    """构建某格式的编码器回退链，返回 (chain, note)。"""
    spec = FORMATS[fmt]
    chain = [e for e in spec["chain"]
             if not (e in spec["gpu"] and no_gpu) and e in avail]
    note = ""
    if not chain and spec.get("extra"):
        chain = [e for e in spec["extra"] if e in avail]
        note = "无 AV1 编码器，降级到 VP9"
    if not chain:
        chain = [spec["chain"][-1]]
        note = f"未探测到可用编码器，将尝试 {chain[0]}"
    return chain, note


# ---------------- 编码参数（统一质量入口） ----------------

def video_flags(fmt: str, enc: str, args) -> list:
    """按编码器生成 -c:v 与质量参数（CPU 用 --crf，GPU 用 --gpu-cq）。"""
    f = ["-c:v", enc]
    if enc in NVENC:
        # NVENC：constqp 恒定质量 + 无码率上限；p5 为速度/质量平衡档
        f += ["-rc", "constqp", "-cq", str(args.gpu_cq), "-b:v", "0",
              "-preset", "p5"]
    elif enc in QSV:
        f += ["-global_quality", str(args.gpu_cq)]
    elif enc in AMF:
        # AMF quality 0~100（越大越好）：用 gpu_cq(0~51) 近似映射
        f += ["-quality", str(min(95, args.gpu_cq * 2))]
    elif enc in ("libx264", "libx265"):
        f += ["-crf", str(args.crf), "-preset", args.preset]
    elif enc in ("libsvtav1", "libaom-av1", "librav1d"):
        f += ["-crf", str(args.crf)]
    elif enc == "libvpx-vp9":
        f += ["-crf", str(args.crf), "-b:v", "0",
              "-cpu-used", str(VP9_CPU_USED.get(args.preset, 6))]
    if fmt == "hevc":
        f += ["-tag:v", "hvc1"]   # MP4 中 HEVC 的 Apple/Safari 兼容 brand
    return f


def audio_flags(container: str, args):
    """生成 -c:a 参数。默认 auto：MP4→AAC / WebM→Opus。
    容器与所选音频不兼容时落到该容器默认（aac/mp3 in WebM→opus，opus in MP4→aac）
    并返回提示；copy 直拷源音频。"""
    a = args.audio
    if a == "auto":
        a = "aac" if container == ".mp4" else "opus"
    if a == "copy":
        return ["-c:a", "copy"], ""
    codec = {"aac": "aac", "opus": "libopus", "mp3": "libmp3lame"}[a]
    note = ""
    if container == ".webm" and a in ("aac", "mp3"):
        a, codec, note = "opus", "libopus", \
            f"WebM 音频落到 Opus（{a.upper()} 在 WebM 中浏览器普遍不可解）"
    if container == ".mp4" and a == "opus":
        a, codec, note = "aac", "aac", "MP4 音频落到 AAC（Opus 无法写入 MP4 容器）"
    f = ["-c:a", codec, "-b:a", args.audio_bitrate]
    if codec == "libopus":
        f += ["-ar", "48000"]
    return f, note


def build_command(src, target, args, fmt, enc) -> list:
    """按 (格式, 该目标实际使用的编码器) 生成 ffmpeg 命令。"""
    container = FORMATS[fmt]["ext"]
    base = [args.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(src), "-map", "0:v:0", "-map", "0:a?"]
    cmd = base + video_flags(fmt, enc, args) + audio_flags(container, args)[0]
    if container == ".mp4":
        cmd += ["-movflags", "+faststart"]
    cmd += ["-y", str(target)]
    return cmd


def run_ffmpeg(cmd, args):
    """跑一条 ffmpeg；成功返回 None，失败返回 proc（stderr 供日志）。"""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
    out = Path(cmd[-1])
    if proc.returncode == 0 and out.is_file() and out.stat().st_size > 0:
        return None
    return proc


# ---------------- 转码执行 ----------------

def is_fresh(target, src):
    """输出存在且比源新 → 视为已完成（幂等跳过）。"""
    try:
        return target.is_file() and target.stat().st_mtime >= src.stat().st_mtime
    except OSError:
        return False


def transcode_one(src, out_dir, args, chains: dict) -> dict:
    """转码单个源文件。args.formats（有序列表）决定目标集合：
    多选时文件名带 _<fmt> 后缀（xxx_h264.mp4 …），单选保持原名（xxx.mp4）。
    每个目标各持一条编码器回退链，某编码器成功即停；整链失败才记失败。
    返回 {skipped, done, failed, used}，used 记录各格式最终使用的编码器。"""
    sel = args.formats
    stem = src.stem
    multi = len(sel) > 1
    tasks = []
    for fmt in sel:
        suffix = f"_{fmt}" if multi else ""
        tasks.append((out_dir / f"{stem}{suffix}{FORMATS[fmt]['ext']}", fmt, chains[fmt]))

    if not args.force and all(is_fresh(t, src) for t, _, _ in tasks):
        return {"skipped": len(tasks), "done": [], "failed": [], "used": {}}

    # 冲突处理：目标与源同路径时先把源挪开（保留 .orig 可回滚）
    src_path = src
    for t, _, _ in tasks:
        if t.resolve() == src.resolve():
            src_path = src.with_name(src.name + ".orig")
            if src_path.exists():
                src_path.unlink()
            src.rename(src_path)
            break

    used, done, failed = {}, [], []
    for t, fmt, chain in tasks:
        start = time.time()
        last_err = None
        succeeded = False
        for enc in chain:
            cmd = build_command(src_path, t, args, fmt, enc)
            proc = run_ffmpeg(cmd, args)
            if proc is None:
                used[fmt] = enc
                done.append((t.name, round(time.time() - start, 1), enc))
                succeeded = True
                break
            t.unlink(missing_ok=True)   # 删掉该编码器产出的不完整产物
            last_err = proc
            if len(chain) > 1:
                tail = (proc.stderr or "").strip().splitlines()[-2:]
                print(f"    {enc} 失败（回退下一个）：{' '.join(tail) or f'exit={proc.returncode}'}",
                      file=sys.stderr)
        if not succeeded:
            tail = ((last_err.stderr or "").strip().splitlines()[-3:] if last_err else [])
            label = "该编码器" if len(chain) == 1 else f"全部 {len(chain)} 个回退编码器"
            failed.append((t.name, f"{label}均失败" +
                            (": " + " ".join(tail) if tail else "")))

    if args.delete_source and not failed:
        src_path.unlink(missing_ok=True)
    return {"skipped": 0, "done": done, "failed": failed, "used": used}


def iter_sources(path: Path):
    """收集待转码源：文件或目录（递归，按扩展名过滤）。"""
    if path.is_file():
        yield path
        return
    for p in sorted(path.rglob("*")):
        if p.is_file() and p.suffix.lower() in SRC_EXTS and ".orig" not in p.name:
            yield p


def main():
    ap = argparse.ArgumentParser(
        description="同片源转码（H.264 / HEVC / AV1 / VP9 四种网页可播版本，供相册页去重与播放页分片选择）")
    ap.add_argument("source", help="源视频文件或目录（目录则递归批处理）")
    ap.add_argument("--out", help="输出根目录（默认输出到源文件所在目录；目录模式保留相对结构）")
    ap.add_argument("--formats", default="hevc,av1",
                    help="视频编码格式（逗号分隔多选）：h264,hevc,av1,vp9（默认 hevc,av1；"
                         "多选时文件名带格式后缀，单选保持原名）")
    ap.add_argument("--audio", default="auto", choices=["auto", "aac", "opus", "mp3", "copy"],
                    help="音频格式：auto=容器默认（MP4→AAC / WebM→Opus），"
                         "copy=源音频直拷（默认 auto）")
    ap.add_argument("--crf", type=int, default=28,
                    help="CPU 编码器质量（x264/x265/svtav1/libaom/rav1d/vp9 通用，越小越清晰，默认 28）")
    ap.add_argument("--gpu-cq", type=int, default=28,
                    help="GPU 编码器恒定质量（NVENC constqp / QSV / AMF，默认 28）")
    ap.add_argument("--preset", default="medium",
                    help="速度档：x264/x265 preset 直传，VP9 映射 cpu-used（默认 medium）")
    ap.add_argument("--audio-bitrate", default="128k",
                    help="音频码率（默认 128k；audio=copy 时忽略）")
    ap.add_argument("--no-gpu", action="store_true",
                    help="禁用 GPU 编码器（NVENC/QSV/AMF/GPU-AV1），全部用 CPU")
    # 执行选项
    ap.add_argument("--ffmpeg", default="", help="ffmpeg 可执行文件路径（默认取 PATH）")
    ap.add_argument("--delete-source", action="store_true",
                    help="所选格式全部成功后删除源文件")
    ap.add_argument("--force", action="store_true", help="已有输出也强制重转")
    ap.add_argument("--timeout", type=int, default=7200, help="单条转码超时秒数（默认 7200）")
    args = ap.parse_args()

    # 解析并校验 --formats
    sel = [s.strip().lower() for s in args.formats.split(",") if s.strip()]
    for s in sel:
        if s not in FORMATS:
            ap.error(f"--formats 非法取值：{s}（可选 h264,hevc,av1,vp9）")
    if not sel:
        ap.error("--formats 至少包含一个格式")
    args.formats = sel
    args.ffmpeg = find_ffmpeg(args.ffmpeg)

    # 编码器自动探测 + 按格式构建回退链（GPU 优先）
    avail = detect_encoders(args.ffmpeg)
    chains = {}
    print("[编码器回退链]")
    for fmt in sel:
        chain, note = build_chain(fmt, avail, args.no_gpu)
        chains[fmt] = chain
        print(f"  {fmt:<4} → { ' > '.join(chain) }" + (f"（{note}）" if note else ""))

    # 容器与音频不兼容时的自动替换提示（打印一次，不重复）
    for container in sorted({FORMATS[f]["ext"] for f in sel}):
        _flags, note = audio_flags(container, args)
        if note:
            print(f"[音频] {note}")

    root = Path(args.source)
    if not root.exists():
        sys.exit(f"路径不存在：{root}")

    total = done_all = skipped_all = failed_all = 0
    for src in iter_sources(root):
        out_dir = Path(args.out) if args.out else src.parent
        if args.out and root.is_dir():
            out_dir = Path(args.out) / src.parent.relative_to(root)
        out_dir.mkdir(parents=True, exist_ok=True)

        r = transcode_one(src, out_dir, args, chains)
        total += 1
        skipped_all += r["skipped"]
        done_all += len(r["done"])
        failed_all += len(r["failed"])
        if r["skipped"]:
            print(f"[{total}] {src.name}: 已存在且更新，跳过（--force 可重转）")
        elif r["done"] and not r["failed"]:
            print(f"[{total}] {src.name}: 完成 " +
                  "，".join(f"{n}({s}s,{enc})" for n, s, enc in r["done"]))
        else:
            print(f"[{total}] {src.name}: 失败 " +
                  "；".join(f"{n}: {msg}" for n, msg in r["failed"]), file=sys.stderr)

    print("-" * 46)
    print(f"共 {total} 个源：转码 {done_all} 个产物，跳过 {skipped_all}，失败 {failed_all}")
    if failed_all:
        sys.exit(1)


def find_ffmpeg(name):
    path = shutil.which(name or "ffmpeg")
    if not path:
        sys.exit("找不到 ffmpeg：请先安装（加入 PATH），或用 --ffmpeg <路径> 指定")
    return path


if __name__ == "__main__":
    main()
