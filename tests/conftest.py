"""pytest 夹具：在隔离临时目录中跑真实 FastAPI 应用（TestClient）。

数据目录（config / uploads / .thumbs / cache/zips）全部重定向到临时目录，
互不污染真实部署数据；密钥、用户、链接都在临时空间生成，测试结束即弃。
"""
import os
import sys
import tempfile
import shutil
import json

import pytest

# 项目根目录（本文件位于 <root>/tests/）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(scope="session")
def app_server():
    """会话级：拷贝一份应用数据到临时目录并起 TestClient，返回 (client, base_dir)。"""
    import utils.storage as storage
    import main as main_mod

    tmp = tempfile.mkdtemp(prefix="xs_test_")
    # 重定向所有持久化路径到临时目录（新目录布局：config 人改 / data 业务 / cache 可再生产）
    storage.UPLOAD_DIR = os.path.join(tmp, "uploads")
    storage.THUMB_DIR = os.path.join(tmp, "cache", "thumbs")
    storage.CACHE_ZIP_DIR = os.path.join(tmp, "cache", "zips")
    storage.USER_CONF = os.path.join(tmp, "data", "users.json")
    storage.FOLDER_PERMS_CONF = os.path.join(tmp, "config", "folder_permissions.json")
    storage.ACCESS_LOG_CONF = os.path.join(tmp, "data", "access_log.json")
    storage.SETTING_CONF = os.path.join(tmp, "config", "config.json")
    storage.LINK_CONF = os.path.join(tmp, "data", "links.json")
    storage.SECRET_CONF = os.path.join(tmp, "data", "secret.key")
    # 旧版迁移文件指到临时目录（避免误读真实旧数据）
    storage.LEGACY_SHARE_CONF = os.path.join(tmp, "config", "shares.json")
    storage.LEGACY_UPLOAD_LINK_CONF = os.path.join(tmp, "config", "upload_links.json")
    # 目录迁移测试项：模拟旧布局遗留（临时目录内构造旧文件，验证会被搬到新位置）
    storage.DIR_MIGRATIONS = [
        (os.path.join(tmp, "config", "users.json"), os.path.join(tmp, "data", "users.json")),
        (os.path.join(tmp, "config", "secret.key"), os.path.join(tmp, "data", "secret.key")),
        (os.path.join(tmp, "config", "links.json"), os.path.join(tmp, "data", "links.json")),
        (os.path.join(tmp, "config", "share_access_log.json"), os.path.join(tmp, "data", "access_log.json")),
    ]
    storage.LEGACY_DIRS_TO_PURGE = [os.path.join(tmp, ".thumbs")]

    storage.init_config()

    from fastapi.testclient import TestClient
    client = TestClient(main_mod.app)
    yield client, tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="session")
def base_dir(app_server):
    """临时项目根（含 config/uploads 等）。"""
    return app_server[1]
