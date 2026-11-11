#!/usr/bin/env python3
"""MP4 faststart（moov 前置）诊断 + 可选重排脚本（标准库 + 可选 ffmpeg）。

起播原理：MP4 播放前需要读到 moov box（元数据：时长/轨道/关键帧索引）。
  - moov 在文件头部（faststart）：首帧数据之前就能起播；
  - moov 在文件尾部：浏览器/CDN 要先拉完尾部 moov（长视频可达数 MB）才知
    道总时长 → 起播卡一下、拖动进度条先卡。

本脚本：
  1. 诊断（默认，dry-run，只读不改）：扫描目录/文件里所有 .mp4/.m4v，
     解析 top-level box 判 moov 位置与体积，输出报告。
  2. --fix：对"moov 在尾部"的文件用 ffmpeg -c copy -movflags +faststart
     无重编码重排（秒级，像素不动），输出到 --out 独立目录（默认
     cache/faststart，保留相对结构），**绝不改写 uploads 源文件**；
     并写 cache/faststart/manifest.json（源文件指纹 mtime+size，供后续
     失效判断：源一变即需重排）。

用法：
  python tools/faststart_check.py uploads/                    # 只诊断
  python tools/faststart_check.py uploads/ --min-moov 0       # 报告全部（含小 moov）
  python tools/faststart_check.py uploads/ --fix              # 诊断 + 重排到 cache/faststart
  python tools/faststart_check.py uploads/ --fix --out D:/fs --ffmpeg C:/tools/ffmpeg/bin/ffmpeg.exe
  python tools/faststart_check.py uploads/ --json             # 机器可读输出

默认阈值 --min-moov 1MB：moov <= 1MB 的文件即使尾部也快，标记 skip-small
（--fix 默认不处理，可用 --fix-small 强制）。
"""
import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

VIDEO_EXTS = (".mp4", ".m4v")


def analyze_mp4(path: Path) -> dict:
    """解析 MP4 top-level box，返回 moov 位置诊断信息（纯只读）。"""
    total = path.stat().st_size
    info = {
        "file": str(path),
        "total": total,
        "moov_offset": None,
        "moov_size": None,
        "moov_at_head": None,
        "status": "unknown",  # ok | need-fix | skip-small | not-mp4 | unreadable
    }
    try:
        with open(path, "rb") as f:
            head = f.read(8)
            if len(head) < 8 or head[4:8] not in (b"ftyp", b"free"):
                info["status"] = "not-mp4"
                return info
            pos = 0
            mdat_before = False
            while pos < total:
                f.seek(pos)
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                size = struct.unpack(">I", hdr[:4])[0]
                btype = hdr[4:8]
                if btype == b"moov":
                    info["moov_offset"] = pos
                    if size == 1:  # 64 位 large size
                        size = struct.unpack(">Q", f.read(8))[0]
                    info["moov_size"] = size
                    info["moov_at_head"] = not mdat_before
                    info["status"] = "need-fix" if mdat_before else "ok"
                    break
                if btype == b"mdat":
                    mdat_before = True
                if size == 1:  # large box
                    pos += 16 + (struct.unpack(">Q", f.read(8))[0] - 16)
                elif size == 0:  # box extends to EOF
                    pos = total
                else:
                    pos += size
            if info["status"] == "unknown":
                info["status"] = "moov-missing"
        return info
    except OSError:
        info["status"] = "unreadable"
        return info


def iter_targets(root: Path):
    """遍历文件/目录，产出 (file, relpath)（relpath 含文件名，相对 root）。"""
    if root.is_file():
        yield root, ""
        return
    for dirpath, _, files in os.walk(root):
        for name in sorted(files):
            if name.lower().endswith(VIDEO_EXTS):
                f = Path(dirpath) / name
                yield f, f.relative_to(root).as_posix()


def source_fingerprint(path: Path) -> dict:
    st = path.stat()
    h = hashlib.md5()
    h.update(str(st.st_mtime).encode())
    h.update(str(st.st_size).encode())
    return {"mtime": st.st_mtime, "size": st.st_size, "md5": h.hexdigest()}


