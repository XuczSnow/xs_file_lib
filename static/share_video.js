/**
 * 视频播放页（share_video.html）逻辑：选集面板 + 连播。
 *
 * URL 形如 /share/video/{share_id}/{filename}（?access=xxx 为密码共享令牌）。
 * 加载时拉 /api/share-content/{share_id} 取全层级视频列表作为选集，
 * 当前文件由 URL 定位；点选集只换 <video> 的 src（同页切源，不重开窗口），
 * 历史可回退。
 *
 * 连播：默认开启；当前集 ended 且开启时自动切下一集，末集停止。
 */
(function () {
    "use strict";

    const X = window.XSLib;
    const esc = X.escapeHtml;

    /* 模块状态：选集列表、当前索引、视频元素、共享类型（决定下载端点） */
    let episodes = [];
    let currentEp = 0;
    let shareType = "folder";
    const video = document.getElementById("videoPlayer");

    // ---------- 初始化 ----------

    window.onload = async function () {
        // 解析 URL：/share/video/{shareId}/{filename}，access 令牌透传
        const m = window.location.pathname.match(/^\/share\/video\/([^/]+)(\/.*)?$/);
        const backLink = document.getElementById("backToShare");
        const accessParam = new URLSearchParams(window.location.search).get("access");
        const qs = accessParam ? "?access=" + encodeURIComponent(accessParam) : "";
        if (!m) {
            document.title = "无效的视频链接";
            backLink.href = "/";
            return;
        }
        const shareId = m[1];
        // 返回共享带 access 令牌：密码共享回跳后免重新验证（share.js 识别 ?access=）
        backLink.href = "/share/" + shareId + (accessParam ? "?access=" + encodeURIComponent(accessParam) : "");

        // 拉取共享内容，提取全层级视频作为选集
        const data = await X.getJSON("/api/share-content/" + shareId + qs);
        if (!data || !data.success) {
            document.title = (data && data.message) || "共享不可用";
            document.getElementById("epList").innerHTML =
                '<div class="ep-empty">无法加载选集</div>';
            return;
        }
        shareType = data.type;   // "file" | "folder"，决定下载端点

        episodes = (data.files || [])
            .filter((f) => X.VIDEO_EXTS.includes(f.extension || X.getExt(f.name)))
            .map((f) => ({
                name: f.name,
                path: f.path,                     // 相对 UPLOAD_DIR 的全路径
                rel: f.path.startsWith(data.name + "/")
                    ? f.path.slice(data.name.length + 1)
                    : f.name,                    // 相对共享根（视频流接口用）
                size: f.size || 0,
            }));

        // 定位当前集（按 URL 的 filename 匹配，URL 段可能是相对共享根的路径）
        const urlFile = (m[2] || "").replace(/^\/+/, "");
        currentEp = Math.max(0, episodes.findIndex(
            (e) => e.rel === urlFile || e.path === urlFile || e.name === urlFile
        ));

        renderEpisodes();
        loadEpisode(currentEp, false);

        // ---------- 事件绑定 ----------
        document.getElementById("epList").addEventListener("click", (e) => {
            const item = e.target.closest("[data-ep]");
            if (item) {
                const idx = Number(item.dataset.ep);
                if (idx !== currentEp) {
                    loadEpisode(idx, true);
                    document.getElementById("epPanel").scrollIntoView({ block: "nearest" });
                }
            }
        });
        document.getElementById("prevEp").addEventListener("click",
            () => currentEp > 0 && loadEpisode(currentEp - 1, true));
        document.getElementById("nextEp").addEventListener("click",
            () => currentEp < episodes.length - 1 && loadEpisode(currentEp + 1, true));

        // 菜单栏：下载当前视频（按共享类型选端点：单文件 → share-download，
        // 文件夹 → share-folder-download；密码共享带 access 令牌）
        const dlBtn = document.getElementById("downloadBtn");
        if (dlBtn) {
            dlBtn.addEventListener("click", () => {
                const ep = episodes[currentEp];
                if (!ep) return;
                const acc = new URLSearchParams(window.location.search).get("access");
                const qs = acc ? "?access=" + encodeURIComponent(acc) : "";
                window.location.href = shareType === "file"
                    ? "/api/share-download/" + encodeURIComponent(currentShareId()) + qs
                    : "/api/share-folder-download/" + encodeURIComponent(currentShareId()) +
                        "/" + encodeURIComponent(ep.rel) + qs;
            });
        }

        // 菜单栏：自动连播开关（pill 组内按钮，.active 类 = 开启，默认开启）
        const apBtn = document.getElementById("autoplayBtn");
        let autoplayOn = true;
        if (apBtn) {
            apBtn.addEventListener("click", () => {
                autoplayOn = !autoplayOn;
                apBtn.classList.toggle("active", autoplayOn);
                apBtn.title = autoplayOn ? "自动连播：开" : "自动连播：关";
            });
        }

        // 菜单栏：全屏（页面内最大化模式，不进入 OS/浏览器全屏，保留浏览器界面）
        const maxBtn = document.getElementById("maxBtn");
        const maxIcon = maxBtn ? maxBtn.querySelector(".bi") : null;
        if (maxBtn) {
            maxBtn.addEventListener("click", () => {
                const on = document.body.classList.toggle("video-max");
                maxIcon.classList.toggle("bi-fullscreen", !on);
                maxIcon.classList.toggle("bi-fullscreen-exit", on);
                const lbl = maxBtn.querySelector(".lbl");
                if (lbl) lbl.textContent = on ? "退出" : "全屏";
                maxBtn.title = on ? "退出全屏" : "全屏";
            });
        }

        // 连播：当前集播完且开启开关时自动下一集
        video.addEventListener("ended", () => {
            if (!autoplayOn) return;
            if (currentEp < episodes.length - 1) loadEpisode(currentEp + 1, true);
        });

        // 键盘：← 上一集 / → 下一集 / F 全屏 / A 连播 / D 下载（焦点不在 video 上时生效）
        document.addEventListener("keydown", (e) => {
            if (e.target === video) return;
            if (e.key === "ArrowLeft" && currentEp > 0) loadEpisode(currentEp - 1, true);
            if (e.key === "ArrowRight" && currentEp < episodes.length - 1) loadEpisode(currentEp + 1, true);
            if ((e.key === "f" || e.key === "F") && maxBtn) maxBtn.click();
            if ((e.key === "a" || e.key === "A") && apBtn) apBtn.click();
            if ((e.key === "d" || e.key === "D") && dlBtn) dlBtn.click();
        });
    };

    // ---------- 选集面板 ----------

    /** 渲染选集列表：编号 + 首帧缩略图（失败兜底占位）+ 文件名 + 大小。 */
    function renderEpisodes() {
        const list = document.getElementById("epList");
        document.getElementById("epCount").innerText =
            episodes.length ? episodes.length + " 集" : "";
        if (!episodes.length) {
            list.innerHTML = '<div class="ep-empty">该共享没有视频</div>';
            return;
        }

        list.innerHTML = episodes.map((ep, i) => `
            <div class="ep-item${i === currentEp ? " active" : ""}" data-ep="${i}">
                <div class="ep-thumb">
                    <img src="/api/video-thumbnail/${encodeURIComponent(ep.path)}"
                         loading="lazy" alt="${esc(ep.name)}">
                </div>
                <div class="ep-no">${i + 1}</div>
                <div class="ep-meta">
                    <div class="ep-name">${esc(ep.name)}</div>
                    <div class="ep-sub">${X.formatSize(ep.size)}</div>
                </div>
            </div>`).join("");

        // 缩略图加载失败 → 替换为 ▶ 占位（统一用 JS error 监听，不写内联 onerror）
        list.querySelectorAll(".ep-thumb img").forEach((img) => {
            img.addEventListener("error", function () {
                this.style.display = "none";
                const fb = document.createElement("span");
                fb.className = "ep-thumb-fallback";
                fb.textContent = "▶";
                this.insertAdjacentElement("beforeend", fb);
            });
        });
    }

    // ---------- 播放与切集 ----------

    /**
     * 切换到第 idx 集（0 起）。
     * @param {number} idx 目标集
     * @param {boolean} updateHistory 是否写进浏览器历史（选集/上下集点击为 true，
     *   首次加载与自动连播为 false，避免污染后退栈）
     */
    function loadEpisode(idx, updateHistory) {
        if (!episodes[idx]) return;
        currentEp = idx;
        const ep = episodes[idx];

        // 拼视频流地址（后端 Range + ETag + 缓存头，见 shares.py share_video_stream）
        const accessParam = new URLSearchParams(window.location.search).get("access");
        const videoSrc = "/api/video-stream/" +
            encodeURIComponent(currentShareId()) + "/" +
            encodeURIComponent(ep.rel) +
            (accessParam ? "?access=" + encodeURIComponent(accessParam) : "");

        video.src = videoSrc;
        video.load();

        // 顶栏已移除：标题显示在浏览器标签页（document.title），含集数前缀
        document.title =
            (episodes.length > 1 ? `${idx + 1} / ${episodes.length} · ` : "") + ep.name;

        // 菜单栏：上一集/下一集按钮随当前集启用/禁用
        document.getElementById("prevEp").disabled = idx <= 0;
        document.getElementById("nextEp").disabled = idx >= episodes.length - 1;

        // 高亮当前集 + 自动滚动到可见
        document.querySelectorAll("#epList .ep-item").forEach((el, i) => {
            el.classList.toggle("active", i === idx);
            if (i === idx) el.scrollIntoView({ block: "nearest" });
        });

        // 有选集时更新地址栏（history.replaceState 避免连播/切集堆叠历史）
        if (updateHistory && episodes.length > 1) {
            const base = "/share/video/" + currentShareId() + "/" +
                encodeURIComponent(ep.rel);
            const acc = new URLSearchParams(window.location.search).get("access");
            history.replaceState(null, "", acc ? base + "?access=" + encodeURIComponent(acc) : base);
        }

        // 切集/连播时尝试自动播放（首次加载尊重浏览器 autoplay 策略，被阻止则静默，
        // 用户手动点播放即可）；play() 的 Promise reject 时忽略。
        video.play().catch(() => { /* 浏览器阻止自动播放时静默，等用户手动播放 */ });
    }

    /** 取 URL 里的共享 ID（切集/换源时重建视频流地址用）。 */
    function currentShareId() {
        return window.location.pathname.match(/^\/share\/video\/([^/]+)/)[1];
    }
})();
