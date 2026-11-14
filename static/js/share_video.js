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

    // fMP4/DASH（路线 B）：内置真实 dash.js（static/vendor/dash.js）。
    // 格式下拉框内"分段"选项 = 用 DASH fMP4 播放【原视频】（转码分片不参与分段）：
    // 设置 dash_auto_slice 开启且该原视频已切片时走分片播放（秒起播/拖动只拉小片）；
    // 未切片 / 开关关闭 / dash.js 不可用 / manifest 加载失败 → 回退渐进式，
    // 下拉框"分段"选项文案变为"分段 · 已回退"并提示原因（不设浮层徽章）。
    let dashPlayer = null;            // DASH 播放器实例（dash.js），渐进式为 null
    let dashInited = false;           // 当前 dashPlayer 是否已 initialize（v5.3 未初始化前 reset() 会抛异常）
    let dashErrHandler = null;        // player 的 ERROR 监听句柄（回退时清理）
    const dashCache = {};             // "share|rel|access" → 该分片 DASH 是否可用(true/false)
    const tcCache = {};               // "share|path" → 该原视频在 cache/video 的可用转码格式（端点 A 结果缓存）

    /** dash.js player 的 ERROR 事件名（v5.3: dashjs.MediaPlayer.events.ERROR === "error"）。 */
    function dashErrorEvent() {
        try {
            const ev = window.dashjs && window.dashjs.MediaPlayer && window.dashjs.MediaPlayer.events;
            if (ev && typeof ev.ERROR === "string" && ev.ERROR) return ev.ERROR;
        } catch (e) { /* 取不到用 v5 字面量兜底 */ }
        return "error";
    }

    /** “分段”选项状态：播放方式由格式下拉框的选中项表达（不设浮层徽章）。
     *  tried=false（本集尚未尝试 DASH）→ 乐观文案“分段（DASH）”；
     *  尝试成功 ok=true → “分段（DASH）”；回退 ok=false → “分段 · 已回退” + title 给原因。 */
    let dashStatus = { tried: false, ok: false, reason: "" };
    let dashAutoSliceOn = null;          // 设置项 dash_auto_slice（onload 拉取，决定回退原因文案）

    /** 分段选项是否处于"可用文案"（未尝试过 或 上次成功）。 */
    function dashOptionGood() { return !dashStatus.tried || dashStatus.ok; }

    /** DASH 成功/回退后刷新"自动"选项 title、左上角徽章与标签页标题（三处同一来源）。 */
    function refreshDashOption() {
        renderFmtGroup();   // 重建下拉（自动项 title 随 dashStatus 变化；选中态按 desiredFmt 保持一致）
        updateFmtBadge();
        updateTitle();     // 异步回退/成功后标签页标题也要跟着变（避免停在过期 "· DASH"）
        const good = dashOptionGood();
        console.debug("[share_video] 播放模式:", good ? "DASH 分段" : "渐进式回退", dashStatus.reason || "");
    }

    /** 左上角格式徽章（#fmtBadge，.video-content 内 absolute 定位）：显示当前实际播放格式。
     *  自动 + DASH 成功 → "DASH · <格式>"；自动 + DASH 已回退 → "<格式> · DASH回退"；
     *  自动未走 DASH → "自动 · <格式>"；手动指定格式 → 该格式标签。 */
    function updateFmtBadge() {
        const badge = document.getElementById("fmtBadge");
        if (!badge) return;
        const ep = episodes[currentEp];
        const act = ep ? activeSource(ep) : null;
        const label = act ? act.fmt.label : "";
        const autoMode = desiredFmt === "auto" || desiredFmt === DASH_FMT;
        let text = "";
        if (autoMode && dashStatus.ok) text = "DASH" + (label ? " · " + label : "");
        else if (autoMode && dashStatus.tried && !dashStatus.ok) text = label ? label + " · DASH回退" : "渐进式";
        else if (autoMode) text = label ? "自动 · " + label : "自动";
        else text = label;
        badge.textContent = text;
        badge.hidden = !text;
    }

    /** 浏览器标签页标题（单一来源）：集数前缀 + 集名 + 当前播放方式。
     *  播放方式随 dashStatus 变化（DASH 成功/回退），故 loadEpisode 与
     *  refreshDashOption（异步回退路径）都调用它，避免标题停在过期状态。 */
    function updateTitle() {
        const ep = episodes[currentEp];
        if (!ep) return;
        const modeTag = (desiredFmt === "auto" || desiredFmt === DASH_FMT)
            ? (dashStatus.ok ? "DASH" : "自动")
            : (FMT_LABELS[desiredFmt] || desiredFmt.toUpperCase());
        const fmtTag = ep.sources.length > 1 ? " · " + modeTag : "";
        document.title =
            (episodes.length > 1 ? `${currentEp + 1} / ${episodes.length} · ` : "") + ep.name + fmtTag;
    }

    /** manifest 不可用（404）时的原因文案：区分“开关未开”与“开关开了但未切/无 ffmpeg”。 */
    function dash404Reason() {
        if (dashAutoSliceOn) return "该原视频未切片或无 ffmpeg，已回退渐进式";
        if (dashAutoSliceOn === false) return "自动切片未开启（设置页“相册设置”可开启），已回退渐进式";
        return "该原视频暂无可用 DASH 分片，已回退渐进式";
    }

    // 同片源各"格式分片"的 <source> MIME（按扩展名）
    const MIME = { mp4: "video/mp4", m4v: "video/mp4", webm: "video/webm", ogg: "video/ogg" };
    // 转码脚本输出的格式后缀（xxx_hevc.mp4 / xxx_vp9.webm …）→ 友好标签
    const FMT_LABELS = { h264: "H.264", hevc: "HEVC", av1: "AV1", vp9: "VP9" };
    const FMT_SUFFIX_RE = /_(h264|hevc|av1|vp9)$/i;
    // “分段”选项值（并入格式下拉）：用 DASH fMP4 播放【原视频】；转码分片不参与分段
    const DASH_FMT = "dash";

    /** 判断某分片是否为"原视频"（无转码格式后缀 → fmt.key 为扩展名、不在 FMT_LABELS 内）。
     *  DASH 分段只针对原视频；转码后的分片（xxx_h264 / xxx_hevc …）永不分段。 */
    function isOriginalSource(s) {
        return !!s && !FMT_LABELS[s.fmt.key];
    }

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

    // 转码格式 key → 扩展名（供前端识别/兜底；与后端 TC_FORMATS、transcode.py FORMATS 同口径）
    const FMT_EXT = { h264: "mp4", hevc: "mp4", av1: "webm", vp9: "webm" };

    /** 访问令牌（原始值），供 DASH/渐进式播放路径拼接 URL 用。 */
    function accessToken() {
        return new URLSearchParams(window.location.search).get("access") || "";
    }

    /** 访问令牌的 query 片段（"?access=xxx" 或 ""），供转码端点 A/B 拼接。 */
    function accessQs() {
        const a = accessToken();
        return a ? "?access=" + encodeURIComponent(a) : "";
    }

    /** 查某原视频（ep）在 cache/video 下可用的转码格式（端点 A），结果 + 在途 Promise 缓存；
     *  返回 Promise<[{fmt, label, path}]>（path 相对 cache/video，端点 B 用 fmt 定位）。
     *  ep._tcPending 存"在途 Promise"，ep.transcodes 存"结果数组"，避免并发重复请求。 */
    function ensureTranscodes(ep) {
        if (ep.transcodes) return Promise.resolve(ep.transcodes);
        const key = currentShareId() + "|" + ep.path;
        if (tcCache[key]) { ep.transcodes = tcCache[key]; return Promise.resolve(ep.transcodes); }
        if (ep._tcPending) return ep._tcPending;
        ep._tcPending = X.getJSON(
            "/api/video-transcodes/" + encodeURIComponent(currentShareId()) +
            "/" + encodeURIComponent(ep.path) + accessQs())
            .then((data) => {
                const arr = (data && data.success && Array.isArray(data.available))
                    ? data.available : [];
                tcCache[key] = arr;
                ep.transcodes = arr;
                return arr;
            })
            .catch(() => {
                // F1 守护：请求失败（404/网络/mixed content）也收敛为 []（ep.transcodes 为真值，
                // 后续调用直接短路不再重发；写 tcCache 让切回同一集同样短路，防请求风暴）
                tcCache[key] = [];
                ep.transcodes = [];
                return [];
            })
            .finally(() => { ep._tcPending = null; });
        return ep._tcPending;
    }

    /** 某转码格式的文件流 URL（端点 B）：/api/video-stream-fmt/{id}/{原视频path}/{fmt}。
     *  注意中间段是【原视频】路径（鉴权定位用），末段 fmt 才是要取哪个格式产物。 */
    function fmtStreamUrl(ep, fmt, dl) {
        const parts = [accessQs()];
        if (dl) parts.push("dl=1");
        const qs = parts.filter(Boolean).join("&");
        return X.resolveUrl("/api/video-stream-fmt/" + encodeURIComponent(currentShareId()) +
            "/" + encodeURIComponent(ep.path) + "/" + encodeURIComponent(fmt) +
            (qs ? "?" + qs : ""));
    }

    // ---------- 初始化 ----------

    window.onload = async function () {
        await X.loadMediaBase();   // IPv6 媒体改道：生成视频流/下载 URL 前探测可达性

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

        // 共享目录树只含【原始视频】（转码产物已迁到 cache/video，不再进 uploads），
        // 故每个视频文件 = 一条独立选集，ep.sources 恒为单个原视频 source。
        // "格式"下拉的选项来自端点 A（/api/video-transcodes，查 cache/video 有哪些转码），
        // 不再靠"同片源后缀合并"——旧的"同目录 _h264/_hevc/_av1/_vp9 并成一条选集"逻辑已废弃。
        const rawVideos = (data.files || [])
            .filter((f) => X.VIDEO_EXTS.includes(f.extension || X.getExt(f.name)))
            .map((f) => {
                const rel = f.path.startsWith(data.name + "/")
                    ? f.path.slice(data.name.length + 1)
                    : f.name;
                return {
                    name: f.name,
                    path: f.path,                       // 相对 UPLOAD_DIR 的全路径（缩略图/下载/端点 A、B 用）
                    rel,                                // 相对共享根（渐进式 /api/video-stream 用）
                    ext: (f.extension || X.getExt(f.name)).toLowerCase(),
                    size: f.size || 0,
                };
            });
        episodes = rawVideos.map((v) => ({
            key: v.rel,
            name: v.name.replace(/\.[^.]+$/, ""),       // 显示名（去扩展名）
            path: v.path, rel: v.rel, ext: v.ext, size: v.size,
            sources: [Object.assign({}, v, { fmt: sourceFmt(v.rel) })],  // 原视频
            transcodes: null, _tcPending: null,          // null=未查端点A；[]/数组=已查结果
        }));

        // 定位当前集（按 URL 的 filename 匹配；URL 段是编码形态，先解码再与原路径比对，
        // 否则中文/空格文件名全部失配 → 永远回落到第一集）。现在每视频一集，直接按 rel 匹配。
        let urlFile = (m[2] || "").replace(/^\/+/, "");
        try { urlFile = decodeURIComponent(urlFile); } catch (e) { /* 已是原始形态则保持 */ }
        currentEp = Math.max(0, episodes.findIndex(
            (e) => e.rel === urlFile || e.path === urlFile || e.name === urlFile
        ));

        // 格式深链：?fmt=auto / ?fmt=h264 / ?fmt=hevc / ?fmt=av1 / ?fmt=vp9；
        // ?fmt=dash 为旧"分段"深链兼容，归一化为 auto（现在"自动"已默认优先 DASH）。
        // 非法值（如旧的 mp4/webm/…）保持 auto（loadEpisode 会按该视频实际有无转码再归一）
        const urlFmt = (params.get("fmt") || "").toLowerCase();
        if (urlFmt === DASH_FMT) desiredFmt = "auto";
        else if (urlFmt && urlFmt !== "auto" &&
            (["h264", "hevc", "av1", "vp9"].includes(urlFmt))) {
            desiredFmt = urlFmt;
        }

        // 拉取"自动切片"开关状态（决定 DASH 回退时的原因文案）；失败不影响播放
        X.getJSON("/api/settings")
            .then((s) => { dashAutoSliceOn = !!s.dash_auto_slice; })
            .catch(() => { dashAutoSliceOn = null; });

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

        // 菜单栏：下载当前视频。
        //  - 手动选了【转码格式】→ 下载 cache/video 的转码文件（端点 B ?dl=1，带 attachment）；
        //  - 否则（自动/DASH 回退等，片源=原视频）按共享类型选下载端点：
        //      单文件 → /api/share-download/{id}；文件夹 → /api/share-folder-download/{id}/{rel}
        const dlBtn = document.getElementById("downloadBtn");
        if (dlBtn) {
            dlBtn.addEventListener("click", () => {
                const ep = episodes[currentEp];
                if (!ep) return;
                const src = activeSource(ep);
                const acc = new URLSearchParams(window.location.search).get("access");
                const qs = acc ? "?access=" + encodeURIComponent(acc) : "";
                if (src && src.isTranscode) {
                    // 转码文件：端点 B ?dl=1（Content-Disposition: attachment）
                    window.location.href = fmtStreamUrl(ep, desiredFmt, true);
                    return;
                }
                if (shareType === "file") {
                    window.location.href = X.resolveUrl("/api/share-download/" + encodeURIComponent(currentShareId()) + qs);
                } else {
                    window.location.href = X.resolveUrl("/api/share-folder-download/" + encodeURIComponent(currentShareId()) +
                        "/" + encodeURIComponent(src.rel) + qs);
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

    /** 取某选集"当前应播放/下载"的片源：
     *  - auto/dash：都是【原视频】（DASH 分段播原视频，回退也只挂原视频，片源一致）
     *  - 手动选转码格式：若该格式在 ep.transcodes（端点 A 查得）里 → 返回"转码片源"
     *    （isTranscode=true，播放/下载走 /api/video-stream-fmt），否则回原视频 */
    function activeSource(ep) {
        if (!ep || !ep.sources.length) return null;
        if (desiredFmt === "auto" || desiredFmt === DASH_FMT) {
            return ep.sources[0];   // 自动/DASH 均为原视频
        }
        const tc = (ep.transcodes || []).find((t) => t.fmt === desiredFmt);
        if (tc) return { fmt: { key: tc.fmt, label: tc.label }, isTranscode: true,
                         ext: FMT_EXT[tc.fmt] || "mp4" };
        return ep.sources[0];
    }

    /** 渲染格式选择下拉框（选项 = "自动" + 该视频在 cache/video 下【实际存在】的转码格式）：
     *  - "自动" = 优先 DASH fMP4 分段播放【原视频】，失败自动回退渐进式（原视频）；
     *  - 各转码格式（H.264/HEVC/AV1/VP9）来自端点 A /api/video-transcodes（查 cache/video），
     *    只有真实存在才列；选中某格式 → 播放/下载走端点 B /api/video-stream-fmt。
     *  F2：下拉【始终显示】（无转码产物时至少保留"自动"一项），不再整块隐藏——
     *    避免从"有格式"的集切到"无格式"的集时下拉突然消失、用户误以为功能坏了。
     *  ep.transcodes 尚未查询（null）时先异步 ensure 再重渲染。 */
    function renderFmtGroup() {
        const host = document.getElementById("fmtSelect");
        if (!host) return;
        const ep = episodes[currentEp];
        if (!ep) return;
        if (ep.transcodes === null) {
            ensureTranscodes(ep).then(() => renderFmtGroup());   // 首次进入、尚未查询
            return;
        }
        const sep = host.previousElementSibling;   // .pill-sep 与格式组联动显隐
        const tcs = ep.transcodes || [];           // [{fmt,label,path}]
        // F2：始终显示下拉（至少含"自动"一项），不再因"无转码产物"而整个隐藏——
        // 避免用户从"有格式"的集切到"无格式"的集时下拉突然消失、误以为功能坏了。
        // 有转码格式时列 auto + 各真实存在的格式；无时仅 auto（选中即原视频/DASH 分段播放）。
        const opts = ["auto"].concat(tcs.map((t) => t.fmt));
        host.style.display = "";
        if (sep) sep.style.display = "";
        const labelOf = (k) =>
            k === "auto" ? "自动" : (FMT_LABELS[k] || tcs.find((t) => t.fmt === k)?.label || k.toUpperCase());
        const optTitle = (k) => k === "auto"
            ? (dashStatus.ok ? "DASH 分段播放（原视频）"
                : (dashStatus.tried ? (dashStatus.reason || "已回退渐进式")
                : "优先 DASH 分段，失败回退渐进式"))
            : "播放 cache/video 的转码版本";
        // desiredFmt 归一化：历史 ?fmt=dash 深链映射 auto；其余不在可选集合回落 auto
        let shown = desiredFmt;
        if (shown === DASH_FMT) shown = "auto";
        else if (shown !== "auto" && !opts.includes(shown)) shown = "auto";
        if (shown !== desiredFmt) desiredFmt = shown;
        host.innerHTML = opts.map((k) =>
            `<option value="${esc(k)}"${k === shown ? " selected" : ""} ${optTitle(k) ? 'title="' + esc(optTitle(k)) + '"' : ""}>${labelOf(k)}</option>`
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
                    <img src="${X.resolveUrl("/api/video-thumbnail/" + encodeURIComponent(ep.path))}"
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
    // ---- fMP4/DASH（路线 B）：优先分片播放，缺失/未切片自动回退渐进式 ----
    // shipped 的 static/vendor/dash.js 是占位 stub（window.dashjs 未定义）→
    // 以下探测恒失败 → 恒走渐进式，零回归；部署真实 dash.js 后自动启用。

    function dashjsReady() {
        return !!(window.dashjs &&
            ((window.dashjs.MediaPlayer && typeof window.dashjs.MediaPlayer === "function") ||
             (window.dashjs.create && typeof window.dashjs.create === "function")));
    }

    function makeDashPlayer() {
        if (!dashjsReady()) return null;
        try {
            // v5: dashjs.MediaPlayer() 是工厂（返回 {create,...}）；v4 部分版本
            // MediaPlayer 直接是实例。create() 存在则用它取实例，否则直接用返回值。
            const mp = window.dashjs.MediaPlayer
                ? window.dashjs.MediaPlayer()
                : window.dashjs.create();
            return (mp && typeof mp.create === "function") ? mp.create() : mp;
        } catch (e) { return null; }
    }

    /** 该分片 DASH manifest 地址（后端已把 MPD 的 BaseURL 改写为分片端点前缀）。 */
    function dashManifestUrl(rel, accessParam) {
        const qs = accessParam ? "?access=" + encodeURIComponent(accessParam) : "";
        return X.resolveUrl("/api/video-dash/" +
            encodeURIComponent(currentShareId()) + "/" + encodeURIComponent(rel) + qs);
    }

    /** 清理 DASH 播放器（reset 只在已 initialize 后安全 + destroy 彻底清场）。
     *  切回原生渐进式 / 转码流前调用，避免 dash.js 会话残留踩源。 */
    function teardownDashPlayer() {
        if (dashPlayer) {
            if (dashErrHandler) {
                try { dashPlayer.off(dashErrorEvent(), dashErrHandler); } catch (e) {}
                dashErrHandler = null;
            }
            try { if (dashInited) dashPlayer.reset(); } catch (e) { /* 清理失败不影响回退 */ }
            try { dashPlayer.destroy(); } catch (e) { /* 未初始化时 destroy 亦可能抛异常 */ }
            dashPlayer = null;
        }
        dashInited = false;
    }

    /** 播放某原视频的【转码格式产物】（端点 B /api/video-stream-fmt/{id}/{path}/{fmt}）。
     *  挂单个 <source>（格式是用户明确选的，不再多 source 连挂让浏览器自选）。
     *  先 teardownDashPlayer 清掉 DASH 会话，再挂转码流。 */
    function playTranscode(ep, fmt, accessParam) {
        teardownDashPlayer();
        const qs = accessParam ? "?access=" + encodeURIComponent(accessParam) : "";
        while (video.firstChild) video.removeChild(video.firstChild);
        const so = document.createElement("source");
        so.src = fmtStreamUrl(ep, fmt, false);
        so.type = MIME[(FMT_EXT[fmt] || "mp4")] || "video/mp4";
        video.appendChild(so);
        video.load();
    }

    /** 原生 <source> 渐进式播放（无 DASH / DASH 回退时）。
     *  @param {boolean} [onlyOriginal] 为 true 时只挂"原视频"这一个 source
     *   （DASH 回退场景，保持"分段片源=原视频"一致）；否则挂全部原视频 source。 */
    function useProgressive(ep, accessParam, onlyOriginal) {
        // 切回渐进式前清理 DASH 播放器（见 teardownDashPlayer）
        teardownDashPlayer();
        const qs = accessParam ? "?access=" + encodeURIComponent(accessParam) : "";
        const streamUrl = (rel) =>
            X.resolveUrl("/api/video-stream/" + encodeURIComponent(currentShareId()) +
            "/" + encodeURIComponent(rel) + qs);
        // 现在每选集只有一个原视频 source；onlyOriginal 语义保留（DASH 回退只挂原视频）
        const pickList = onlyOriginal
            ? ep.sources.filter(isOriginalSource)
            : ep.sources;
        const useList = (pickList.length ? pickList : ep.sources);
        while (video.firstChild) video.removeChild(video.firstChild);
        useList.forEach((s) => {
            const so = document.createElement("source");
            so.src = streamUrl(s.rel);
            so.type = MIME[s.ext] || "video/mp4";
            video.appendChild(so);
        });
        video.load();
    }

    /** 尝试用 dash.js 播放【原视频】（fMP4）；不可用返回 false（调用方回退渐进式）。 */
    function tryPlayDash(ep, src, accessParam) {
        if (!src) return false;
        dashStatus = { tried: true, ok: false, reason: "" };   // 本集开始尝试 DASH
        if (!dashjsReady()) {
            dashStatus.reason = "dash.js 不可用，已回退渐进式";
            return false;   // dash.js 未加载 → 渐进式
        }
        const key = currentShareId() + "|" + src.rel + "|" + (accessParam || "");
        if (dashCache[key] === false) {   // 本会话已探测失败 → 快速回退（原因已知）
            dashStatus.reason = dash404Reason();
            return false;
        }
        if (!dashPlayer) dashPlayer = makeDashPlayer();
        if (!dashPlayer) {
            dashStatus.reason = "dash.js 初始化失败，已回退渐进式";
            return false;
        }

        let settled = false;
        const epIdx = currentEp;   // 记录发起 DASH 时的集号：异步回退前校验，避免切集后旧闭包踩空
        const fallback = (reason) => {
            if (settled) return;
            settled = true;
            if (currentEp !== epIdx) return;   // 用户已切到别的集：旧闭包不回退（新集有自己的流程）
            dashCache[key] = false;
            dashStatus.ok = false;   // initialize 成功后曾乐观置 true，回退必须回写 false，
            dashStatus.reason = reason || dash404Reason();   // 否则徽章/标题/下拉 title 三处误报 DASH 成功
            useProgressive(ep, accessParam, true);   // 回退只挂原视频（保持分段片源一致）
            refreshDashOption();
        };

        // v5.3：reset() 只对"已 initialize 过"的播放器安全，裸调会抛异常中断 loadEpisode
        // → 加守护 + try/catch（上一版因此连渐进式回退都没执行）。
        if (dashInited) {
            try { dashPlayer.reset(); } catch (e) { dashInited = false; }
        }

        try {
            const ret = dashPlayer.initialize(video, dashManifestUrl(src.rel, accessParam), false);
            // dash.js v5 的 initialize 为同步调用；manifest 加载失败（404/超时/解析错误）
            // 经 player ERROR 事件异步暴露 → 下面 fallback；同步异常同样回退
            dashInited = true;
            dashCache[key] = true;
            dashStatus.ok = true;
            refreshDashOption();
        } catch (e) {
            console.warn("[share_video] DASH initialize 抛异常，回退渐进式:", e);
            fallback("DASH 初始化异常，已回退渐进式");
            return false;
        }

        // 兜底 1：player 的 ERROR 事件（manifest 404 / 分片加载失败等 dash.js 内部错误）。
        // 切集复用同一 player 实例时先摘掉旧 handler，避免旧集闭包重复触发回退
        if (dashErrHandler) {
            try { dashPlayer.off(dashErrorEvent(), dashErrHandler); } catch (e) {}
            dashErrHandler = null;
        }
        const errEv = dashErrorEvent();
        dashErrHandler = () => {
            if (settled) return;
            // 已开始正常播放（有数据缓冲）则视为常规瞬时错误，不回退，
            // 避免丢弃可用的 DASH 会话（manifest 404 类错误此时视频尚无数据 → 回退）
            if (video.readyState >= 2 && !video.error) return;
            fallback();
        };
        try { dashPlayer.on(errEv, dashErrHandler); } catch (e) { dashErrHandler = null; }
        // 兜底 2：原生 video 的 error + 4s 超时（到点仍加载失败才回退，避免误判）
        const onErr = () => fallback();
        video.addEventListener("error", onErr, { once: true });
        const t = setTimeout(() => {
            if (!settled && video.error) fallback();
        }, 4000);
        // 成功起播后清掉一次性监听（video error 为 once；超时 timer 在 settled 后变空操作）
        video.addEventListener("playing", () => { clearTimeout(t); }, { once: true });
        return true;
    }

    /** 取"分段"片源：即该选集的【原视频】（现在每选集只有一个 source）。
     *  转码产物（cache/video）永不参与 DASH 分段。无原视频返回 null。 */
    function dashSourceOf(ep) {
        if (!ep || !ep.sources.length) return null;
        return ep.sources[0];
    }

    function loadEpisode(idx, updateHistory, opts = {}) {
        if (!episodes[idx]) return;
        currentEp = idx;
        const ep = episodes[idx];
        const originalOfEp = dashSourceOf(ep);   // 该选集的可分段原视频（null = 无原视频）

        // 切集重置 DASH 状态（上一集的成败不适用于新集）
        dashStatus = { tried: false, ok: false, reason: "" };
        // 历史 ?fmt=dash 深链残留值归一化为 auto（renderFmtGroup 也会再归一，双保险）
        if (desiredFmt === DASH_FMT) desiredFmt = "auto";

        const accessParam = accessToken();
        // 播放决策：
        //  - 自动：优先 DASH 分段播放【原视频】，失败回退渐进式（原视频）；
        //  - 手动选转码格式：确保该格式在 cache/video 存在 → 挂转码流（端点 B）；
        //    该视频没有此格式 → 回落"自动"（原视频渐进式）。转码格式不进 DASH 分段。
        if (desiredFmt === "auto" && originalOfEp) {
            if (!tryPlayDash(ep, originalOfEp, accessParam)) {
                useProgressive(ep, accessParam, true);   // 只挂原视频
            }
            updateFmtBadge();
            updateTitle();
        } else {
            const doTranscode = (tcs) => {
                if (currentEp !== idx) return;           // 用户已切走，旧闭包不挂
                const hit = tcs.find((t) => t.fmt === desiredFmt);
                if (hit) {
                    playTranscode(ep, desiredFmt, accessParam);
                } else {
                    desiredFmt = "auto";                  // 该视频无此转码格式 → 回落自动（原视频）
                    useProgressive(ep, accessParam, false);
                }
                updateFmtBadge();
                updateTitle();
            };
            if (ep.transcodes) doTranscode(ep.transcodes);   // 已查过（切回/同集换格式）
            else ensureTranscodes(ep).then(doTranscode);     // 首次/深链：异步查 cache/video
        }

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
            // syncFmt：保持 share_id 之后的【全部】段（含子目录，编码形态），
            // 原 split("/").pop() 只取末段，子目录视频切格式会丢路径 → 刷新 404
            const idMatch = window.location.pathname.match(/^\/share\/video\/[^/]+\/(.*)$/);
            const seg = opts.syncFmt
                ? (idMatch ? idMatch[1] : encodeURIComponent(ep.rel))
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
