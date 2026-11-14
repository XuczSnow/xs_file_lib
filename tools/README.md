# tools/ —— 离线维护脚本（不随 Web 服务运行）

独立、可单跑的标准库脚本，放在这里与运行时后端（`main.py` / `routers/` / `utils/`）分离，
避免根目录堆满一次性工具。均为"按需手动执行"，服务启动时不会调用。

| 脚本 | 用途 |
|---|---|
| `transcode.py` | 同片源多格式网页转码（h264/hevc/av1/vp9，GPU 优先自动回退，`--quality` 三档 / `--max-height` / 分格式音频码率）；**默认输出到 `cache/video/`（按 uploads 相对结构），不进 uploads 共享目录树**，播放页按目录到 `cache/video` 查可用格式 |
| `faststart_check.py` | MP4 `moov` 位置诊断 + 可选 faststart 重排（默认 dry-run 只报告，`--fix` 才重排到独立目录，不动源文件） |
| `make_dash.py` | fMP4 + DASH 单文件切片（视频零重编 `-c copy`，音频按需转 AAC）；产物写 `cache/dash/{key}/`，配合 `utils/dashtask.py` 惰性生成 + 播放页 dash.js 分片播放 |

## 用法示例

```bash
# 多格式转码（GPU 优先，自动回退 CPU）
python tools/transcode.py uploads/clip.mov --formats hevc,av1

# MP4 faststart 诊断（只报告哪些文件 moov 在尾部、体积多大）
python tools/faststart_check.py uploads/

# 需要时给 moov 尾部的 MP4 重排 faststart（输出到独立目录，源文件不动）
python tools/faststart_check.py uploads/ --fix --out cache/faststart

# fMP4 + DASH 切片（视频零重编，秒级）
python tools/make_dash.py uploads/clip.mp4            # 单文件
python tools/make_dash.py uploads/ --out cache/dash    # 递归整目录
python tools/make_dash.py uploads/clip.mp4 --base uploads   # 指定 base（key 口径见下）
```

## `transcode.py` 输出位置与播放页联动

- **默认输出到 `cache/video/`（不再写进 uploads 共享目录树）**：
  - 源文件位于 `uploads/` 内 → `cache/video/<相对 uploads 结构>/clip_<fmt>.<ext>`
    （如 `uploads/1111/1234/clip.mp4` → `cache/video/1111/1234/clip_h264.mp4`），与 DASH 缓存 key 口径一致；
  - 目录模式（`uploads/`）保留相对结构；单文件模式且源不在 `uploads/` 内 → 平铺 `cache/video/`。
  - `--out <dir>` 可显式覆盖（保留"相对源目录结构挂到该根下"的旧语义）。
- **为什么不进 uploads**：共享目录树应只显示原始视频；转码产物放 `cache/`（可再生产、随 `cache_max_gb` 裁剪），目录树干净，播放页"选集"不再靠"同片源后缀合并"（旧逻辑已删）。
- **播放页如何取格式**（`routers/share_media.py` + `share_video.js`）：
  - 切选集时 `GET /api/video-transcodes/{share_id}/{原视频上传相对路径}` 查该视频在 `cache/video` 下**实际存在**的转码格式（`available: [{fmt,label,path}]`）；
  - 格式下拉"自动 + 各真实存在的格式"；选中某格式 → 挂单 `<source>` 走 `GET /api/video-stream-fmt/{share_id}/{原视频路径}/{fmt}`（Range/206/分块/ETag，`?dl=1` 附件下载）；
  - "自动"仍优先 DASH 分段播放**原视频**（uploads 源，`dash_auto_slice` 语义不变）；转码格式不进 DASH 分段。
- **兼容性**：旧版脚本生成的"同目录"转码产物（`uploads/…/clip_h264.mp4`）**不再被识别**（已迁出 uploads 概念）；如需保留请手动把它们移到 `cache/video/<对应结构>/` 或重跑新脚本。

## `make_dash.py` 参数与 key 口径

- `--base`（默认 `uploads`）：决定"源 key"的计算口径 —— key = `md5(文件相对 base 的正斜杠路径，含文件名)[:16]`，
  产物落在 `cache/dash/{key}/`。与 `utils/dashtask.py` 的 `_rel_key`（`md5(UPLOAD_DIR 相对路径)[:16]`）**同口径**，
  所以离线预生成与在线惰性切片命中同一目录，互不覆盖。
- `--seg-duration`（默认 2s）：fMP4 分段时长；`--ffmpeg` 指定 ffmpeg 可执行文件。
- MPD 的 `<BaseURL>` 输出为纯分片文件名（`manifest-stream0.mp4`），服务端端点
  `GET /api/video-dash/{id}/{file}` 会将其改写为 `/api/video-dash-media/{share_id}/{filename}/` 前缀，
  前端无需关心绝对路径。
- 产物附 `meta.json`（源指纹 mtime+size + 编码清单；HEVC 源额外记 `video_codecs` 完整串如 `hvc1.1.6.L93.B0`，供服务端 serve 时补全 MPD 的裸 `hvc1` 标签——见 release note"DASH 播放有声无画修复"），`dashtask.ensure_dash()` 依此做"源变更即重切"。

## 与 `utils/dashtask.py` 的关系（惰性生成）

`make_dash.py` 是**可选的离线预生成**手段（适合批量预热缓存）。Web 服务内的默认链路是：

1. 播放页 dash.js 请求 `GET /api/video-dash/{id}/{file}`；
2. 端点调 `dashtask.ensure_dash(rel)`：缓存目录 key 命中且源指纹不变 → 直接返回 manifest；
   未命中/源已变 → 后台调用 ffmpeg 现切 fMP4 DASH（等同 `make_dash` 的产物）；
3. 整个 `cache/dash` 纳入设置里的 `cache_max_gb` 容量裁剪（最旧优先）。

因此不预跑 `make_dash.py` 也能正常分片播放，只是首次访问多等一次切片。

> **服务端自动切片默认关闭**（设置项 `dash_auto_slice`，设置页"相册设置"卡片的
> "自动切片共享视频"开关，默认不勾选）。关闭时 `ensure_dash` 直接返回空，
> manifest 端点 404，播放页一律走渐进式；**离线预切的产物需开启该开关后才会被
> 服务端复用**。播放页格式下拉的"分段"选项回退时会在选项上标注原因。

## dash.js（已内置真实实现）

`static/vendor/dash.js` 已内置 DASH-IF 官方 `dash.all.min.js`（v5.3.0，约 831KB）：
播放页探测到 `window.dashjs.MediaPlayer` 即尝试 DASH 分片播放，失败自动回退
原生 `<source>` 渐进式。升级版本时：

1. 下载新版 [DASH-IF Reference Player](https://cdn.dashjs.org/latest/dash.all.min.js)（v5+）；
2. 覆盖 `static/vendor/dash.js`；
3. 更新 `static/share_video.html` 中 script 标签的 `?v=dash53` 破缓存参数，刷新页面。

## ffmpeg 路径

各脚本的 ffmpeg 查找顺序：`--ffmpeg` 参数 > 系统 `PATH`。Web 服务内（`dashtask` / 转码）
另读 `config.json` 的 `ffmpeg_path` 键（设置界面可配，留空走 PATH）。
无 ffmpeg 时：faststart 纯诊断不受影响；`--fix` 与 DASH 切片不可用，manifest 端点返回 404，
前端静默回退渐进式。

> 依赖：`transcode.py` / `faststart_check.py` 仅标准库 + 系统 `ffmpeg`（`faststart_check.py` 的 `--fix` 才需 ffmpeg；纯诊断不需要）。
