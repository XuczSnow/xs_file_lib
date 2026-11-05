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
    let desiredFmt = "auto";          // 当前选集要播放的"格式分片"（auto = 浏览器 <source> 自选）
    const video = document.getElementById("videoPlayer");

    // 同片源各"格式分片"的 <source> MIME（按扩展名）
    const MIME = { mp4: "video/mp4", m4v: "video/mp4", webm: "video/webm", ogg: "video/ogg" };
    // 转码脚本输出的格式后缀（xxx_hevc.mp4 / xxx_vp9.webm …）→ 友好标签
    const FMT_LABELS = { h264: "H.264", hevc: "HEVC", av1: "AV1", vp9: "VP9" };
    const FMT_SUFFIX_RE = /_(h264|hevc|av1|vp9)$/i;
    // "自动"模式下浏览器按序自选 <source> 的优先级：越宽兼容/越常用越靠前
    const FMT_PRIORITY = { h264: 1, hevc: 2, mp4: 3, m4v: 4, vp9: 5, av1: 6, webm: 7, ogg: 8 };

    /** 由 rel（相对共享根路径）识别该分片的格式 key + 标签。
     *  优先认转码输出的格式后缀（_hevc/_vp9…），否则按扩展名兜底（mp4→MP4 / webm→WebM）。 */
    function sourceFmt(rel) {
        const noExt = rel.replace(/\.[^.]+$/, "");
        const m = noExt.match(FMT_SUFFIX_RE);
        if (m) {
            const k = m[1].toLowerCase();
            return { key: k, label: FMT_LABELS[k] };
        }
        const ext = (rel.match(/\.([a-z0-9]+)$/i) || [, ""])[1].toLowerCase();
        const key = MIME[ext] ? ext : "mp4";
        return { key, label: key === "mp4" ? "MP4" : (key === "webm" ? "WebM" : key.toUpperCase()) };
    }
    /** 片源分组键：去掉扩展名 + 转码格式后缀（小写），使 4 个格式分片归到同一选集。 */
    function sourceGroupKey(rel) {
        return rel.replace(/\.[^.]+$/, "").replace(FMT_SUFFIX_RE, "").toLowerCase();
    }

    // ---------- 初始化 ----------

    window.onload = async function () {
        // 解析 URL：/share/video/{shareId}/{filename}，access 令牌透传
        const m = window.location.pathname.match(/^\/share\/video\/([^/]+)(\/.*)?$/);
        const backLink = document.getElementById("backToShare");
        const params = new URLSearchParams(window.location.search);
        const accessParam = params.get("access");
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

        // 视频按"片源"合并：同目录 + 同 stem（clip_h264.mp4 + clip_hevc.mp4 + clip_av1.webm +
        // clip_vp9.webm，或旧的 clip.mp4 + clip.webm）合成一条选集；
        // 每个分片带 fmt 标签（转码格式后缀优先，扩展名兜底）；
        // 播放时默认按"自动"顺序挂多个 <source> 让浏览器自选可解码分片，
        // 或在胶囊菜单手动指定某一格式分片（desiredFmt）。
        const rawVideos = (data.files || [])
            .filter((f) => X.VIDEO_EXTS.includes(f.extension || X.getExt(f.name)))
            .map((f) => {
                const rel = f.path.startsWith(data.name + "/")
                    ? f.path.slice(data.name.length + 1)
                    : f.name;
                return {
                    name: f.name,
                    path: f.path,                       // 相对 UPLOAD_DIR 的全路径（缩略图/下载用）
                    rel,                                // 相对共享根（视频流接口用）
                    ext: (f.extension || X.getExt(f.name)).toLowerCase(),
                    size: f.size || 0,
                };
            });
        const epGroups = new Map();
        rawVideos.forEach((v) => {
            const key = sourceGroupKey(v.rel);   // 去扩展名 + 去格式后缀：4 个分片归同组
            const g = epGroups.get(key);
            if (!g) {
                epGroups.set(key, { key, name: "", path: "", rel: "", size: 0, ext: "",
                                   sources: [Object.assign(v, { fmt: sourceFmt(v.rel) })] });
            } else {
                g.sources.push(Object.assign(v, { fmt: sourceFmt(v.rel) }));
            }
        });
        episodes = Array.from(epGroups.values()).map((g) => {
            // 分片按"自动播放优先级"排序（宽兼容在前）：同组内的 h264→hevc→mp4→vp9→av1
            g.sources.sort((a, b) =>
                (FMT_PRIORITY[a.fmt.key] || 9) - (FMT_PRIORITY[b.fmt.key] || 9));
            const primary = g.sources[0];            // 优先级最高的分片作为"代表"
            g.name = primary.name
                .replace(/\.[^.]+$/, "")            // 先去掉扩展名
                .replace(FMT_SUFFIX_RE, "");        // 再去转码格式后缀（_hevc/_vp9…）
            g.rel = primary.rel;
            g.path = primary.path;
            g.size = primary.size;
            g.ext = primary.ext;
            return g;
        });

        // 定位当前集（按 URL 的 filename 匹配，URL 段可能是相对共享根的路径）
        // 关键：location.pathname 是【编码形态】（share.js 用 encodeURIComponent 拼的，
        // 中文/空格/% 均为 %xx），必须先解码才能与选集的原始路径比对；
        // 否则全部失配 → findIndex=-1 → Math.max(0,-1)=0，永远回落到第一集（修复 bug）
        let urlFile = (m[2] || "").replace(/^\/+/, "");
        try { urlFile = decodeURIComponent(urlFile); } catch (e) { /* 已是原始形态则保持 */ }
        // 按"片源"定位：URL 命中的可能是该组任一分片（clip.mp4 或 clip.webm），都归到同一选集
        currentEp = Math.max(0, episodes.findIndex(
            (e) => e.sources.some(
                (s) => s.rel === urlFile || s.path === urlFile || s.name === urlFile
            )
        ));

        // 格式深链：?fmt=hevc 等（与 FMT_LABELS 的 key 一致时生效，否则按"自动"）
        const urlFmt = (params.get("fmt") || "").toLowerCase();
        if (urlFmt && urlFmt !== "auto" && (FMT_LABELS[urlFmt] || ["mp4", "m4v", "webm", "ogg"].includes(urlFmt))) {
            desiredFmt = urlFmt;
        }

        renderEpisodes();
        renderFmtGroup();
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
        // 文件夹 → share-folder-download；密码共享带 access 令牌）；
        // 单文件型共享时跟随"当前选中的格式分片"下载（文件夹型端点按分片 rel 下载）
        const dlBtn = document.getElementById("downloadBtn");
        if (dlBtn) {
            dlBtn.addEventListener("click", () => {
                const ep = episodes[currentEp];
                if (!ep) return;
                const src = activeSource(ep);
                const acc = new URLSearchParams(window.location.search).get("access");
                const qs = acc ? "?access=" + encodeURIComponent(acc) : "";
                if (shareType === "file") {
                    window.location.href = "/api/share-download/" + encodeURIComponent(currentShareId()) + qs;
                } else {
                    window.location.href = "/api/share-folder-download/" + encodeURIComponent(currentShareId()) +
                        "/" + encodeURIComponent(src.rel) + qs;
                }
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

    // ---------- 格式胶囊组（视频下方菜单栏内） ----------

    /** 取某选集"当前应播放"的分片：desiredFmt=auto 时取浏览器可自检的优先级最高分片
     *  （实际挂多 <source> 让浏览器自选）；否则按 fmt.key 精确匹配，匹配不到回落到最高优先分片。 */
    function activeSource(ep) {
        if (!ep || !ep.sources.length) return null;
        if (desiredFmt === "auto") return ep.sources[0];
        const hit = ep.sources.find((s) => s.fmt.key === desiredFmt);
        return hit || ep.sources[0];
    }

    /** 渲染格式选择下拉框（自动 + 当前选集实际具备的各格式分片）。
     *  选集只有一个分片时隐藏整个下拉框（无可选项）。
     *  位置：注入到 #fmtSelect（share_video.html 胶囊菜单栏内、与功能按钮同一行）。 */
    function renderFmtGroup() {
        const host = document.getElementById("fmtSelect");
        if (!host) return;
        const sep = host.previousElementSibling;   // .pill-sep 与格式组联动显隐
        const ep = episodes[currentEp];
        const keys = new Set();
        if (ep) ep.sources.forEach((s) => keys.add(s.fmt.key));
        // 仅当该选集有 ≥2 个不同格式分片时才显示格式组（单个分片时 自动 与它等价，隐藏）
        if (keys.size < 2) {
            host.style.display = "none";
            if (sep) sep.style.display = "none";
            return;
        }
        host.style.display = "";
        if (sep) sep.style.display = "";
        // 自动 + 存在的格式（按"自动播放优先级"排序，与分片顺序一致）
        const opts = ["auto", ...Array.from(keys)];
        const fmtName = (k) =>
            k === "auto" ? "自动（浏览器自选）"
                : ((ep && ep.sources.find((s) => s.fmt.key === k))?.fmt.label || FMT_LABELS[k] || k.toUpperCase());
        // 当前 desiredFmt 若不在可选集合内（例如刚切到没有该格式的分集），回落"自动"
        const shown = desiredFmt !== "auto" && !opts.includes(desiredFmt) ? "auto" : desiredFmt;
        host.innerHTML = opts.map((k) =>
            `<option value="${esc(k)}"${k === shown ? " selected" : ""}>${fmtName(k)}</option>`
        ).join("");
        host.onchange = () => {
            desiredFmt = host.value;
            loadEpisode(currentEp, false, { syncFmt: true });
        };
    }
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
     * @param {boolean} updateHistory 切集导航（选集/上下集点击为 true，首次加载与自动连播为 false，
     *   避免污染后退栈）
     * @param {Object} [opts] syncFmt=true 表示格式 pill 触发的同集重载：保留当前文件名段，只同步/移除 ?fmt=
     */
    function loadEpisode(idx, updateHistory, opts = {}) {
        if (!episodes[idx]) return;
        currentEp = idx;
        const ep = episodes[idx];

        // 回落：新选集没有当前所选格式的分片（如"下一集"切过去）时，
        // 还原"自动"，避免残留失效选择（选集无此格式时 pill 组本就不会显示它）
        if (desiredFmt !== "auto" && !ep.sources.some((s) => s.fmt.key === desiredFmt)) {
            desiredFmt = "auto";
        }

        // 拼视频流地址（后端 Range + ETag + 缓存头，见 share_media.py share_video_stream）。
        // desiredFmt="auto"（默认）：同一"片源"的各分片（h264/hevc/av1/vp9 …）各生成一个
        // <source>，按可播优先级排序，浏览器自选第一个自己能解码的分片；
        // 指定格式（胶囊菜单点 H.264/HEVC/AV1/VP9）：只挂对应分片，浏览器强制走该编码。
        const accessParam = new URLSearchParams(window.location.search).get("access");
        const qs = accessParam ? "?access=" + encodeURIComponent(accessParam) : "";
        const streamUrl = (rel) =>
            "/api/video-stream/" + encodeURIComponent(currentShareId()) +
            "/" + encodeURIComponent(rel) + qs;
        const pickList = desiredFmt === "auto"
            ? ep.sources
            : ep.sources.filter((s) => s.fmt.key === desiredFmt);
        const sources = (pickList.length ? pickList : ep.sources).map((s) => ({
            src: streamUrl(s.rel),
            type: MIME[s.ext] || "video/mp4",
        }));
        // 用 <source> 子节点替换单 src（保留 controls 等原生属性，清掉旧源避免残留）
        while (video.firstChild) video.removeChild(video.firstChild);
        sources.forEach((s) => {
            const so = document.createElement("source");
            so.src = s.src;
            so.type = s.type;
            video.appendChild(so);
        });
        video.load();

        // 顶栏已移除：标题显示在浏览器标签页（document.title），含集数前缀 + 当前格式
        const fmtTag = ep.sources.length > 1
            ? " · " + (desiredFmt === "auto" ? "自动" : (FMT_LABELS[desiredFmt] || desiredFmt.toUpperCase()))
            : "";
        document.title =
            (episodes.length > 1 ? `${idx + 1} / ${episodes.length} · ` : "") + ep.name + fmtTag;

        // 菜单栏：上一集/下一集按钮随当前集启用/禁用
        document.getElementById("prevEp").disabled = idx <= 0;
        document.getElementById("nextEp").disabled = idx >= episodes.length - 1;

        // 高亮当前集 + 自动滚动到可见
        document.querySelectorAll("#epList .ep-item").forEach((el, i) => {
            el.classList.toggle("active", i === idx);
            if (i === idx) el.scrollIntoView({ block: "nearest" });
        });

        // 切到新一集时按新选集可用分片重建格式胶囊组
        renderFmtGroup();

        // 地址栏语义（history.replaceState，不堆叠历史）：
        //  - 切集导航（updateHistory）：归一化到该组"代表分片"（最高优先），并同步 ?fmt=
        //  - 格式 pill 同集重载（opts.syncFmt）：保持用户当前文件名段，只 写入/移除 ?fmt=
        //  - 首次加载 / 自动连播：不重写 URL（保持用户点开时的链接，浏览器可正常后退）
        if (updateHistory || opts.syncFmt) {
            const p = new URLSearchParams(window.location.search);
            const seg = opts.syncFmt
                ? window.location.pathname.split("/").pop()   // 保持当前链接文件名段
                : encodeURIComponent(ep.rel);                   // 切集归一化到代表分片
            const base = "/share/video/" + currentShareId() + "/" + seg;
            if (desiredFmt === "auto") p.delete("fmt"); else p.set("fmt", desiredFmt);
            const qsUrl = p.toString();
            history.replaceState(null, "", base + (qsUrl ? "?" + qsUrl : ""));
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
