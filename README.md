# XS File Library

基于 **FastAPI + 原生 JS + Bootstrap 5** 的轻量级个人 / 小团队文件库系统。

无构建工具、无前端框架，开箱即跑；用 JSON 文件做持久化，适合单机部署到
个人网盘、NAS、或 Cloudflare 反代后的 VPS。功能覆盖：文件管理、用户与权限、
文件夹树、共享 / 访客上传、在线预览与视频选集连播、二维码分享、访问数据看板。

## 功能特性

- **文件管理**：上传 / 下载 / 删除 / 重命名 / 建目录，可折叠文件夹树展示，文件搜索
- **用户与权限**：登录鉴权（HMAC 签名 token）、基于权限项的细粒度授权、目录级权限
- **共享链接**：普通 / 相册两种模式，有效期 + 提取码，统一链接表（共享 + 访客上传）
- **访客上传**：无需登录的上传链接，支持有效期 / 密码 / 指定目录
- **在线预览**：图片灯箱（连续切换）、PDF、视频、音频、文本
- **视频播放**：选集面板 + 自动连播 + 上一/下一集，Range/206 流式播放，Cloudflare 边缘缓存
- **二维码分享**：链接二维码，配色 / 纠错 / 尺寸可配，编码完整公网 URL
- **访问数据看板**：PV / UV / TOP IP / 按天分布 / 各链接 PV
- **深色模式**、移动端适配、静态资源本地化（离线 / 内网可部署）

## 项目结构

```text
xs_file_lib/
├── main.py                 入口：挂载静态、注册路由、鉴权中间件
├── requirements.txt
├── routers/                按功能场景拆分的路由
│   ├── auth.py             登录（限流 + token 签发）
│   ├── users.py            用户管理
│   ├── files.py            文件管理（浏览/树/上传/下载/删除/重命名/目录权限）
│   ├── shares.py           共享链接 CRUD + 落地页 + 二维码
│   ├── share_media.py      共享内容/下载/密码校验/视频流（Range）
│   ├── share_log.py        访问信息/日志/数据看板
│   ├── media.py            图片/视频缩略图
│   ├── settings.py         系统设置
│   ├── upload_links.py     访客上传
│   └── zip_tasks.py        文件夹 zip 打包（两入口共用底层）
├── utils/
│   ├── storage.py          路径常量 + JSON 原子读写 + 目录迁移 + 链接统一存储
│   ├── security.py         路径穿越守卫 / 登录限流 / HMAC token / bcrypt / 上传限制
│   ├── share.py            共享公共 helper（查记录 / 密码闸门 / 视频 Content-Type）
│   ├── ziptask.py          zip 打包（文件夹变更即删旧重打包 + 容量裁剪）
│   ├── filetree.py         文件夹树构建 / 扁平化 / 媒体收集
│   └── hotstore.py         热写内存聚合（view_count / 访问日志，后台落盘）
├── static/
│   ├── index.html  share.html  share_video.html  upload.html
│   ├── css/        style / share / preview / share_video（+ share_video 独立）
│   ├── js/         common(XSLib) / app / share / preview / share_video
│   └── vendor/     本地化的 Bootstrap + Bootstrap Icons（含字体）
├── tests/                pytest 回归套件（见 tests/README.md）
│
├── config/               ① 人手工可改的轻量设置（配置）
│   ├── config.json         站点名 / 根目录 / 公网地址 / 缓存上限 / 二维码样式
│   └── folder_permissions.json
├── data/                 ② 程序写入的业务数据（备份必保，含密钥）
│   ├── users.json  secret.key  links.json  access_log.json
├── cache/                ③ 可再生产缓存（删了自动重建，无需备份）
│   ├── zips/               zip 打包缓存
│   └── thumbs/             图片/视频缩略图缓存
└── uploads/              ④ 用户文件
```

**目录职责三分**：*删掉会丢用户资产* 的进 `data/`（含密钥）；*删掉可自动重建* 的
进 `cache/`；*人手改* 的留在 `config/`。旧布局（业务数据在 `config/`、缩略图在
`.thumbs/`）会在启动时一次性幂等迁移到新位置。

## 安装与启动

**环境要求**
- Python ≥ 3.10（本项目在 3.14 验证通过）
- 系统已安装 **ffmpeg**（视频首帧缩略图由 PyAV 调用，纯 `pip install` 后若 `import av` 报 `libav*` 缺失，需先装 ffmpeg 全功能包并加入 PATH；Windows 可直接用 `winget install Gyan.FFmpeg`）

**步骤**

