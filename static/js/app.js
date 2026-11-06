/**
 * 主页面（index.html）业务逻辑。
 *
 * 依赖 common.js（XSLib）提供的 escapeHtml / formatSize / postForm /
 * getJSON / pollZipTask / copyText。预览逻辑由 preview.js 提供 previewFile。
 *
 * 交互模型：
 *   - 静态控件（登录/上传/搜索/共享/弹窗按钮）用 addEventListener 绑定；
 *   - 动态列表（文件、共享、用户）统一用 data-* 属性 + 事件委托
 *     （#fileList 上挂一个 click 监听器），避免 inline onclick 中
 *     文件名含引号导致 JS 断裂、以及文件名注入 innerHTML（XSS）。
 *   - 所有需要被 HTML 内联属性调用的函数挂到 window。
 *
 * 页面分节（与下方代码区段一一对应）：
 *   初始化 → 登录与权限 → 页面切换 → 文件浏览 → 主题
 *   → 新建文件夹/上传 → 共享 → 文件夹权限 → 访客上传链接
 *   → 用户管理 → 系统设置 → 重命名 → 文件夹 zip 打包
 */
(function () {
    "use strict";

    const X = window.XSLib;
    const esc = X.escapeHtml;

    /* 模块级状态：当前目录、可预览图片列表、共享/权限/重命名上下文 */
    let currentFolder = ".";
    let imageFiles = [];
    let currentShareFile = "";
    let currentShareType = "";
    let currentPermFolder = ".";
    let renamePath = "";
    // 共享/上传弹窗默认值（loadSettings 时从 /api/settings 载入）
    let shareDefaults = { days: 7, mode: "normal" };

    // ---------- 初始化 ----------

    // DOM 就绪后绑定所有静态控件。#fileList 只挂一个委托监听器，
    // 其内部动态生成的文件/文件夹卡片上的操作全部由它分发。
    document.addEventListener("DOMContentLoaded", () => {
        // 恢复上次选择的深浅主题
        if (localStorage.getItem("theme") === "dark") {
            document.body.classList.add("dark");
        }

        document.getElementById("loginBtn").addEventListener("click", login);
        document.getElementById("uploadBtn").addEventListener("click", uploadFile);
        document.getElementById("search").addEventListener("keyup", searchFiles);
        document.getElementById("createShareBtn").addEventListener("click", createShareLink);
        document.getElementById("fileList").addEventListener("click", onFileListClick);

        // 二维码预览：配色/纠错/模块尺寸任一变化即刷新右上角真实二维码（绑定一次）
        document.getElementById("qrColor").addEventListener("change", updateQrPreview);
        document.getElementById("qrEc").addEventListener("change", updateQrPreview);
        document.getElementById("qrBox").addEventListener("input", updateQrPreview);

        // 登录框回车直接提交
        document
            .getElementById("login-page")
            .addEventListener("keydown", (e) => {
                if (e.key === "Enter") login();
            });

        // 有上次的会话令牌则尝试免登录恢复
        restoreSession();
    });

    // 页面加载后拉取站点名并刷新所有标题位置
    window.onload = loadSiteSetting;

    /**
     * 会话恢复：localStorage 里存有 token 与用户名时，带 token 调一个受保护
     * 接口（/api/users）验证有效性；成功则直接进主界面，失败则清除重登。
     */
    async function restoreSession() {
        const username = localStorage.getItem("username");
        const token = localStorage.getItem("xs_token");
        if (!username || !token) return;

        X.setAuth(token);
        try {
            const res = await X.getJSON("/api/users"); // 受保护接口，无有效 token 返回 401
            // 中间件对未授权统一回 {success:false, message}；有效 token 才返回用户字典
            if (res && res.success !== false) {
                await enterMainPage(username);
                return;
            }
        } catch (e) {
            /* 网络异常按未登录处理 */
        }
        // 验证失败：清除会话，重新登录
        X.setAuth("");
        localStorage.removeItem("username");
        localStorage.removeItem("permissions");
    }

    /** 进入主界面：按 localStorage 里缓存的用户与权限显示界面并加载数据。 */
    async function enterMainPage(username) {
        document.getElementById("login-page").style.display = "none";
        document.getElementById("main-page").style.display = "block";
        document.getElementById("currentUser").innerText = username;
        document.getElementById("mobileUserName").innerText = username;
        initPermission();
        loadFiles();
    }

    // ---------- 登录与权限 ----------

    /**
     * 登录：成功后把用户名与权限缓存进 localStorage，
     * 隐藏登录页、显示主界面，并按权限显隐各功能入口。
     */
    async function login() {
        const result = await X.postForm("/api/login", {
            username: document.getElementById("username").value,
            password: document.getElementById("password").value,
        });

        if (!result.success) {
            alert(result.message || "密码错误");
            return;
        }

        localStorage.setItem("username", result.username);
        localStorage.setItem("permissions", JSON.stringify(result.permissions));
        X.setAuth(result.token || "");   // 会话令牌：后续管理 API 自动携带

        await enterMainPage(result.username);
    }

    /** 判断当前用户是否拥有某权限（权限缓存自登录响应）。 */
    function hasPermission(name) {
        return JSON.parse(localStorage.getItem("permissions") || "[]").includes(name);
    }

    /** 按权限隐藏无权限的桌面端与移动端入口。 */
    function initPermission() {
        const hide = (ids) => ids.forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.style.display = "none";
        });

        if (!hasPermission("upload")) hide(["uploadArea"]);
        if (!hasPermission("user_manage")) hide(["userManageBtn", "mobileUserBtn"]);
        if (!hasPermission("share_manage")) hide(["shareManageBtn", "mobileShareBtn"]);
        if (!hasPermission("system_manage")) hide(["settingManageBtn", "mobileSettingBtn"]);
    }

    // ---------- 页面切换 ----------

    // 四个主功能区（文件/共享/用户/设置）互斥显示；每个入口函数
    // 先切到对应面板，再加载该面板数据。
    const PAGE_IDS = ["filePage", "shareManagerPage", "userManagerPage", "settingManagerPage"];

    function showPage(pageId) {
        PAGE_IDS.forEach((id) => {
            document.getElementById(id).style.display = id === pageId ? "block" : "none";
        });
    }

    window.showFilePage = function () {
        showPage("filePage");
        loadFiles();
    };

    window.showShareManager = function () {
        showPage("shareManagerPage");
        loadLinks();
    };

    window.showUserManager = function () {
        showPage("userManagerPage");
        loadUsers();
    };

    window.showSetManager = function () {
        showPage("settingManagerPage");
        loadSettings();
    };

    // ---------- 文件浏览 ----------

    // ---------- 树状文件渲染 ----------

    /** 原地排序目录/子节点：含中文的排后面，其余按中文区 localeCompare。 */
    function sortTreeNodes(nodes) {
        const isZh = (s) => /[\u4e00-\u9fa5]/.test(s);
        return nodes.sort((a, b) => {
            const zhA = isZh(a.name), zhB = isZh(b.name);
            if (zhA !== zhB) return zhA ? 1 : -1;
            return a.name.localeCompare(b.name, "zh-CN");
        });
    }

    /** 单条文件行。收集可预览图片到 imageFiles。缩进由 CSS 嵌套层级控制。 */
    function fileRowHtml(file) {
        const ext = file.extension || X.getExt(file.name);
        if (X.IMAGE_EXTS.includes(ext)) {
            imageFiles.push({ path: file.path, name: file.name });
        }
        const p = esc(file.path);
        return `
        <div class="file-item tree-file">
            <div class="file-name" data-action="preview" data-path="${p}">
                📄 ${esc(file.name)}
            </div>
            <div class="file-meta">
                <span class="meta-size">${X.formatSize(file.size)}</span>
                <span class="meta-type">${esc(ext)}</span>
                <span class="meta-date">${esc(file.modified)}</span>
            </div>
            <div class="file-actions">
                ${hasPermission("download") ? `
                    <a data-action="download" data-path="${p}" class="icon-btn" title="下载">
                        <i class="bi bi-download"></i>
                    </a>` : ""}
                ${hasPermission("share") ? `
                    <button data-action="share" data-path="${p}" data-type="file"
                        class="icon-btn" title="共享">
                        <i class="bi bi-share"></i>
                    </button>` : ""}
                ${hasPermission("upload") ? `
                    <button data-action="rename" data-path="${p}"
                        class="icon-btn" title="重命名">
                        <i class="bi bi-pencil"></i>
                    </button>` : ""}
                ${hasPermission("delete") ? `
                    <button data-action="delete" data-path="${p}"
                        class="icon-btn delete-btn" title="删除">
                        <i class="bi bi-trash"></i>
                    </button>` : ""}
            </div>
        </div>`;
    }

    /** 文件夹节点的操作按钮组（下载/访客上传/共享/权限）。 */
    function folderActionHtml(folderPath) {
        const fp = esc(folderPath);
        return `
            ${hasPermission("download") ? `
                <button data-action="folder-zip" data-folder-path="${fp}"
                    class="icon-btn" title="下载文件夹">
                    <i class="bi bi-download"></i>
                </button>` : ""}
            ${hasPermission("share") ? `
                <button data-action="upload-link" data-folder-path="${fp}"
                    class="icon-btn" title="访客上传">
                    <i class="bi bi-upload"></i>
                </button>
                <button data-action="share" data-path="${fp}" data-type="folder"
                    class="icon-btn" title="共享">
                    <i class="bi bi-share"></i>
                </button>` : ""}
            ${hasPermission("share_manage") ? `
                <button data-action="folder-perm" data-folder-path="${fp}"
                    class="icon-btn" title="权限">
                    <i class="bi bi-shield-lock"></i>
                </button>` : ""}`;
    }

    /**
     * 递归渲染一个目录节点（标题 + 包裹内容）。
     * 缩进完全由 CSS 嵌套的 .node-content 层级控制，不再用内联 padding。
     * 默认展开。
     */
    function renderTreeNode(node) {
        const folderId = node.path.replaceAll("/", "_").replaceAll("\\", "_");
        const folderPath = node.path === "." ? "." : node.path;
        sortTreeNodes(node.children || []);

        let inner = "";
        (node.files || []).forEach((f) => { inner += fileRowHtml(f); });
        (node.children || []).forEach((c) => { inner += renderTreeNode(c); });

        const hasContent = inner.length > 0;
        // 默认收起：有内容的文件夹节点初始带 collapsed（CSS 让指示器指向右），
        // 用户点击 .tree-folder-title 展开；空文件夹无内容行，保持展开。
        return `
        <div class="tree-node${hasContent ? " collapsed" : ""}">
            <div class="folder-title tree-folder-title"
                 data-action="toggle" data-node-id="${esc(folderId)}" data-folder-path="${esc(folderPath)}">
                <span class="tree-toggle-indicator">${hasContent ? "▾" : ""}</span>
                <span class="folder-name tree-folder-name">📁 ${esc(node.name)}</span>
                <div class="folder-meta">
                    <span class="folder-tag">📄 ${node.file_count}</span>
                    <span class="folder-tag">${X.formatSize(node.size)}</span>
                </div>
                <div class="folder-actions">${folderActionHtml(folderPath)}</div>
            </div>
            ${hasContent ? `<div id="node-${esc(folderId)}" class="node-content">${inner}</div>` : ""}
        </div>`;
    }

    /**
     * 拉取树状文件结构并渲染。
     * 同时收集全层级可预览图片到 imageFiles，供灯箱前后切换。
     * 节点内操作按钮都带 data-action + data-*，由 onFileListClick 统一分发。
     */
    window.loadFiles = async function () {
        const data = await X.getJSON("/api/files/tree?username=" + encodeURIComponent(
            localStorage.getItem("username") || ""
        ));

        const fileList = document.getElementById("fileList");
        fileList.innerHTML = "";
        imageFiles = [];

        const treeHtml = (data.children || []).map((child) => renderTreeNode(child)).join("");
        fileList.insertAdjacentHTML("beforeend", treeHtml || '<div class="empty-tip">暂无文件</div>');
    };

    /**
     * 事件委托：#fileList 上唯一的点击监听器，
     * 依据被点元素最近的 [data-action] 上下文分发到对应处理函数。
     * data-* 值在渲染时已 HTML 转义，取用时无引号断裂/XSS 风险。
     */
    function onFileListClick(e) {
        const el = e.target.closest("[data-action]");
        if (!el) return;

        // 点击文件夹标题里的操作按钮时不触发折叠（按钮自身处理）
        if (el.dataset.action === "toggle") {
            if (e.target.closest(".icon-btn")) return;
            toggleNode(el.dataset.nodeId, el.dataset.folderPath);
            return;
        }

        switch (el.dataset.action) {
            case "preview":
                previewFile(el.dataset.path, imageFiles);
                break;
            case "download":
                // 浏览器整页跳转无法带请求头，把 token 放进查询参数（authedUrl）
                window.location = X.authedUrl("/api/download/" + encodeURIComponent(el.dataset.path));
                break;
            case "share":
                shareFile(el.dataset.path, el.dataset.type);
                break;
            case "rename":
                renameFile(el.dataset.path);
                break;
            case "delete":
                deleteFile(el.dataset.path);
                break;
            case "folder-zip":
                downloadFolderZip(el.dataset.folderPath);
                break;
            case "upload-link":
                createUploadLink(el.dataset.folderPath);
                break;
            case "folder-perm":
                showFolderPermission(el.dataset.folderPath);
                break;
        }
    }

    /**
     * 展开/折叠树节点：切换 .tree-node 的 collapsed class（CSS 驱动
     * 指示器旋转与内容显隐），更新当前目录，并把该节点设为选中态。
     */
    window.toggleNode = function (nodeId, folderPath) {
        currentFolder = folderPath;

        const label = document.getElementById("folderSelect");
        if (label) label.innerText = folderPath;

        const title = document.querySelector(`[data-node-id="${nodeId}"]`);
        if (!title) return;

        document.querySelectorAll(".tree-folder-title")
            .forEach((item) => item.classList.remove("folder-selected"));
        title.classList.add("folder-selected");

        // 切换该节点 collapsed 状态（CSS 据此隐藏内容、旋转指示器）
        const treeNode = title.closest(".tree-node");
        treeNode.classList.toggle("collapsed");
    };

    /** 删除文件（带确认弹窗），成功后刷新列表。删除接口需携带会话 token。 */
    window.deleteFile = async function (path) {
        if (!confirm("确定删除文件吗？")) return;
        await fetch("/api/delete/" + encodeURIComponent(path), {
            method: "DELETE",
            headers: X.authHeaders(),
        });
        loadFiles();
    };

    /**
     * 前端本地搜索（树状）：自底向上判断每个目录是否保留。
     * 一个目录保留的条件：目录名匹配，或其直接可见文件匹配，或其可见子目录被保留。
     * 空关键词恢复全部显示。
     */
    window.searchFiles = function () {
        const keyword = document.getElementById("search").value.trim().toLowerCase();
        const nodes = Array.from(document.querySelectorAll("#fileList .tree-node"));

        // 自底向上：先处理没有子节点的（叶子），再逐层向上
        nodes.sort((a, b) =>
            b.querySelectorAll(".tree-node").length - a.querySelectorAll(".tree-node").length
        );

        nodes.forEach((node) => {
            const contentEl = node.querySelector(".node-content");
            const directFiles = Array.from(contentEl.querySelectorAll(":scope > .file-item"));
            const directChildren = Array.from(contentEl.querySelectorAll(":scope > .tree-node"));
            const titleText = (node.querySelector(".tree-folder-name")?.textContent || "").toLowerCase();

            const fileMatch = keyword === "" ||
                directFiles.some((f) => f.innerText.toLowerCase().includes(keyword));
            const childKeep = directChildren.some((c) => c.style.display !== "none");
            const titleMatch = keyword === "" || titleText.includes(keyword);

            // 子目录显示与否取决于其是否匹配（已逐层算好），文件/标题按关键词
            directFiles.forEach((f) => {
                f.style.display = (keyword === "" ||
                    f.innerText.toLowerCase().includes(keyword)) ? "" : "none";
            });

            node.style.display = (titleMatch || fileMatch || childKeep) ? "" : "none";

            // 默认收起：搜索时自动展开命中路径上的折叠节点（否则深层结果被 .collapsed 挡住）；
            // 清空关键词时不强制恢复，保留用户当前的展开/收起浏览状态。
            if (keyword !== "" && node.style.display !== "none") {
                node.classList.remove("collapsed");
            }
        });
    };

    // ---------- 主题 ----------

    /** 切换深浅主题并写入 localStorage；同步更新桌面与移动端按钮文案。 */
    window.toggleDarkMode = function () {
        const isDark = document.body.classList.toggle("dark");
        localStorage.setItem("theme", isDark ? "dark" : "light");

        const btn = document.getElementById("darkModeBtn");
        if (btn) btn.innerHTML = isDark ? "☀️ 浅色模式" : "🌙 深色模式";

        const mobileBtn = document.getElementById("mobileThemeBtn");
        if (mobileBtn) mobileBtn.innerHTML = isDark ? "☀️" : "🌙";
    };

    // ---------- 新建文件夹 / 上传 ----------

    /** 在 currentFolder 下创建子文件夹（根目录时直接建在顶层）。 */
    window.createFolder = async function () {
        const input = document.getElementById("folderName");
        const folderName = input.value.trim();
        if (!folderName) {
            alert("请输入文件夹名称");
            return;
        }

        const target = currentFolder === "." ? folderName : currentFolder + "/" + folderName;
        const result = await X.postForm("/api/create-folder", { folder_name: target });

        if (result.success) {
            alert("文件夹创建成功");
            input.value = "";
            loadFiles();
        } else {
            alert("创建失败");
        }
    };

    /** 上传当前选择文件到 currentFolder（根目录时 folder 传空串）。 */
    window.uploadFile = async function () {
        const fileInput = document.getElementById("fileInput");
        if (!fileInput.files.length) {
            alert("请选择文件");
            return;
        }

        const fd = new FormData();
        fd.append("folder", currentFolder === "." ? "" : currentFolder);
        fd.append("file", fileInput.files[0]);   // 修复：原实现漏掉 file 字段

        const result = await (await fetch("/api/upload", {
            method: "POST",
            body: fd,
            headers: X.authHeaders(),   // 上传接口受 token 保护
        })).json();
        if (result.success) {
            fileInput.value = "";
            loadFiles();
        } else if (result.message) {
            alert(result.message);
        }
    };

    // ---------- 共享 ----------

    /** 记录待共享对象并弹出共享配置弹窗。type: "file" | "folder"。 */
    window.shareFile = function (path, type) {
        currentShareFile = path;
        currentShareType = type;
        // 按设置预填弹窗默认值（有效期/共享模式），用户仍可在弹窗内改
        const daysSel = document.getElementById("shareDays");
        const wantDays = String(shareDefaults.days);
        if (!Array.from(daysSel.options).some((o) => o.value === wantDays)) {
            const opt = document.createElement("option");
            opt.value = wantDays;
            opt.textContent = wantDays + "天";
            daysSel.appendChild(opt);
        }
        daysSel.value = wantDays;
        document.getElementById("shareMode").value = shareDefaults.mode || "normal";
        new bootstrap.Modal(document.getElementById("shareModal")).show();
    };

    /**
     * 创建共享链接：按弹窗内有效期/密码/模式提交，成功后复制链接并提示。
     * 若后端判定为已存在的有效共享（duplicate），提示文案相应区分。
     */
    window.createShareLink = async function () {
        const password = document.getElementById("sharePassword").value;
        const result = await X.postForm("/api/share", {
            filepath: currentShareFile,
            share_type: currentShareType,
            share_mode: document.getElementById("shareMode").value,
            days: document.getElementById("shareDays").value,
            password,
        });

        if (!result.success) return;

        const shareUrl = window.location.origin + "/share/" + result.share_id;
        await X.copyText(shareUrl);
        const pass = password === "" ? "无密码" : password;

        alert(
            (result.duplicate ? "该文件已存在有效共享链接\n\n" : "共享链接已复制\n\n") +
            shareUrl + "\n\n有效期至：" + result.expire + "\n\n密码：" + pass
        );

        bootstrap.Modal
            .getInstance(document.getElementById("shareModal"))
            .hide();
    };

    /**
     * 渲染统一链接列表（共享 + 访客上传）：拉取 /api/links，
     * 按 kind 区分展示，各自提供"复制链接 / 删除"操作。
     * 链接 ID 为 uuid 片段（字母数字），内联 onclick 引号安全。
     */
    window.loadLinks = async function () {
        const links = await X.getJSON("/api/links");

        document.getElementById("shareCount").innerText =
            Object.keys(links).length + " 个链接";

        const list = document.getElementById("shareList");
        list.innerHTML = "";

        // 排序：共享在前、访客上传在后，组内按到期时间
        const entries = Object.entries(links).sort(([id, a], [id2, b]) => {
            const ca = a.kind === "upload" ? 1 : 0;
            const cb = b.kind === "upload" ? 1 : 0;
            if (ca !== cb) return ca - cb;
            return a.expire.localeCompare(b.expire);
        });

        list.insertAdjacentHTML(
            "beforeend",
            entries.map(([id, link]) => {
                const isUpload = link.kind === "upload";
                // "YYYY-MM-DD HH:MM:SS" 兼容解析（Safari 不支持直接 new Date）
                const [datePart, timePart] = link.expire.split(" ");
                const expired = new Date(datePart + "T" + (timePart || "00:00:00")) < new Date();
                const target = isUpload ? link.folder : link.path;

                return `
                <div class="share-card">
                    <div class="share-card-top">
                        <div class="share-id">${isUpload ? "⬆ 访客上传" : "🔗 共享"} · ${esc(id)}</div>
                        <div class="share-status ${expired ? "share-expired" : "share-active"}">
                            ${expired ? "已过期" : "有效"}
                        </div>
                    </div>
                    <div class="share-path">
                        ${isUpload ? "📥" : (link.type === "folder" ? "📁" : "📄")} ${esc(target)}
                    </div>
                    <div class="share-meta-row">
                        <span class="share-meta">⏰ ${esc(link.expire)}</span>
                        ${isUpload
                            ? `<span class="share-meta">📤 已传 ${link.upload_count || 0}</span>`
                            : `<span class="share-meta">👀 查看 ${link.view_count || 0}</span>`}
                        <span class="share-meta">🔒 ${link.password ? "已设置密码" : "无需密码"}</span>
                        ${!isUpload ? `<span class="share-meta">Ⓜ️ ${esc(link.share_mode || "normal")}</span>` : ""}
                    </div>
                    <div class="share-actions">
                        <button class="btn btn-primary btn-sm"
                            onclick="copyLink('${esc(id)}', ${isUpload})">复制链接</button>
                        <button class="btn btn-outline-primary btn-sm"
                            onclick="showLinkQr('${esc(id)}')">📷 二维码</button>
                        <button class="btn btn-danger btn-sm"
                            onclick="deleteLink('${esc(id)}')">删除</button>
                    </div>
                </div>`;
            }).join("")
        );
    };

    /** 复制链接到剪贴板：共享走 /share/{id}，访客上传走 /upload/{id}。 */
    window.copyLink = async function (id, isUpload) {
        const url = window.location.origin + (isUpload ? "/upload/" : "/share/") + id;
        await X.copyText(url);
        alert("已复制");
    };

    /** 删除某条链接（带确认，统一 /api/delete-link 入口），成功后刷新。 */
    window.deleteLink = async function (id) {
        if (!confirm("删除该链接？")) return;
        await X.postForm("/api/delete-link", { link_id: id });
        loadLinks();
    };

    /**
     * 显示某条链接（共享/访客上传）的二维码：
     * 拉取后端 /api/qr/{id}（返回 PNG），放入 #qrModal 并附上该链接的公开 URL。
     * 二维码内容为"本站该链接的公开路径"，扫码设备需能访问到本机服务地址。
     */
    window.showLinkQr = async function (id) {
        const modal = document.getElementById("qrModal");
        const img = document.getElementById("qrImage");
        const urlText = document.getElementById("qrUrlText");

        modal.querySelector(".modal-title").innerText = "链接二维码";
        img.src = "/api/qr/" + encodeURIComponent(id);
        urlText.innerText = "";

        // 拉取链接记录以显示可复制的完整 URL
        const links = await X.getJSON("/api/links");
        const rec = links[id];
        const isUpload = rec && rec.kind === "upload";
        const publicUrl = window.location.origin + (isUpload ? "/upload/" : "/share/") + id;
        urlText.innerText = publicUrl;
        modal.querySelector(".qr-copy-hint").onclick = async () => {
            await X.copyText(publicUrl);
            urlText.innerText = "已复制到剪贴板 ✓";
            setTimeout(() => { urlText.innerText = publicUrl; }, 1500);
        };

        new bootstrap.Modal(modal).show();
    };

    // ---------- 文件夹权限 ----------

    /** 弹出文件夹权限编辑弹窗：拉取全部用户并渲染勾选列表。 */
    window.showFolderPermission = async function (folder) {
        currentPermFolder = folder;

        const users = await X.getJSON("/api/users");
        document.getElementById("folderPermissionUsers").innerHTML =
            Object.keys(users).map((name) => `
                <label>
                    <input type="checkbox" class="folder-user" value="${esc(name)}">
                    ${esc(name)}
                </label><br>`
            ).join("");

        new bootstrap.Modal(
            document.getElementById("folderPermissionModal")
        ).show();
    };

    /** 保存文件夹权限：收集勾选的用户（空数组表示取消该文件夹访问限制）。 */
    window.saveFolderPermission = async function () {
        const selected = Array.from(
            document.querySelectorAll(".folder-user:checked")
        ).map((item) => item.value);

        await X.postForm("/api/folder-permissions", {
            folder: currentPermFolder,
            users: JSON.stringify(selected),
        });
        alert("权限保存成功");
    };

    // ---------- 访客上传链接 ----------

    /**
     * 为指定文件夹创建访客上传链接：用 prompt 收集有效期与密码，
     * 提交成功后复制生成的 /upload/{id} 链接到剪贴板。
     */
    window.createUploadLink = async function (folder) {
        const days = prompt("有效期(天)", String(shareDefaults.days || 7));
        if (!days) return;
        const password = prompt("访问密码(可空)", "");

        const result = await X.postForm("/api/create-upload-link", {
            folder,
            days,
            password,
        });
        if (!result.upload_id) return;

        const uploadUrl = window.location.origin + "/upload/" + result.upload_id;
        await X.copyText(uploadUrl);
        alert("上传链接已复制\n\n" + uploadUrl);   // 修复：原代码 "\\n" 是字面反斜杠
    };

    // ---------- 用户管理 ----------

    /** 渲染用户表格：用户名 + 删除按钮（用户名经 HTML 转义）。 */
    window.loadUsers = async function () {
        const users = await X.getJSON("/api/users");
        const table = document.getElementById("userTable");

        table.innerHTML = Object.keys(users).map((name) => `
            <tr>
                <td>${esc(name)}</td>
                <td><button class="btn btn-danger btn-sm"
                    onclick="deleteUser('${esc(name)}')">删除</button></td>
            </tr>`).join("");
    };

    /** 新增用户：收集用户名/密码/勾选的权限（.perm-grid），提交后刷新表格。 */
    window.addUser = async function () {
        const permissions = Array.from(
            document.querySelectorAll(".perm-grid input:checked")
        ).map((item) => item.value);

        const result = await X.postForm("/api/add-user", {
            username: document.getElementById("newUsername").value,
            password: document.getElementById("newPassword").value,
            permissions: JSON.stringify(permissions),
        });

        if (result.success) loadUsers();
    };

    /** 删除用户（带确认），成功后刷新表格。 */
    window.deleteUser = async function (username) {
        if (!confirm("确定删除用户？")) return;
        await X.postForm("/api/delete-user", { username });
        loadUsers();
    };

        // ---------- 系统设置 ----------

    /* 二维码"高级感"配色预设（前景 / 背景成对，均取饱和度克制的色板）：
       每项 { label, fg, bg }。fg 为 QR 模块色，bg 为底色；
       保存时写回 qr_style.fg_color / bg_color，后端 /api/qr 据此渲染。 */
    const QR_COLOR_PRESETS = [
        { label: "经典黑 / 纯白", fg: "000000", bg: "FFFFFF" },
        { label: "石墨 / 暖白", fg: "1F2937", bg: "F9FAFB" },
        { label: "靛蓝 / 雾白", fg: "312E81", bg: "EEF2FF" },
        { label: "藏青 / 米白", fg: "1E3A5F", bg: "F7F5F0" },
        { label: "墨绿 / 象牙", fg: "14532D", bg: "F7F8F3" },
        { label: "绛紫 / 浅杏", fg: "4A1D2E", bg: "FBF5F0" },
        { label: "酒红 / 素白", fg: "5A1E2B", bg: "FDF8F7" },
        { label: "金棕 / 宣纸", fg: "5C4520", bg: "FAF6EE" },
        { label: "银灰（反色）", fg: "E5E7EB", bg: "111827" },
        { label: "蓝金（反色）", fg: "93C5FD", bg: "0F172A" },
    ];

    /** 把预设填充到配色下拉框（选项 value = 下标），仅负责选项与选中值；
     *  事件监听器在 DOMContentLoaded 绑定一次，此处不再重复绑定。 */
    function buildQrColorOptions(selectedLabel) {
        const sel = document.getElementById("qrColor");
        sel.innerHTML = QR_COLOR_PRESETS.map((p, i) =>
            `<option value="${i}"${p.label === selectedLabel ? " selected" : ""}>${p.label}</option>`).join("");
        updateQrPreview();
    }

    /** 按当前配色/纠错/模块尺寸刷新右上角"真实二维码预览"
     * （/api/qr-preview 用相同配色渲染一张编码 git 库地址的示例二维码）。 */
    function updateQrPreview() {
        const sel = document.getElementById("qrColor");
        const p = QR_COLOR_PRESETS[Number(sel.value)] || QR_COLOR_PRESETS[0];
        const img = document.getElementById("qrPreview");
        if (!img) return;
        const ec = document.getElementById("qrEc");
        const box = document.getElementById("qrBox");
        const qs = new URLSearchParams({
            fg: p.fg, bg: p.bg,
            ec: ec ? ec.value : "M",
            box: box ? box.value : 8,
        });
        img.src = "/api/qr-preview?" + qs.toString();
    }

    /** 用当前 fg/bg 反查命中的预设 label（无命中返回 null，保持默认第一项）。 */
    function matchQrPreset(fg, bg) {
        const hit = QR_COLOR_PRESETS.find(
            (p) => p.fg.toLowerCase() === String(fg).toLowerCase() &&
                  p.bg.toLowerCase() === String(bg).toLowerCase());
        return hit ? hit.label : null;
    }

    /** 载入站点设置（全部键：站点信息 / 上传与共享 / 安全与性能 / 二维码样式）。 */
    window.loadSettings = async function () {
        const settings = await X.getJSON("/api/settings");
        // 站点信息
        document.getElementById("siteName").value = settings.site_name || "";
        document.getElementById("rootPath").value = settings.root_path || "";
        document.getElementById("publicBaseUrl").value = settings.public_base_url || "";
        document.getElementById("cacheMaxGb").value =
            settings.cache_max_gb === undefined ? 10 : settings.cache_max_gb;
        // 上传与共享
        document.getElementById("uploadMaxMb").value =
            settings.upload_max_mb === undefined ? 100 : settings.upload_max_mb;
        document.getElementById("uploadExtensions").value =
            settings.upload_extensions || "";
        document.getElementById("defaultShareDays").value =
            settings.default_share_days === undefined ? 7 : settings.default_share_days;
        document.getElementById("defaultShareMode").value =
            settings.default_share_mode || "normal";
        // 安全与性能
        document.getElementById("loginMaxFailures").value =
            settings.login_max_failures === undefined ? 5 : settings.login_max_failures;
        document.getElementById("loginCooldownSec").value =
            settings.login_cooldown_sec === undefined ? 300 : settings.login_cooldown_sec;
        document.getElementById("tokenTtlHours").value =
            settings.token_ttl_hours === undefined ? 12 : settings.token_ttl_hours;
        document.getElementById("accessLogMax").value =
            settings.access_log_max === undefined ? 5000 : settings.access_log_max;
        document.getElementById("hotFlushSec").value =
            settings.hot_flush_sec === undefined ? 30 : settings.hot_flush_sec;
        document.getElementById("thumbSizeList").value =
            settings.thumb_size_list === undefined ? 600 : settings.thumb_size_list;
        document.getElementById("thumbSizePreview").value =
            settings.thumb_size_preview === undefined ? 1200 : settings.thumb_size_preview;

        // 二维码样式
        const qs = settings.qr_style || {};
        buildQrColorOptions(matchQrPreset(qs.fg_color || "000000", qs.bg_color || "FFFFFF") ||
            QR_COLOR_PRESETS[0].label);
        document.getElementById("qrEc").value = qs.error_correction || "M";
        document.getElementById("qrBox").value = qs.box_size || 8;

        // 缓存共享/上传弹窗默认值（创建链接时用）
        shareDefaults = {
            days: settings.default_share_days === undefined ? 7 : settings.default_share_days,
            mode: settings.default_share_mode || "normal",
        };
    };

    /** 保存站点设置（以 JSON 提交全部键），成功后提示。 */
    window.saveSettings = async function () {
        const sel = document.getElementById("qrColor");
        const preset = QR_COLOR_PRESETS[Number(sel.value)] || QR_COLOR_PRESETS[0];
        const body = {
            // 站点信息
            site_name: document.getElementById("siteName").value,
            root_path: document.getElementById("rootPath").value,
            public_base_url: document.getElementById("publicBaseUrl").value,
            cache_max_gb: parseInt(document.getElementById("cacheMaxGb").value, 10) || 0,
            // 上传与共享
            upload_max_mb: parseInt(document.getElementById("uploadMaxMb").value, 10) || 0,
            upload_extensions: document.getElementById("uploadExtensions").value.trim(),
            default_share_days: parseInt(document.getElementById("defaultShareDays").value, 10) || 7,
            default_share_mode: document.getElementById("defaultShareMode").value,
            // 安全与性能
            login_max_failures: parseInt(document.getElementById("loginMaxFailures").value, 10) || 5,
            login_cooldown_sec: parseInt(document.getElementById("loginCooldownSec").value, 10) || 0,
            token_ttl_hours: parseInt(document.getElementById("tokenTtlHours").value, 10) || 12,
            access_log_max: parseInt(document.getElementById("accessLogMax").value, 10) || 0,
            hot_flush_sec: parseInt(document.getElementById("hotFlushSec").value, 10) || 30,
            thumb_size_list: parseInt(document.getElementById("thumbSizeList").value, 10) || 600,
            thumb_size_preview: parseInt(document.getElementById("thumbSizePreview").value, 10) || 1200,
            // 二维码样式
            qr_style: {
                fg_color: preset.fg,
                bg_color: preset.bg,
                error_correction: document.getElementById("qrEc").value,
                box_size: parseInt(document.getElementById("qrBox").value, 10) || 8,
                border: 2,
            },
        };
        const res = await fetch("/api/settings", {
            method: "POST",
            headers: Object.assign({ "Content-Type": "application/json" }, X.authHeaders()),
            body: JSON.stringify(body),
        });
        if ((await res.json()).success) alert("保存成功");
    };

    /** 拉取站点设置并刷新页面各处标题（页签/侧边栏/移动端顶栏）；
     *  同时载入共享/上传弹窗的默认值（有效期/模式），供创建链接时预填。 */
    async function loadSiteSetting() {
        const settings = await X.getJSON("/api/settings");
        document.getElementById("pageTitle").innerText = settings.site_name;
        document.getElementById("siteTitle").innerText = settings.site_name;
        document.getElementById("sideTitle").innerText = settings.site_name;
        document.getElementById("mobileSiteTitle").innerText = "📁 " + settings.site_name;
        // 共享/上传弹窗默认值（默认值缺失时用内置缺省，不阻断标题刷新）
        shareDefaults = {
            days: settings.default_share_days === undefined ? 7 : settings.default_share_days,
            mode: settings.default_share_mode || "normal",
        };
    }

    // ---------- 重命名 ----------

    /** 弹出重命名弹窗，默认值取路径末段作为新文件名。 */
    window.renameFile = function (path) {
        renamePath = path;
        document.getElementById("renameInput").value = path.split("/").pop();
        bootstrap.Modal
            .getOrCreateInstance(document.getElementById("renameModal"))
            .show();
    };

    /** 确认重命名：提交原路径与新名（JSON），成功后刷新文件列表并关闭弹窗。 */
    window.confirmRename = async function () {
        const newName = document.getElementById("renameInput").value;
        const res = await fetch("/api/rename", {
            method: "POST",
            headers: Object.assign({ "Content-Type": "application/json" }, X.authHeaders()),
            body: JSON.stringify({ path: renamePath, new_name: newName }),
        });

        if ((await res.json()).success) {
            loadFiles();
            bootstrap.Modal
                .getOrCreateInstance(document.getElementById("renameModal"))
                .hide();
        }
    };

    // ---------- 文件夹 zip 打包（任务卡片） ----------

    /**
     * 打包下载文件夹：先插入进度任务卡片，再轮询后端打包状态，
     * 完成后跳转下载链接、失败则标记卡片。
     * cardId 用时间戳 + 随机串保证同页多任务不冲突。
     */
    window.downloadFolderZip = async function (folderPath) {
        const cardId = "task-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
        createTaskCard(cardId, `📦 正在打包：${folderPath}`);

        const res = await fetch("/api/folder-zip-task/" + encodeURIComponent(folderPath), {
            method: "POST",
            headers: X.authHeaders(),   // 管理面板打包接口受 token 保护
        });
        const data = await res.json();

        X.pollZipTask(data.task_id, { intervalMs: 1500 })
            .then((task) => {
                completeTask(cardId, task.filename);
                // 打包下载是公开 zip 任务（靠 task_id 访问），无需 token
                window.location = "/api/share-folder-zip-download/" + data.task_id;
            })
            .catch(() => failTask(cardId));
    };

    /** 在任务容器底部插入一个"正在打包"的进度卡片。 */
    function createTaskCard(cardId, title) {
        document.getElementById("taskContainer").insertAdjacentHTML("beforeend", `
            <div id="${cardId}" class="task-card">
                <div class="task-title">${esc(title)}</div>
                <div class="task-status">正在打包...</div>
                <div class="task-progress">
                    <div class="task-progress-bar"></div>
                </div>
            </div>`);
    }

    /** 打包完成：卡片显示成功文案，5 秒后自动移除。 */
    function completeTask(cardId, filename) {
        const card = document.getElementById(cardId);
        if (!card) return;

        card.innerHTML = `
            <div class="task-title">✅ ${esc(filename)}</div>
            <div class="task-status">下载已开始</div>`;
        setTimeout(() => card.remove(), 5000);
    }

    /** 打包失败：卡片显示失败文案（保留，便于用户查看）。 */
    function failTask(cardId) {
        const card = document.getElementById(cardId);
        if (!card) return;

        card.innerHTML = '<div class="task-title">❌ 打包失败</div>';
    }
})();
