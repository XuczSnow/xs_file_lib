"""热数据内存聚合：view_count 与访问日志不再逐请求写盘。

原实现每次共享页访问都 load_shares + 计数 + fsync 写回 links.json、
追加并 fsync 写回 access_log.json——热门链接下 I/O 放大严重
（弱服务器 + 弱盘场景尤其明显）。

现改为：
- 请求路径只写【进程内存】（dict / list + 锁），微秒级；
- 后台守护线程按固定间隔把内存缓冲【原子合并】落盘一次
  （view_count 累加进 links.json；日志追加进 access_log.json 并滚动裁剪）；
- 进程退出时 atexit 再 flush 一次，保证不丢。

线程安全：计数/追加用自身锁；落盘复用 storage._save_lock 与
storage.load_links/load_json，"读-改-写"在锁内完成，
与并发 save_shares/save_links 不会交错写坏文件。

flush 间隔由设置项 hot_flush_sec 控制（默认 30s）：启动时与每次保存
设置后调用 set_flush_interval()，动态调整，无需重启进程。
"""
import atexit
import threading

from . import storage

# 后台 flush 间隔（秒，默认 30；可被设置项 hot_flush_sec 覆盖）
FLUSH_INTERVAL = 30

# 访问日志滚动上限：默认 5000，可被设置项 access_log_max 覆盖
# （动态读取 storage.access_log_max()，不在此硬编码）

_view_counter: dict = {}     # share_id -> 未落盘的 view_count 增量
_view_lock = threading.Lock()

_log_buffer: list = []       # 未落盘的访问日志条目
_log_lock = threading.Lock()

_flush_lock = threading.Lock()          # 串行化落盘本身
_flush_started = False
_flush_thread: threading.Thread = None
_flush_interval = FLUSH_INTERVAL        # 后台线程实际采用的间隔（可被 set_flush_interval 调整）


def bump_view_count(share_id: str):
    """共享被访问：只累加内存计数（调用方：share_log.share_info）。"""
    with _view_lock:
        _view_counter[share_id] = _view_counter.get(share_id, 0) + 1


def buffer_access_log(entry: dict):
    """访问日志只进内存缓冲，由后台线程落盘（替代原 write_share_access_log 的即时写盘）。"""
    with _log_lock:
        _log_buffer.append(entry)


def pending_logs() -> list:
    """返回尚未落盘的日志缓冲快照（读端点合并用，做到"写批量、读实时"）。"""
    with _log_lock:
        return list(_log_buffer)


def flush() -> dict:
    """把内存缓冲原子合并落盘，返回 {views: 条数, logs: 条数}。

    幂等、可重复调用；无缓冲时几乎零成本。
    """
    global _view_counter, _log_buffer
    # 1) 先把缓冲"摘走"（换新容器，后续请求继续写新容器，不阻塞）
    with _view_lock:
        views = _view_counter
        _view_counter = {}
    with _log_lock:
        logs = _log_buffer
        _log_buffer = []

    if not views and not logs:
        return {"views": 0, "logs": 0}

    with _flush_lock:
        # ---- view_count 累加进 links.json（读-改-写由 apply_links 在锁内原子完成）----
        if views:
            def _apply_views(links: dict) -> dict:
                for sid, inc in views.items():
                    rec = links.get(sid)
                    if rec is not None:
                        rec["view_count"] = int(rec.get("view_count", 0)) + inc
                return links
            storage.apply_links(_apply_views)

        # ---- 访问日志追加 + 滚动裁剪（读-改-写由 apply_access_log 在锁内原子完成）----
        if logs:
            limit = storage.access_log_max()   # 动态上限（设置项 access_log_max，默认 5000）

            def _apply_logs(cur: list) -> list:
                cur.extend(logs)
                if limit > 0 and len(cur) > limit:
                    cur = cur[-limit:]
                return cur
            storage.apply_access_log(_apply_logs)

    return {"views": len(views), "logs": len(logs)}


def _flush_loop():
    import time
    global _flush_interval
    while True:
        time.sleep(_flush_interval)
        try:
            flush()
        except Exception:
            pass  # 落盘失败（磁盘满等）不崩后台线程，下轮重试


def set_flush_interval(seconds: int):
    """动态调整后台 flush 间隔（设置页保存后热生效，无需重启）。最小 5s。"""
    global _flush_interval
    try:
        s = int(seconds)
        if s > 0:
            _flush_interval = max(5, s)
    except (ValueError, TypeError):
        pass


def apply_from_config():
    """从 config.json 读取热写间隔并应用到后台线程（设置页保存后调用）。"""
    try:
        set_flush_interval((storage.load_config() or {}).get("hot_flush_sec", FLUSH_INTERVAL))
    except Exception:
        pass


def start_flusher():
    """启动后台 flush 线程 + atexit 兜底（幂等，由 storage.init_config 调用）。"""
    global _flush_started, _flush_thread, _flush_interval
    if _flush_started:
        return
    apply_from_config()
    _flush_started = True
    _flush_thread = threading.Thread(target=_flush_loop, daemon=True)
    _flush_thread.start()
    atexit.register(flush)


def stop_and_flush():
    """测试/优雅停机用：停止后台线程并立即落盘剩余缓冲。"""
    global _flush_thread
    _flush_thread = None
    flush()