```bash
# 1) 建虚拟环境并安装依赖（版本已锁定在 requirements.txt）
python -m venv .venv
# Windows: .venv\Scripts\Activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# 2) 本地开发（--reload）
uvicorn main:app --reload

# 3) 生产
uvicorn main:app --host 0.0.0.0 --port 8000
```

访问 `http://127.0.0.1:8000`。

**首次启动会自动完成**（`storage.init_config()` 兜底，缺失的目录与默认配置全部自动创建，无需手动建 `config/`、`data/`、`cache/`）：
- 目录：`uploads/`、`config/`、`data/`、`cache/thumbs/`、`cache/zips/`
- 文件：`config/config.json`（默认设置）、`data/users.json`（默认账号）、`data/links.json`、`data/access_log.json`、`data/secret.key`（随机 HMAC 密钥）

**上线前建议核对的默认项**（设置页"⚙️ 系统设置"）：
- **默认账号 `admin` / `admin123`（首次生成，请立即在"用户管理"改密）**
- `public_base_url`（公网访问地址，二维码编码完整 URL 用，留空则按请求头推断）
- `cache_max_gb`（zip 缓存上限，默认 10G）、`thumb_size_list` / `thumb_size_preview`（缩略图尺寸）
- 上传上限 `upload_max_mb` 与扩展名白名单 `upload_extensions`（默认 100MB + 内置白名单）

## 安全加固

| 项 | 说明 |
|---|---|
| 路径穿越防护 | `utils/security.safe_join` 守卫全部文件接口（下载/删除/上传/改名/建目录/共享/视频流/缩略图）；上传文件名取 basename |
| 登录限流 | 按 IP 连续 5 次失败冷却 300s |
| 管理 API 鉴权 | HMAC-SHA256 签名 token（`Authorization: Bearer` 或 `?token=`），保护管理/数据类接口；公开分享/上传/媒体保持开放 |
| 会话密钥 | 首次启动随机生成 32 字节落 `data/secret.key`（不随代码分发），env `XS_TOKEN_SECRET` 可覆盖 |
| 密码哈希 | **bcrypt**（随机盐），存量 SHA-256 在下次登录时透明升级 |
| 共享密码 + 过期 | 内容/下载/视频流全部补齐密码闸门与过期校验；校验后签发**短期 access 令牌**，后续请求免重复验证 |
| 上传限制 | 单文件 100MB 上限（可配）+ 扩展名白名单，超限/非白名单即时拒绝并清理残留 |

## 视频播放

- 流式端点支持 **Range / 206**，同步执行（慢客户端不阻塞事件循环）+ 4MB 分块
- `Content-Type` 按扩展名推断（不再写死 `video/mp4`）
- **ETag + Cache-Control**：公开流 `s-maxage=3600`（Cloudflare 边缘缓存整对象，
  拖动/回看由边缘合成 206）；密码流 `no-store`（过期后缓存立即失效）
- 前端选集面板 + 自动连播 + 上下集；"全屏"为**页面内占满**（保留浏览器界面）

> Cloudflare 用户：对 `contains(uri.query,"access=")` 一律 Bypass cache；
> `/api/video-stream/`、`/api/share*-download/` 设为 Cache eligible + Respect Origin。

## 二维码分享

`/api/qr/{link_id}` 生成 PNG（`qrcode` 库）。基址优先级：设置 `public_base_url`
→ 反向代理头 `X-Forwarded-Proto/Host` → 请求 host（公网扫码可打开真实地址）。
样式（配色 / 纠错 / 模块尺寸 / 边距）在**设置页**可配，配色为成对高级色板。

## 性能

- **热写内存聚合**：`view_count` 与访问日志请求路径只写进程内存，后台线程每 30s
  原子落盘 + 滚动裁剪（访问日志 5000 条上限），热门链接不再逐请求 fsync
- **zip 生命周期**：文件夹指纹变更即删旧重打包；按 `cache_max_gb`（默认 10G，可配）
  最旧优先裁剪；启动清理 index 一致性
- **缩略图缓存**：图片/视频首帧落盘 + `Cache-Control: max-age=30d`（边缘长期复用）

## 测试

回归套件在 `tests/`，覆盖登录鉴权、路径穿越、上传白名单、二维码、密码共享闸门、
zip 打包、访问看板、限流、目录迁移、热写聚合。运行方式与用例说明见
[tests/README.md](tests/README.md)。

```bash
python -m pytest tests/ -q
```

## 技术栈

- **后端**：FastAPI + Uvicorn + Python
- **前端**：原生 HTML/CSS/JS + Bootstrap 5 + Bootstrap Icons（本地化）
- **媒体**：Pillow（图片缩略图）/ PyAV（视频首帧）/ qrcode / bcrypt
- **存储**：JSON 文件（原子写 + 线程锁）+ 内存热写聚合