def remux_faststart(src: Path, dst: Path, ffmpeg: str) -> bool:
    """ffmpeg 无重编码重排 moov 到头部；成功返回 True。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
           "-i", str(src), "-c", "copy", "-movflags", "+faststart",
           "-y", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        if proc.stderr:
            print(f"  ffmpeg 失败: {proc.stderr.strip().splitlines()[-1]}")
        return False
    return True


def main():
    ap = argparse.ArgumentParser(
        description="MP4 faststart 诊断（默认只读）+ 可选 ffmpeg 重排（不动源文件）")
    ap.add_argument("path", help="单个 MP4 文件或目录（递归扫描 .mp4/.m4v）")
    ap.add_argument("--min-moov", type=int, default=1048576,
                    help="moov 大于该字节才标记 need-fix 并被 --fix 处理（默认 1MB，0=全部）")
    ap.add_argument("--fix", action="store_true",
                    help="对需处理的文件用 ffmpeg -c copy -movflags +faststart 重排到 --out")
    ap.add_argument("--fix-small", action="store_true",
                    help="--fix 时连小 moov（<= --min-moov）也一并重排")
    ap.add_argument("--out", default="cache/faststart",
                    help="重排输出目录（保留相对结构；默认 cache/faststart，不动源文件）")
    ap.add_argument("--ffmpeg", default="", help="ffmpeg 可执行文件路径（默认取 PATH）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出诊断结果")
    args = ap.parse_args()

    root = Path(args.path)
    if not root.exists():
        print(f"路径不存在: {root}")
        sys.exit(1)

    results = []
    for f, rel in iter_targets(root):
        info = analyze_mp4(f)
        if info["status"] == "need-fix" and (info["moov_size"] or 0) <= args.min_moov:
            info["status"] = "skip-small"
        info["rel"] = rel
        results.append(info)

    # ---- 报告 ----
    if not args.json:
        print("=" * 72)
        print("MP4 faststart 诊断" + ("（--fix 模式：诊断 + 重排）" if args.fix else "（dry-run 只读）"))
        print(f"扫描: {root}   moov 阈值: {args.min_moov}B")
        print("=" * 72)
    mb = lambda n: f"{n / 1048576:.2f}MB" if n else "?"
    fix_list = []
    for r in results:
        mark = {
            "ok": "OK   ", "need-fix": "NEED ", "skip-small": "skip ",
            "not-mp4": "skip ", "moov-missing": "WARN ", "unreadable": "WARN ",
        }.get(r["status"], "???? ")
        if not args.json:
            loc = "头部" if r["moov_at_head"] else ("尾部" if r["moov_at_head"] is False else "  -")
            print(f"  [{mark}] {r['rel'] or r['file']}  "
                  f"(总 {mb(r['total'])})  moov {loc} {mb(r['moov_size'])}  [{r['status']}]")
        if r["status"] == "need-fix" or (args.fix_small and r["status"] == "skip-small" and args.fix):
            fix_list.append(r)

    summary = {
        "scanned": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "need_fix": sum(1 for r in results if r["status"] == "need-fix"),
        "skip_small": sum(1 for r in results if r["status"] == "skip-small"),
        "other": sum(1 for r in results if r["status"] in ("not-mp4", "moov-missing", "unreadable")),
        "fix_done": 0, "fix_failed": 0,
    }
    if not args.json:
        print("-" * 72)
        print(f"共 {summary['scanned']} 个文件：moov 前置 {summary['ok']}，"
              f"需重排 {summary['need_fix']}，小 moov 跳过 {summary['skip_small']}"
              f"，其它 {summary['other']}")
        if not args.fix and summary["need_fix"]:
            print("提示：加 --fix 用 ffmpeg 无重编码重排到独立目录（源文件不动）")

    # ---- 可选重排 ----
    if args.fix and fix_list:
        ffmpeg = args.ffmpeg or find_ffmpeg_in_path()
        if not ffmpeg:
            print("\n[fix] 未找到 ffmpeg（可用 --ffmpeg 指定路径），跳过重排")
        else:
            out_root = Path(args.out)
            manifest = {
                "note": "faststart 重排产物索引（源文件指纹 mtime+size，源变更即失效需重排）",
                "entries": {},
            }
            manifest_path = out_root / "manifest.json"
            if manifest_path.is_file():
                # 读回已有索引：skip 判断（recorded）依赖它跨进程生效，幂等
                try:
                    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if isinstance(loaded.get("entries"), dict):
                        manifest["entries"].update(loaded["entries"])
                except (OSError, ValueError):
                    pass
            for r in fix_list:
                src = Path(r["file"])
                rel_dir = Path(r["rel"]).parent if r.get("rel") else Path(".")
                dst = out_root / rel_dir / (src.name + ".fast.mp4")
                fp = source_fingerprint(src)
                # 幂等：产物比源新且指纹记录一致 → 跳过
                recorded = manifest.get("entries", {}).get(str(src))
                if dst.is_file() and recorded and recorded.get("src_fingerprint") == fp \
                        and dst.stat().st_mtime >= fp["mtime"]:
                    if not args.json:
                        print(f"  [skip] {dst}（已是最新）")
                    summary["fix_done"] += 1
                    continue
                if not args.json:
                    print(f"  [fix]  {src.name} → {dst}")
                if remux_faststart(src, dst, ffmpeg):
                    # 复验产物 moov 位置
                    check = analyze_mp4(dst)
                    ok2 = check["status"] == "ok"
                    if not args.json:
                        print(f"         结果: moov {'头部' if ok2 else '仍尾部(异常)'} "
                              f"{mb(check['moov_size'])}")
                    summary["fix_done" if ok2 else "fix_failed"] += 1
                    if ok2:
                        manifest["entries"][str(src)] = {
                            "out": str(dst), "src_fingerprint": fp,
                            "moov_size": check["moov_size"],
                        }
                else:
                    summary["fix_failed"] += 1
            out_root.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            if not args.json:
                print(f"\n[fix] 完成：成功 {summary['fix_done']}，失败 {summary['fix_failed']}")
                print(f"      索引: {out_root / 'manifest.json'}（源变更后指纹不匹配即需重排）")

    if args.json:
        print(json.dumps({"summary": summary, "results": results},
                         ensure_ascii=False, indent=2))
    elif summary["fix_failed"]:
        sys.exit(1)


def find_ffmpeg_in_path() -> str:
    return shutil.which("ffmpeg") or ""


if __name__ == "__main__":
    main()
