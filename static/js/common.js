/**
 * 公共工具模块（XSLib）。
 *
 * 汇总各页面共享的纯函数工具，避免在 app.js / share.js / preview.js /
 * 各 HTML 内联脚本中重复实现。挂载到 window.XSLib，供其它脚本通过
 * `const X = window.XSLib` 解构使用。
 *
 * 约定：本页所有 HTML 必须在自身业务脚本之前引入本文件，例如
 *   <script src="/static/common.js"></script>
 *
 * 模块内容一览：
 *   - escapeHtml   HTML 转义（防 XSS）
 *   - formatSize   字节 → 人类可读大小
 *   - getExt       取文件扩展名
 *   - *_EXTS       各媒体类型扩展名白名单（图片/视频/音频/文本）
 *   - postForm     表单 POST 封装
 *   - getJSON      GET 封装
 *   - pollZipTask  zip 打包任务轮询（Promise 化，带超时与失败）
 *   - loadSiteSetting 拉取站点设置并更新 <title>
 *   - loadMediaBase  IPv6 媒体改道：读设置 + 可达性探测 + 自动降级
 *   - resolveUrl     给站内资源/下载路径拼 IPv6 基址前缀（不可用则相对）
 *   - copyText     剪贴板复制（含非 https 降级）
 */
