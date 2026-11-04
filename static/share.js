/**
 * 共享页（share.html）业务逻辑。
 *
 * 依赖 common.js（XSLib）提供的请求/轮询/转义工具；
 * 图片灯箱预览由 preview.js 提供 previewFile。
 *
 * 页面流程：
 *   1. onload 拉取 /api/share-info/{id}，展示共享文件名/到期/密码状态；
 *   2. 用户点"预览文件"触发 verifyShare：先 /api/share-check 校验密码，
 *      再 /api/share-content 拉内容，按 share_mode 渲染：
 *        - normal：普通文件夹文件列表（renderFolder）
 *        - album ：相册/视频网格（renderAlbum）
 *   3. 打包下载走 /api/share-folder-zip-task + 轮询 + 下载。
 *
 * 交互模型：动态列表用 data-* 属性 + 事件委托（#fileCard / #albumCard
 * 各挂一个 click 监听器），避免 inline onclick 中文件名含引号导致
 * JS 断裂、以及文件名注入 innerHTML（XSS）。
 */
(function () {
    "use strict";

    const X = window.XSLib;
    const esc = X.escapeHtml;

    /* 模块级状态：当前共享 ID、可预览图片列表（灯箱切换用）、共享密码访问令牌 */
    let currentShareId = "";
    let imageFiles = [];
    let currentAccess = "";   // /api/share-check 校验密码后签发的短期访问令牌

    // 相册筛选状态（扁平网格 + 组合筛选：类型 × 目录）
    let currentAlbumType = "all";   // "all" | "photos" | "videos"
    let currentAlbumDir = "all";    // "all" 或某目录相对路径（含其子目录）

    // ---------- 初始化 ----------

    // 事件委托：fileCard / albumCard 内所有交互
    // （脚本在 body 末尾执行，两个容器此时已存在于 DOM）
    document.getElementById("fileCard").addEventListener("click", onFileCardClick);
    document.getElementById("albumCard").addEventListener("click", onAlbumCardClick);

    /**
     * 页面加载：从 URL 末段取共享 ID，拉取共享信息并填充首屏卡片。
     * 共享不存在/已过期时给出对应提示并禁用预览按钮。
     */
    window.onload = async function () {
        await X.loadSiteSetting();

        // 共享页 URL 形如 /share/{share_id}，末段即 ID
        currentShareId = window.location.pathname.split("/").pop();
        const result = await X.getJSON("/api/share-info/" + currentShareId);

        if (!result.success) {
            document.getElementById("shareFileName").innerText = "共享链接不存在";
            return;
        }

        document.getElementById("shareFileName").innerText = result.name;
        document.getElementById("shareExpire").innerText = "⏰ 有效期至：" + result.expire;

        const isFolder = result.type === "folder";
        document.querySelector(".file-icon").innerText = isFolder ? "📁" : "📄";
        document.querySelector(".text-center").innerText = isFolder ? "文件夹共享" : "文件共享";
        document.getElementById("sharePasswordStatus").innerText =
            result.has_password ? "🔒 已启用提取码" : "🔓 无需密码";

        // 无密码则隐藏密码输入框
        if (!result.has_password) {
            document.getElementById("sharePassword").style.display = "none";
        }

        // 已过期则提示并禁用预览
        if (result.expired) {
            document.getElementById("errorText").innerText = "共享链接已过期";
            document.querySelector(".download-btn").disabled = true;
        }

        // 免验证路径：URL 带 ?access=（从视频播放页"返回共享"跳回时携带令牌），
        // 直接渲染内容，不再显示密码卡片要求重新验证；令牌失效则回退到密码卡
        const urlAccess = new URLSearchParams(window.location.search).get("access");
        if (urlAccess && !result.expired) {
            currentAccess = urlAccess;
            document.getElementById("shareCard").style.display = "none";
            const ok = await loadContentAndRender();
            if (!ok) {
                document.getElementById("shareCard").style.display = "";
            }
        }
    };

    // ---------- 验证并渲染 ----------

    /**
     * 校验提取码后渲染内容。
     * 由 share.html 的"预览文件"按钮（onclick="verifyShare()"）触发。
     */
    window.verifyShare = async function () {
        const result = await X.postForm("/api/share-check", {
            share_id: currentShareId,
            password: document.getElementById("sharePassword").value,
        });
        if (!result.success) {
            document.getElementById("errorText").innerText = result.message || "验证失败";
            return;
        }
        // 保存后端签发的短期访问令牌，后续取内容/下载/播放都携带它
        currentAccess = result.access_token || "";
        await loadContentAndRender();
    };

    /**
     * 拉取共享内容并按 share_mode 渲染（相册 / 普通文件夹）。
     * currentAccess 非空时自动附加 access 令牌（免密码验证路径）。
     * 供 verifyShare（密码验证后）与 onload 的 ?access= 免验证路径共用。
     */
    async function loadContentAndRender() {
        const qs = currentAccess ? "?access=" + encodeURIComponent(currentAccess) : "";
        const data = await X.getJSON("/api/share-content/" + currentShareId + qs);
        if (!data.success) {
            document.getElementById("errorText").innerText = data.message || "加载失败";
            return false;
        }
        // 按共享模式分流渲染（渲染函数内部会隐藏密码卡片）
        if (data.share_mode === "album") {
            renderAlbum(data);
        } else {
            renderFolder(data);
        }
        return true;
    }

    /**
     * 计算某文件的下载 URL。
     * 文件型共享走 /api/share-download/{id}；文件夹型用文件全路径拼
     * /api/share-folder-download/{id}/{全路径}（子目录文件也能正确编码）。
     */
    function fileDownloadUrl(data, file) {
        if (data.type === "file") {
            let u = "/api/share-download/" + data.share_id;
            if (currentAccess) u += "?access=" + encodeURIComponent(currentAccess);
            return u;
        }
        // file.path 是相对 UPLOAD_DIR 的全路径（共享页 tree 已统一）
        const rel = file.path ? file.path : (data.name + "/" + file.name);
        let u = "/api/share-folder-download/" + data.share_id + "/" +
            encodeURIComponent(rel);
        if (currentAccess) u += "?access=" + encodeURIComponent(currentAccess);
        return u;
    }

    // ---------- 普通文件夹（树状） ----------

    /* 单条文件行：缩进由 CSS 嵌套 .node-content 层级控制，不用内联 padding */
    function shareFileRowHtml(file, data) {
        if (X.IMAGE_EXTS.includes(file.extension || X.getExt(file.name))) {
            imageFiles.push({ path: file.path, name: file.name });
        }
        return `
        <div class="share-file-row tree-share-file">
            <div class="share-file-name" data-preview="${esc(file.path)}">
                📄 ${esc(file.name)}
            </div>
            <div class="share-file-tags">
                <div class="share-file-tag">${X.formatSize(file.size)}</div>
            </div>
            <a href="${fileDownloadUrl(data, file)}" class="btn btn-primary btn-sm"
               target="_blank">下载</a>
        </div>`;
    }

    /** 递归渲染共享树节点（子文件夹 + 其文件，可折叠，由 CSS 嵌套缩进）。 */
    function renderShareTreeNode(node, data) {
        let inner = "";
        (node.files || []).forEach((f) => { inner += shareFileRowHtml(f, data); });
        (node.children || []).forEach((c) => { inner += renderShareTreeNode(c, data); });

        const hasContent = inner.length > 0;
        const titleId = "share-node-" + node.path.replaceAll("/", "_").replaceAll("\\", "_");
        return `
        <div class="tree-node">
            <div class="share-folder-title" data-action="share-toggle" data-node-id="${esc(titleId)}">
                <span class="tree-toggle-indicator">${hasContent ? "▾" : ""}</span>
                <span class="tree-folder-name">📁 ${esc(node.name)}</span>
                <div class="folder-tag">📄 ${node.file_count}</div>
            </div>
            ${hasContent ? `<div id="${esc(titleId)}" class="node-content">${inner}</div>` : ""}
        </div>`;
    }

    /**
     * 渲染普通文件夹（树状）：隐藏首屏、显示 #fileCard，
     * "打包下载"按钮 + 树状文件结构（子文件夹可折叠、文件可预览/下载）。
     * 缩进完全交给 CSS 嵌套 .node-content；折叠由 .tree-node.collapsed 驱动。
     */
    function renderFolder(data) {
        imageFiles = [];

        document.getElementById("shareCard").style.display = "none";
        const fileCard = document.getElementById("fileCard");
        fileCard.style.display = "block";

        // 单文件共享：直接一行下载（设密码时携带 access 令牌）
        let singleDlUrl = "/api/share-download/" + data.share_id;
        if (currentAccess) singleDlUrl += "?access=" + encodeURIComponent(currentAccess);
        if (data.type === "file") {
            fileCard.innerHTML = `
                <div class="folder-header"><h4>${esc(data.name)}</h4></div>
                <div class="share-file-row">
                    <div class="share-file-name">📄 ${esc(data.path)}</div>
                    <a href="${singleDlUrl}"
                       class="btn btn-primary btn-sm" target="_blank">下载</a>
                </div>`;
            return;
        }

        // 文件夹共享：全层级文件收集可预览图片（灯箱切换用）
        (data.files || []).forEach((f) => {
            if (X.IMAGE_EXTS.includes(f.extension || X.getExt(f.name))) {
                imageFiles.push({ path: f.path, name: f.name });
            }
        });

        // 树状结构（data.tree 来自后端 build_file_tree；兼容无 tree 时退回扁平）
        const tree = data.tree || { files: data.files || [], children: [] };
        const topFilesHtml = (tree.files || []).map((f) => shareFileRowHtml(f, data)).join("");
        const treeHtml = (tree.children || []).map((c) => renderShareTreeNode(c, data)).join("");

        fileCard.innerHTML = `
            <div class="folder-header"><h4>${esc(data.name)}</h4></div>
            <div class="folder-download">
                <button class="btn btn-primary btn-sm" data-zip="${esc(data.share_id)}">
                    📦 打包下载所有文件
                </button>
            </div>
            <div id="folderFiles">${topFilesHtml}${treeHtml}</div>`;
    }

    /** 文件夹列表：事件委托处理预览、打包与树节点折叠（collapsed class 驱动）。 */
    function onFileCardClick(e) {
        const previewEl = e.target.closest("[data-preview]");
        if (previewEl) {
            previewFile(previewEl.dataset.preview, imageFiles);
            return;
        }
        const zipEl = e.target.closest("[data-zip]");
        if (zipEl) {
            downloadFolderZip(zipEl.dataset.zip);
            return;
        }
        const toggleEl = e.target.closest("[data-action='share-toggle']");
        if (toggleEl) {
            const treeNode = toggleEl.closest(".tree-node");
            if (treeNode) treeNode.classList.toggle("collapsed");
        }
    }

    // ---------- 打包下载（统一轮询） ----------

    /**
     * 打包并下载整个共享文件夹。
     * 弹出"正在打包"遮罩弹窗，启动 zip 任务后轮询状态，
     * 完成后跳转下载链接、失败则提示；无论成败都关闭遮罩。
     */
    async function downloadFolderZip(shareId) {
        const modal = new bootstrap.Modal(
            document.getElementById("zipLoadingModal")
        );
        modal.show();

        const res = await fetch("/api/share-folder-zip-task/" + shareId, { method: "POST" });
        const data = await res.json();

        try {
            await X.pollZipTask(data.task_id, { intervalMs: 1000 });
            bootstrap.Modal
                .getInstance(document.getElementById("zipLoadingModal"))
                .hide();
            window.location = "/api/share-folder-zip-download/" + data.task_id;
        } catch (e) {
            bootstrap.Modal
                .getInstance(document.getElementById("zipLoadingModal"))
                .hide();
            alert(e.message || "打包失败");
        }
    };

    // ---------- 相册模式（支持树形结构）----------

    /**
     * 递归收集整棵目录树的图片/视频（全层级），用于：
     *   - 灯箱切换（imageFiles）
     *   - 全局统计（横幅 + 过滤按钮计数）
     *   - 封面随机
     * @param {Object} node 树节点（含 files / children）
     * @param {Object} data 共享上下文（data.name 为共享根目录相对路径）
     * @returns {{ images: Array, videos: Array, imageCount: number, videoCount: number }}
     */
    function collectAlbumMedia(node, data) {
        const out = { images: [], videos: [], imageCount: 0, videoCount: 0 };
        (function walk(n) {
            (n.files || []).forEach((f) => {
                const ext = f.extension || X.getExt(f.name);
                const fullPath = f.path || (data.name + "/" + f.name);
                const relPath = fullPath.startsWith(data.name + "/")
                    ? fullPath.slice(data.name.length + 1) : f.name;
                if (X.IMAGE_EXTS.includes(ext)) {
                    out.images.push({ path: fullPath, name: f.name, relPath });
                    out.imageCount++;
                } else if (X.VIDEO_EXTS.includes(ext)) {
                    out.videos.push({ relPath, name: f.name, fullPath });
                    out.videoCount++;
                }
            });
            (n.children || []).forEach(walk);
        })(node);
        return out;
    }

    /*
     * 扁平化卡片渲染（重构：主界面不再显示目录树，全层级媒体合并成一张网格，
     * 目录改由筛选栏的"目录下拉"过滤）。
     * data-rel = 相对共享根的路径（含子目录段），目录筛选按它匹配。
     */

    /** 由文件条目取"相对共享根"的路径（用于目录筛选匹配与视频播放）。 */
    function fileRelPath(file, data) {
        const fullPath = file.path || (data.name + "/" + file.name);
        return fullPath.startsWith(data.name + "/")
            ? fullPath.slice(data.name.length + 1) : file.name;
    }

    /** 单张相册图片卡片：封面图 + 悬停信息层（文件名 + 类型角标 + 大小）。 */
    function albumImageCardHtml(file, data) {
        const fullPath = file.path || (data.name + "/" + file.name);
        const rel = fileRelPath(file, data);
        const thumbUrl = "/api/thumbnail/" + encodeURIComponent(fullPath);
        return `
            <div class="album-item" data-preview="${esc(fullPath)}"
                 data-type="photo" data-rel="${esc(rel)}">
                <img class="card-img" src="${thumbUrl}" loading="lazy" alt="${esc(file.name)}">
                <div class="media-fallback">🖼</div>
                <div class="media-overlay">
                    <span class="media-name">${esc(file.name)}</span>
                    <span class="media-meta"><i class="media-badge">图</i><i class="media-size">${X.formatSize(file.size || 0)}</i></span>
                </div>
            </div>`;
    }

    /** 单个视频卡片（data-video 为相对共享根的路径；data-rel 供目录筛选）。 */
    function albumVideoCardHtml(file, data) {
        const fullPath = file.path || (data.name + "/" + file.name);
        const rel = fileRelPath(file, data);
        const thumbUrl = "/api/video-thumbnail/" + encodeURIComponent(fullPath);
        return `
            <div class="video-card" data-video="${esc(rel)}"
                 data-type="video" data-rel="${esc(rel)}">
                <img class="card-img" src="${thumbUrl}" loading="lazy" alt="${esc(file.name)}">
                <div class="media-fallback">▶</div>
                <div class="media-overlay">
                    <span class="media-name">${esc(file.name)}</span>
                    <span class="media-meta"><i class="media-badge video">视</i><i class="media-size">${X.formatSize(file.size || 0)}</i></span>
                </div>
            </div>`;
    }

    /** 按扩展名选卡片渲染器。 */
    function mediaCardByFile(file, data) {
        const ext = file.extension || X.getExt(file.name);
        if (X.VIDEO_EXTS.includes(ext)) return albumVideoCardHtml(file, data);
        if (X.IMAGE_EXTS.includes(ext)) return albumImageCardHtml(file, data);
        return "";
    }

    /** 递归收集共享目录下所有含媒体的文件夹相对路径（用于目录筛选下拉）。 */
    function collectFolderRels(node, prefix) {
        const out = [];
        (node.children || []).forEach((c) => {
            const rel = prefix ? prefix + "/" + c.name : c.name;
            out.push(rel);
            out.push(...collectFolderRels(c, rel));
        });
        return out;
    }

    /**
     * 渲染相册模式（扁平网格 + 组合筛选，主界面不再显示目录树）：
     *   - 横幅：封面轮播 + 标题 + 总统计 + 类型 pill（全部/图片/视频）
     *   - 筛选信息行：显示当前筛选摘要（如"🖼 图片 · 📁 1234 · 3 张"）
     *   - 目录筛选下拉：选某目录则只显示该目录（含子目录）下的媒体
     *   - 全层级媒体合并成一张扁平网格（data-rel 标记目录归属）
     *   - 灯箱图片列表随当前筛选（类型+目录）动态变化
     */
    function renderAlbum(data) {
        imageFiles = [];

        document.getElementById("shareCard").style.display = "none";
        const container = document.getElementById("albumCard");
        container.style.display = "block";
        // 重置筛选状态（每次进相册页默认"全部 + 全部目录"）
        currentAlbumType = "all";
        currentAlbumDir = "all";
        container.classList.remove("filter-photos", "filter-videos");

        const tree = data.tree || { files: data.files || [], children: [] };
        const media = collectAlbumMedia(tree, data);
        media.images.forEach((img) => imageFiles.push({ path: img.path, name: img.name }));

        // 全层级媒体文件（分离成图片网格 + 视频网格，不混合）
        const allFiles = data.files || [];
        const folderRels = collectFolderRels(tree, "");

        // 图片卡片 + 视频卡片（分别渲染两个独立网格）
        let photosHtml = "", videosHtml = "";
        allFiles.forEach((f) => {
            const ext = f.extension || X.getExt(f.name);
            if (X.IMAGE_EXTS.includes(ext)) photosHtml += albumImageCardHtml(f, data);
            else if (X.VIDEO_EXTS.includes(ext)) videosHtml += albumVideoCardHtml(f, data);
        });

        // 封面轮播（3~4 张交叉淡入；无图则 📷 占位）
        const carouselCount = Math.min(4, imageFiles.length);
        let carouselHtml = "";
        if (imageFiles.length > 0) {
            const step = imageFiles.length / carouselCount;
            carouselHtml = Array.from({ length: carouselCount }, (_, i) => {
                const img = imageFiles[Math.floor(i * step) % imageFiles.length];
                return `<img class="album-cover-image album-cover-slide"
                            style="animation-delay:${i * 3.2}s"
                            src="/api/preview-image/${encodeURIComponent(img.path)}" alt="">`;
            }).join("");
        }
        const bannerInner = imageFiles.length > 0
            ? carouselHtml
            : `<div class="album-cover-fallback" aria-hidden="true">📷</div>`;

        // 类型 pill（全部/图片/视频）紧凑胶囊组（不显示数字，横幅统计已含数量）
        const pillsHtml = `
            <div class="album-switch">
                <div class="album-entry active" data-action="album-filter" data-filter="all">
                    <span class="entry-icon">✨</span><span class="entry-title">全部</span>
                </div>
                <div class="album-entry" data-action="album-filter" data-filter="photos">
                    <span class="entry-icon">🖼</span><span class="entry-title">图片</span>
                </div>
                <div class="album-entry" data-action="album-filter" data-filter="videos">
                    <span class="entry-icon">🎬</span><span class="entry-title">视频</span>
                </div>
            </div>`;

        // 目录筛选下拉（"全部目录" + 各子文件夹；选某目录 = 该目录及其子目录）
        const dirSelectHtml = folderRels.length ? `
            <select id="albumDirSelect" class="album-dir-select" data-action="album-dir">
                <option value="all">📁 全部目录</option>
                ${folderRels.map((r) => `<option value="${esc(r)}">${esc(r)}</option>`).join("")}
            </select>` : "";

        const emptyStateHtml = (media.imageCount + media.videoCount === 0) ? `
            <div class="album-empty">
                <div class="album-empty-icon">🗂</div>
                <div>该共享中没有可预览的图片或视频</div>
            </div>` : "";

        container.innerHTML = `
            <div class="album-banner${imageFiles.length > 0 ? " album-carousel" : " album-no-cover"}">
                ${bannerInner}
                <div class="album-overlay">
                    <div class="album-title">${esc(data.name)}</div>
                    <div class="album-stats">${media.imageCount}张图片 · ${media.videoCount}个视频</div>
                </div>
            </div>
            <!-- 筛选栏：左侧筛选信息（含目录）+ 右侧类型胶囊组 -->
            <div class="album-filter-bar">
                <span class="album-filter-info" id="albumFilterInfo"></span>
                ${dirSelectHtml}
                ${pillsHtml}
            </div>
            ${emptyStateHtml ? emptyStateHtml : `
            <div class="album-grid-wrap">
                ${media.imageCount > 0 ? `
                <section class="album-section photo-section" id="photoSection">
                    <div class="album-section-title">🖼 图片<span class="album-section-count">${media.imageCount}</span></div>
                    <div class="album-grid" id="photoGrid">${photosHtml}</div>
                </section>` : ""}
                ${media.videoCount > 0 ? `
                <section class="album-section video-section" id="videoSection">
                    <div class="album-section-title">🎬 视频<span class="album-section-count">${media.videoCount}</span></div>
                    <div class="album-grid" id="videoGrid">${videosHtml}</div>
                </section>` : ""}
                <div class="album-grid-empty" style="display:none">
                    <div class="album-empty-icon">🔍</div>
                    <div>当前筛选条件下没有图片/视频</div>
                </div>
            </div>`}
            ${media.imageCount + media.videoCount > 0 ? `
            <button id="albumBackTop" class="album-back-top"
                    title="返回顶部" aria-label="返回顶部">↑</button>` : ""}`;

        // 缩略图：出图淡入 / 失败兜底
        container.querySelectorAll(".card-img").forEach((img) => {
            if (img.complete && img.naturalWidth > 0) img.classList.add("loaded");
            else {
                img.addEventListener("load", () => img.classList.add("loaded"), { once: true });
                img.addEventListener("error", () => {
                    img.style.display = "none";
                    const holder = img.parentElement.querySelector(".media-fallback");
                    if (holder) holder.style.display = "flex";
                }, { once: true });
            }
        });

        // 目录下拉：change 时与当前类型组合筛选
        const dirSel = document.getElementById("albumDirSelect");
        if (dirSel) {
            dirSel.addEventListener("change", () => {
                currentAlbumDir = dirSel.value;
                applyAlbumFilter(currentAlbumType, currentAlbumDir);
            });
        }

        // 初始应用一次（默认 全部/全部目录）以填充筛选信息行
        applyAlbumFilter("all", "all");

        // 返回顶部
        const backTop = document.getElementById("albumBackTop");
        if (backTop) {
            window.addEventListener("scroll", () => {
                backTop.classList.toggle("show", window.scrollY > 300);
            }, { passive: true });
            backTop.addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));
        }
    }

    /** 当前筛选（类型×目录）下可见的图片列表（灯箱切换用）：读可见的 .album-item 卡片。 */
    function visibleAlbumPhotos() {
        const grid = document.getElementById("photoGrid");
        if (!grid) return imageFiles;
        return [...grid.querySelectorAll(".album-item")]
            .filter((el) => el.style.display !== "none")
            .map((el) => ({ path: el.dataset.preview, name: el.querySelector(".media-name").textContent }));
    }

    /**
     * 组合筛选：类型（全部/图片/视频）× 目录（全部/某目录含子目录）。
     *   - 类型：photos 只显示图片网格、videos 只显示视频网格、all 都显示；
     *   - 目录：按卡片 data-rel 前缀匹配，逐卡显隐；
     *   - 高亮当前类型 pill（筛选栏内）；
     *   - 更新筛选信息行 #albumFilterInfo（如"🖼 图片 · 📁 1234 · 3 张"）；
     *   - 若筛选结果为空，显示网格空态提示。
     * @param {string} type "all" | "photos" | "videos"
     * @param {string} dir  "all" 或某目录相对路径（含其子目录）
     */
    function applyAlbumFilter(type, dir) {
        currentAlbumType = type;
        currentAlbumDir = dir;
        const card = document.getElementById("albumCard");
        const photoGrid = card.querySelector("#photoGrid");
        const videoGrid = card.querySelector("#videoGrid");
        const photoSection = card.querySelector("#photoSection");
        const videoSection = card.querySelector("#videoSection");

        // 按类型切换两个区块的显示
        const showPhotos = (type === "all" || type === "photos") && !!photoSection;
        const showVideos = (type === "all" || type === "videos") && !!videoSection;
        if (photoSection) photoSection.style.display = showPhotos ? "" : "none";
        if (videoSection) videoSection.style.display = showVideos ? "" : "none";

        // 按目录逐卡显隐（data-rel 前缀匹配）
        let shownCount = 0;
        const allCards = [
            ...(photoGrid ? photoGrid.querySelectorAll(".album-item") : []),
            ...(videoGrid ? videoGrid.querySelectorAll(".video-card") : []),
        ];
        allCards.forEach((el) => {
            const rel = el.dataset.rel || "";
            const dirOk = (dir === "all")
                || (rel === dir || rel.startsWith(dir + "/"));
            el.style.display = dirOk ? "" : "none";
            if (dirOk) shownCount++;
        });

        // 高亮对应类型 pill
        card.querySelectorAll("[data-action='album-filter']").forEach((el) => {
            el.classList.toggle("active", el.dataset.filter === type);
        });

        // 筛选信息行
        const info = card.querySelector("#albumFilterInfo");
        if (info) {
            let label = "";
            if (type === "all" && dir === "all") {
                label = `✨ 全部 · ${shownCount} 项`;
            } else {
                const tIcon = type === "photos" ? "🖼 图片" : (type === "videos" ? "🎬 视频" : "✨ 全部");
                const dLabel = dir === "all" ? "全部目录" : ("📁 " + dir);
                const nUnit = type === "videos" ? "个" : (type === "photos" ? "张" : "项");
                label = `${tIcon} · ${dLabel} · ${shownCount} ${nUnit}`;
            }
            info.textContent = label;
        }

        // 筛选结果为空 → 网格空态
        const empty = card.querySelector(".album-grid-empty");
        if (empty) empty.style.display = shownCount === 0 ? "block" : "none";
    }

    /** 相册事件委托：图片预览（可见集合）/ 视频播放 / 类型 pill（携带当前目录）。 */
    function onAlbumCardClick(e) {
        // 图片预览（灯箱用"当前筛选下可见"的图片集合，可前后切换）
        const previewEl = e.target.closest("[data-preview]");
        if (previewEl) {
            previewFile(previewEl.dataset.preview, visibleAlbumPhotos());
            return;
        }
        // 视频播放：同标签页跳转到播放页（不使用新窗口，浏览器可正常返回）
        const videoEl = e.target.closest("[data-video]");
        if (videoEl) {
            let url = "/share/video/" + currentShareId + "/" +
                encodeURIComponent(videoEl.dataset.video);
            if (currentAccess) url += "?access=" + encodeURIComponent(currentAccess);
            window.location.href = url;
            return;
        }
        // 类型 pill（全部/图片/视频）：与当前目录组合筛选
        const filterEl = e.target.closest("[data-action='album-filter']");
        if (filterEl) {
            applyAlbumFilter(filterEl.dataset.filter, currentAlbumDir);
            return;
        }
    }
})();
