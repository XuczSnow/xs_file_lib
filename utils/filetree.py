"""文件树构建工具：将目录遍历结果转换为嵌套树结构。

管理页（整库）与共享页（单个共享子目录）共用。
"""
import os
from datetime import datetime


def build_file_tree(base_dir: str, start_dir: str | None = None,
                    folder_permissions: dict | None = None,
                    username: str = "", include_files: bool = True) -> dict:
    """构建嵌套文件树。

    参数:
        base_dir       路径计算基准目录（文件 path 字段相对于此目录）。
        start_dir      实际遍历起点，默认等于 base_dir。
                       共享页传共享子目录，使 path 仍相对 UPLOAD_DIR。
        folder_permissions {目录相对路径: 允许用户列表}，命中且用户不在其中则不展开。
        username       当前用户（权限过滤用）。

    返回单个根节点：
        { name, path, type:"folder", children:[...], files:[...], size, file_count }
    """
    start_dir = start_dir or base_dir
    rel_path = os.path.relpath(start_dir, base_dir)
    if rel_path == ".":
        rel_path = ""
    return _walk(base_dir, start_dir, rel_path,
                  folder_permissions, username, include_files)


def _walk(base_dir: str, current_dir: str, rel_path: str,
          perms: dict | None, username: str, include_files: bool) -> dict:
    node = {
        "name": os.path.basename(current_dir) if rel_path else "",
        "path": "." if not rel_path else rel_path,
        "type": "folder",
        "children": [],
        "files": [],
        "size": 0,
        "file_count": 0,
    }

    # 权限过滤：命中且当前用户不在允许名单 → 不展开子内容
    if perms and rel_path in perms:
        allowed = perms[rel_path]
        if allowed and username not in allowed:
            return node

    try:
        entries = sorted(os.listdir(current_dir))
    except OSError:
        return node

    subdirs = []
    for entry in entries:
        full = os.path.join(current_dir, entry)
        if os.path.isdir(full):
            subdirs.append(entry)
        elif include_files:
            try:
                fsize = os.path.getsize(full)
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            node["files"].append({
                "name": entry,
                "path": (rel_path + "/" + entry) if rel_path else entry,
                "size": fsize,
                "modified": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
                "extension": os.path.splitext(entry)[1][1:].lower(),
            })
            node["size"] += fsize
            node["file_count"] += 1

    for subdir in subdirs:
        child_rel = (rel_path + "/" + subdir) if rel_path else subdir
        child_node = _walk(base_dir, os.path.join(current_dir, subdir),
                           child_rel, perms, username, include_files)
        node["children"].append(child_node)
        node["size"] += child_node["size"]
        node["file_count"] += child_node["file_count"]

    return node


def flatten_files(node: dict) -> list:
    """递归收集节点下所有文件（全层级），返回 [{name,path,size,modified,extension}]。"""
    out = []

    def _collect(n):
        out.extend(n.get("files", []))
        for c in n.get("children", []):
            _collect(c)

    _collect(node)
    return out


def collect_media_files(node: dict, image_exts: list, video_exts: list):
    """从树中递归收集可预览图片 / 视频（全层级）。返回 (images, videos)。"""
    images, videos = [], []

    def _collect(n):
        for f in n.get("files", []):
            ext = f.get("extension", "")
            if ext in image_exts:
                images.append({"path": f["path"], "name": f["name"]})
            elif ext in video_exts:
                videos.append({"path": f["path"], "name": f["name"]})
        for c in n.get("children", []):
            _collect(c)

    _collect(node)
    return images, videos
