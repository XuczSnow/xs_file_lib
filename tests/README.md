# tests/ — 回归测试说明

本目录是项目内置的 **pytest 回归套件**，在**隔离的临时目录**里跑真实的
FastAPI 应用（`TestClient`），验证核心安全机制与关键功能，**不污染真实部署
数据**（`config/`、`data/`、`uploads/` 全部重定向到临时目录）。

## 运行方式

```bash
# 需要测试依赖（已写入 requirements.txt）
pip install pytest httpx

# 全量回归
python -m pytest tests/ -q

# 只看某个模块
python -m pytest tests/test_app.py -q

# 只看安全相关
python -m pytest tests/test_app.py -k "traversal or upload or password" -q
```

> 说明：套件用 `httpx` 驱动的 `TestClient` 发真实 HTTP 请求，因此 `httpx`
> 是运行前提。`bcrypt`、`qrcode` 由应用本身依赖（见 `requirements.txt`）。

## 目录结构

| 文件 | 作用 |
|---|---|
| `conftest.py` | 会话级夹具：把 `storage` 的所有持久化路径重定向到临时目录，起一个 `TestClient`；把 `tests/` 加入 `sys.path` 使 `import main` 可用 |
| `test_app.py` | 主回归：登录 / 鉴权 / 路径穿越 / 上传 / 二维码 / 密码共享 / zip / 看板 / 限流 |
| `test_migration.py` | 目录重组迁移回归：旧 `config/` 业务数据搬到 `data/`、旧 `.thumbs` 清理、幂等性 |

**夹具模型**：`conftest.app_server`（session 级）只在整套测试里建一次，
`yield (client, tmp_dir)`；测试用 `app_server` 拿到客户端，用 `base_dir`
拿到临时目录去摆文件。

## 覆盖的行为（test_app.py）

| 测试 | 验证点 |
|---|---|
| `test_login_ok_and_token` | 登录成功返回签名 token |
| `test_protected_requires_token` | 无 token 访问管理 API 返回 401 |
| `test_protected_with_token` | 带 token 访问管理 API 返回 200 |
| `test_public_endpoints_open` | 公开接口（如 `/api/settings`）无 token 也可访问 |
| `test_wrong_password_rejected` | 错误密码被拒 |
| `test_sha256_legacy_hash_transparent_upgrade` | 旧 SHA-256 用户登录成功后哈希被透明升级为 bcrypt |
| `test_download_path_traversal_blocked` | `/api/download/../..` 路径穿越被拒（400/404） |
| `test_delete_root_dir_blocked` | 删除上传根目录被拒（400） |
| `test_upload_allowed_type` | 白名单内文件上传成功 |
| `test_upload_bad_type_rejected` | 非白名单（`.exe`）上传被拒（400） |
| `test_qr_returns_png` | 二维码返回 PNG（魔数校验） |
| `test_qr_missing_404` | 不存在链接的二维码返回 404 |
| `test_password_share_gating` | 密码共享：无令牌被拒、`share-check` 签发令牌、带令牌放行、错密码拒绝 |
| `test_zip_task` | 文件夹 zip 打包任务完成且下载为有效 zip（`PK` 头） |
| `test_zip_change_invalidates_old` | 文件夹变更后旧 zip 被删、新 zip 生成（无 24h 过期删除） |
| `test_share_stats_app` | 访问看板统计 PV / 各链接 PV |
| `test_login_rate_limit` | 连续失败 5 次后进入冷却（放最后，避免污染会话计数） |

## 覆盖的行为（test_migration.py）

| 测试 | 验证点 |
|---|---|
| `test_directory_layout_migration` | 旧布局 4 个业务文件全部搬进 `data/`、旧 `.thumbs` 被清理、内容完整、**再跑一次幂等（0 迁移）** |
| `test_migration_noop_when_already_new_layout` | 全新部署（无旧文件）时迁移为 0、不产生 `data/` |

## 编写新测试的约定

1. **复用夹具**：需要发 HTTP 的用 `app_server`（拿到 `client` 和 `base_dir`），
   需要临时摆文件时用 `os.path.join(base_dir, "uploads", ...)`。
2. **鉴权**：管理侧接口先 `_login(client)` 拿 token，用 `_auth(token)` 生成请求头；
   公开接口（`/share/*`、`/api/video-stream`、`/api/qr` 等）直接调。
3. **避免共享计数污染**：会改动"登录失败计数"等全局态的用例（如限流）放最后；
   `conftest` 把数据隔离在临时目录，但**进程内单例**（限流器、hotstore 缓冲）
   是共享的——新增全局态时注意用例间相互影响。
4. **热写聚合是异步落盘**：`view_count` / 访问日志由后台线程每 30s flush，
   断言"日志已写盘"前需显式调用 `utils.hotstore.flush()` 或等待；
   但读端点（`/api/share-access-log`、`/api/share-stats`）会**合并内存未落盘缓冲**，
   因此通过接口读数不需要等落盘。
5. 断言用 `assert 值, 提示` 带上可读信息；失败信息里带响应体便于排查。

## 已知边界

- 套件验证的是**行为契约**（状态码 / 响应体 / 文件是否落位），不覆盖像素级 UI。
- `zip_task` 等带后台线程的用例已内置轮询等待；如遇 flaky，优先查线程时序而非逻辑。
- 限流用例会占用进程内限流器计数，单跑它没问题，但与其它登录用例的相对顺序要留意
  （当前文件内已把限流排在最后）。
