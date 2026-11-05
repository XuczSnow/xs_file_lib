"""目录重组迁移回归（任务 1）：旧 config/ 业务数据搬到 data/、旧 .thumbs 清理。

直接测 storage._migrate_directory_layout() 的行为，不依赖 app_server；
在独立临时目录里构造"旧布局"，迁移后断言新位置就位、旧位置消失、幂等。
"""
import os
import json

import pytest


def _make_old_layout(tmp):
    """在 tmp 下造一个旧布局：config/ 里有业务数据，.thumbs 里有点前缀缩略图。"""
    cfg = os.path.join(tmp, "config")
    os.makedirs(cfg, exist_ok=True)
    os.makedirs(os.path.join(tmp, ".thumbs", ".img_thumbs_600"), exist_ok=True)

    # 旧位置的业务数据
    with open(os.path.join(cfg, "users.json"), "w", encoding="utf-8") as f:
        json.dump({"admin": {"password": "x", "permissions": []}}, f)
    with open(os.path.join(cfg, "secret.key"), "w", encoding="utf-8") as f:
        f.write("0" * 64)
    with open(os.path.join(cfg, "links.json"), "w", encoding="utf-8") as f:
        json.dump({"l1": {"kind": "share", "path": "a.txt"}}, f)
    with open(os.path.join(cfg, "share_access_log.json"), "w", encoding="utf-8") as f:
        json.dump([{"share_id": "l1", "ip": "1.1.1.1"}], f)
    return cfg


def test_directory_layout_migration(tmp_path):
    import utils.storage as storage

    tmp = str(tmp_path)
    _make_old_layout(tmp)

    # 把迁移目标重定向到本测试目录（避免误伤真实 config/、data/）
    cfg = os.path.join(tmp, "config")
    data = os.path.join(tmp, "data")
    original = (storage.DIR_MIGRATIONS, storage.LEGACY_DIRS_TO_PURGE)
    try:
        storage.DIR_MIGRATIONS = [
            (os.path.join(cfg, "users.json"), os.path.join(data, "users.json")),
            (os.path.join(cfg, "secret.key"), os.path.join(data, "secret.key")),
            (os.path.join(cfg, "links.json"), os.path.join(data, "links.json")),
            (os.path.join(cfg, "share_access_log.json"), os.path.join(data, "access_log.json")),
        ]
        storage.LEGACY_DIRS_TO_PURGE = [os.path.join(tmp, ".thumbs")]

        moved = storage._migrate_directory_layout()

        # 1) 业务数据全部搬进 data/
        assert moved == 4, "应迁移 4 个业务文件"
        assert os.path.isfile(os.path.join(data, "users.json"))
        assert os.path.isfile(os.path.join(data, "secret.key"))
        assert os.path.isfile(os.path.join(data, "links.json"))
        assert os.path.isfile(os.path.join(data, "access_log.json"))
        # 旧 config/ 里业务文件已不在
        assert not os.path.exists(os.path.join(cfg, "links.json"))
        assert not os.path.exists(os.path.join(cfg, "secret.key"))
        # 内容保持完整（以 links.json 校验）
        assert json.load(open(os.path.join(data, "links.json"), encoding="utf-8"))["l1"]["kind"] == "share"

        # 2) 旧 .thumbs 目录被清理
        assert not os.path.exists(os.path.join(tmp, ".thumbs"))

        # 3) 幂等：再跑一次应 0 迁移、不报错
        assert storage._migrate_directory_layout() == 0
        assert os.path.isfile(os.path.join(data, "users.json")), "幂等后数据仍在"
    finally:
        storage.DIR_MIGRATIONS, storage.LEGACY_DIRS_TO_PURGE = original


def test_migration_noop_when_already_new_layout(tmp_path):
    """全新部署（无旧文件）：迁移应为 0 且不产生 data/ 目录。"""
    import utils.storage as storage
    tmp = str(tmp_path)
    os.makedirs(os.path.join(tmp, "config"))
    original = (storage.DIR_MIGRATIONS, storage.LEGACY_DIRS_TO_PURGE)
    try:
        storage.DIR_MIGRATIONS = [
            (os.path.join(tmp, "config", "users.json"), os.path.join(tmp, "data", "users.json")),
        ]
        storage.LEGACY_DIRS_TO_PURGE = [os.path.join(tmp, ".thumbs")]
        assert storage._migrate_directory_layout() == 0
        assert not os.path.exists(os.path.join(tmp, "data"))
    finally:
        storage.DIR_MIGRATIONS, storage.LEGACY_DIRS_TO_PURGE = original