(function (global) {
    "use strict";

    /**
     * HTML 转义，防止用户名/文件名等用户输入注入 innerHTML（XSS）。
     * 依次替换 5 个特殊字符；先用 & 防止后续替换产生二次转义。
     * @param {*} str 任意值，null/undefined 归一化为空串
     * @returns {string} 安全的 HTML 片段
     */
    function escapeHtml(str) {
        return String(str ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#39;");
    }

    /**
     * 字节数格式化为 B/KB/MB/GB（保留 1 位小数）。
     * @param {number} bytes 字节数
     * @returns {string}
     */
    function formatSize(bytes) {
        if (bytes < 1024) return bytes + " B";
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
        if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
        return (bytes / 1024 / 1024 / 1024).toFixed(1) + " GB";
    }

    /**
     * 取文件名的小写扩展名（不含点）。无扩展名返回空串。
     * @param {string} name 文件名
     * @returns {string}
     */
    function getExt(name) {
        const parts = String(name).split(".");
        return parts.length > 1 ? parts.pop().toLowerCase() : "";
    }

    /* 媒体类型扩展名白名单：预览/缩略图/相册据此判断可处理类型 */
    const IMAGE_EXTS = ["jpg", "jpeg", "png", "gif", "webp", "bmp"];
    const VIDEO_EXTS = ["mp4", "webm", "ogg"];
    const AUDIO_EXTS = ["mp3", "wav", "m4a", "flac"];
    const TEXT_EXTS = ["txt", "log", "md", "json", "csv"];

    /* ---------- 会话令牌（管理 API 鉴权）----------
     * 登录成功后由 app.js 调 setAuth 存入（module 变量 + localStorage）。
     * 后续管理 API 请求会自动带上 Authorization 头；浏览器整页跳转的
     * 下载链接用 authedUrl 把 token 放进查询参数。
     * 页面加载时从 localStorage 恢复上次会话，免重新登录。
     */
    let authToken = localStorage.getItem("xs_token") || "";

    /** 保存/清空会话令牌。@param {string} token 登录接口返回的 token。 */
    function setAuth(token) {
        authToken = token || "";
        if (token) localStorage.setItem("xs_token", token);
        else localStorage.removeItem("xs_token");
    }

    /** 返回需注入管理 API 请求的鉴权请求头（无 token 时返回空对象）。 */
    function authHeaders() {
        return authToken ? { Authorization: "Bearer " + authToken } : {};
    }

    /**
     * 给浏览器整页跳转用的 URL 追加 token 查询参数（下载等场景，
     * 因为 window.location 无法附带请求头）。
     * @param {string} url 目标 URL
     * @returns {string} 追加了 token 的 URL
     */
    function authedUrl(url) {
        if (!authToken) return url;
        return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(authToken);
    }

    /**
     * 以 FormData 形式 POST 各键值，返回解析后的 JSON。
     * 自动附带管理 API 鉴权头。适用于后端 Form(...) 接收字段的接口。
     * @param {string} url 接口地址（相对路径）
     * @param {Record<string, string|number>} data 键值对
     * @returns {Promise<Object>}
     */
    async function postForm(url, data) {
        const fd = new FormData();
        Object.entries(data).forEach(([k, v]) => fd.append(k, v));
        const res = await fetch(url, { method: "POST", body: fd, headers: authHeaders() });
        return res.json();
    }

    /**
     * 发起 GET 并解析 JSON（自动附带管理 API 鉴权头）。
     * @param {string} url @returns {Promise<Object>}
     */
    async function getJSON(url) {
        const res = await fetch(url, { headers: authHeaders() });
        return res.json();
    }

    /**
     * 轮询 zip 打包任务状态，封装为 Promise（替代各页面自写的 setInterval）。
     *
     * 轮询 /api/share-folder-zip-status/{taskId}，直到：
     *   - status === "completed"：resolve 任务信息（含 zip_file / filename）
     *   - status === "failed"  ：reject 后端返回的错误信息
     *   - 轮询次数达到 maxAttempts：reject 超时错误
     *
     * @param {string} taskId 后端返回的打包任务 ID
     * @param {Object} [options]
     * @param {number} [options.intervalMs=1000] 轮询间隔（毫秒）
     * @param {number} [options.maxAttempts=180] 最大轮询次数（超时上限）
     * @param {Function} [options.onFail] 网络异常等意外时的额外回调
     * @returns {Promise<Object>} resolve 时得到任务信息
     */
    function pollZipTask(taskId, { intervalMs = 1000, maxAttempts = 180, onFail } = {}) {
        return new Promise((resolve, reject) => {
            let attempts = 0;
            const timer = setInterval(async () => {
                attempts += 1;
                try {
                    const data = await getJSON("/api/share-folder-zip-status/" + taskId);
                    if (data.status === "completed") {
                        clearInterval(timer);
                        resolve(data);
                    } else if (data.status === "failed") {
                        clearInterval(timer);
                        reject(new Error(data.message || "打包失败"));
                    } else if (attempts >= maxAttempts) {
                        clearInterval(timer);
                        reject(new Error("打包超时"));
                    }
                } catch (e) {
                    // 网络抖动等：清理定时器，通知外部并 reject
                    clearInterval(timer);
                    if (onFail) onFail(e);
                    reject(e);
                }
            }, intervalMs);
        });
    }

    /**
     * 拉取站点设置并把 <title>（#pageTitle）更新为站点名。
     * 各页面统一调用；失败时保留 HTML 里的默认标题。
     */
    async function loadSiteSetting() {
        try {
            const settings = await getJSON("/api/settings");
            document.getElementById("pageTitle").innerText = settings.site_name;
        } catch (e) {
            /* 站点名保持默认 */
        }
    }

    /**
     * 复制文本到剪贴板。优先用异步 Clipboard API；在非安全上下文
     * （http 非 localhost）下降级为临时 <textarea> + execCommand。
     * @param {string} text
     * @returns {Promise<boolean>} 是否复制成功
     */
    async function copyText(text) {
        try {
            await navigator.clipboard.writeText(text);
            return true;
        } catch (e) {
            const ta = document.createElement("textarea");
            ta.value = text;
            ta.style.position = "fixed";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.select();
            const ok = document.execCommand("copy");
            document.body.removeChild(ta);
            return ok;
        }
    }

    /* ---------- 媒体资源 IPv6 改道（可配 + 可达性探测 + 自动降级）----------
     * 站点设置 ipv6_base_url 填了 IPv6 基址后，分享/预览页的资源加载与下载
     * 链接优先走该基址；页面生成 URL 前调 loadMediaBase() 探测一次可达性，
     * 不可达（超时/网络错误）自动把 mediaBase 置空 → 降级回相对路径（= 当前
     * origin），功能不受影响。超时毫秒数可由设置项 ipv6_timeout_ms 配置。
     */
    let mediaBase = "";
    let _mediaBaseReady = null;   // Promise 缓存（本会话只探测一次）

    /**
     * 读取站点设置里的 ipv6_base_url + ipv6_timeout_ms，探测 IPv6 可达性，
     * 把 mediaBase 设为"可达的 IPv6 基址"（不可达 / 未配置则为 ""=相对路径）。
     * 各分享页必须在生成任何资源/下载 URL 之前调用一次（幂等，返回同一 Promise）。
     * @returns {Promise<string>} 生效的 mediaBase
     */
    function loadMediaBase() {
        if (_mediaBaseReady) return _mediaBaseReady;
        _mediaBaseReady = (async () => {
            try {
                const settings = await getJSON("/api/settings");
                const base = (settings.ipv6_base_url || "").trim();
                if (!base) { mediaBase = ""; return ""; }   // 未配置 → 相对路径
                // 超时毫秒：设置项 ipv6_timeout_ms（数字/数字串，>=0）；缺省 1500
                let timeout = 1500;
                const raw = settings.ipv6_timeout_ms;
                if (typeof raw === "number" && raw >= 0) timeout = raw;
                else if (typeof raw === "string" && raw !== "") {
                    const v = parseInt(raw, 10);
                    if (!isNaN(v) && v >= 0) timeout = v;
                }
                const probeUrl = base.replace(/\/+$/, "") + "/api/settings";
                const ctrl = new AbortController();
                const timer = setTimeout(() => ctrl.abort(), timeout || 1500);
                // no-cors：只需知道"连不连得上"，不关心响应体；成功 resolve 即判可达
                await fetch(probeUrl, {
                    method: "GET", mode: "no-cors",
                    cache: "no-store", signal: ctrl.signal,
                });
                clearTimeout(timer);
                mediaBase = base.replace(/\/+$/, "");
                return mediaBase;
            } catch (e) {
                mediaBase = "";   // 不可达 / 读取失败 → 降级相对路径
                return "";
            }
        })();
        return _mediaBaseReady;
    }

    /**
     * 把站内资源/下载相对路径拼上 mediaBase（IPv6 基址，未启用则为空）。
     * 仅对以 "/" 开头的站内地址生效；协议绝对地址（http:/https:/相对）原样返回。
     * 使用前须先 await loadMediaBase()。
     * @param {string} path 站内相对路径，如 "/api/thumbnail/xxx"
     * @returns {string} mediaBase + path
     */
    function resolveUrl(path) {
        if (!path || path[0] !== "/") return path;
        if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(path)) return path;  // 已带协议
        return mediaBase + path;
    }

    /* 汇总导出，挂载为全局 XSLib */
    global.XSLib = {
        escapeHtml,
        formatSize,
        getExt,
        IMAGE_EXTS,
        VIDEO_EXTS,
        AUDIO_EXTS,
        TEXT_EXTS,
        setAuth,
        authHeaders,
        authedUrl,
        postForm,
        getJSON,
        pollZipTask,
        loadSiteSetting,
        copyText,
        loadMediaBase,
        resolveUrl,
    };
})(window);
