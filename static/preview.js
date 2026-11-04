/**
 * 文件预览（图片灯箱 / 视频 / 音频 / PDF / 文本）。
 *
 * 依赖 common.js（XSLib）提供的扩展名白名单与转义工具。
 * 被 index.html、share.html 共用：两个页面各有一个 #previewModal +
 * #previewContent 容器，本脚本统一负责填充其内容并弹窗。
 *
 * 预览分两种形态：
 *   1. 单文件预览——直接渲染 <img>/<video>/<audio>/<iframe>/<pre>；
 *   2. 图片灯箱——当传入 imageFiles（同组可预览图片）时，
 *      显示可左右切换的灯箱，用缩略图接口（/api/preview-image）
 *      加载大图、原图下载走 /api/download。
 *
 * 灯箱当前列表与索引保存在 previewImageFiles / currentImageIndex，
 * 由调用方（app.js / share.js）通过 previewFile(path, imageFiles) 传入。
 */
(function () {
    "use strict";

    const X = window.XSLib;
    const esc = X.escapeHtml;

    /* 灯箱状态：当前图片组与所在索引（左右切换时增减） */
    let previewImageFiles = [];
    let currentImageIndex = 0;

    // 供 HTML 内联属性（onclick）与键盘事件调用的全局函数
    window.previewFile = previewFile;
    window.prevImage = prevImage;
    window.nextImage = nextImage;
    window.openFullImage = openFullImage;

    /**
     * 打开预览。
     * @param {string} path 文件相对路径（后端 UPLOAD_DIR 下）
     * @param {Array<{path:string,name:string}>} [imageFiles]
     *   可选，当前可预览图片组；传入且非空时启用灯箱模式。
     */
    function previewFile(path, imageFiles) {
        const ext = X.getExt(path);
        const url = "/api/download/" + encodeURIComponent(path);
        const preview = document.getElementById("previewContent");

        if (!preview) {
            console.error("previewContent not found");
            return;
        }

        preview.innerHTML = "";

        /*
         * 按扩展名分发到对应渲染器。注意：各分支共用上面拼好的
         * 下载 url；图片进入灯箱时先 return（灯箱内自行弹窗），
         * 其余分支渲染完成后统一 openModal()。
         */
        if (X.IMAGE_EXTS.includes(ext)) {
            // 有图片组 → 灯箱模式（带前后切换）
            if (imageFiles && imageFiles.length > 0) {
                previewImageFiles = imageFiles;
                currentImageIndex = Math.max(
                    0,
                    imageFiles.findIndex((img) => img.path === path)
                );
                showCurrentImage();
                openModal();
                return;
            }

            // 无图片组 → 单图预览 + 下载按钮
            preview.innerHTML = `
                <div class="text-center">
                    <img src="${url}" class="img-fluid rounded" />
                    <div class="mt-3">
                        <a href="${url}" class="btn btn-primary" target="_blank">
                            下载图片
                        </a>
                    </div>
                </div>`;
        } else if (ext === "pdf") {
            // PDF 用 <iframe> 内嵌浏览器原生查看器
            preview.innerHTML =
                `<iframe src="${url}" width="100%" height="700"></iframe>`;
        } else if (X.VIDEO_EXTS.includes(ext)) {
            // 视频：普通 <video> 直接请求 /api/download（浏览器会发 Range）
            preview.innerHTML = `
                <video controls width="100%" class="rounded">
                    <source src="${url}">
                </video>`;
        } else if (X.AUDIO_EXTS.includes(ext)) {
            preview.innerHTML = `
                <audio controls style="width:100%">
                    <source src="${url}">
                </audio>`;
        } else if (X.TEXT_EXTS.includes(ext)) {
            // 文本类（txt/md/json/csv 等）：拉取内容放入 <pre>。
            // 用 textContent 赋值而非拼 innerHTML，避免内容 XSS。
            fetch(url)
                .then((r) => r.text())
                .then((text) => {
                    preview.innerHTML =
                        '<pre style="max-height:700px;overflow:auto;white-space:pre-wrap;"></pre>';
                    preview.querySelector("pre").textContent = text;
                });
        } else {
            // 未知类型：提示不支持 + 提供下载
            preview.innerHTML = `
                <div class="alert alert-warning">
                    当前文件类型 (${esc(ext)}) 不支持在线预览。<br><br>
                    <a href="${url}" class="btn btn-primary">
                        下载文件
                    </a>
                </div>`;
        }

        openModal();
    }

    /** 打开 #previewModal（bootstrap 实例不存在时创建）。 */
    function openModal() {
        const modal = document.getElementById("previewModal");
        if (modal && window.bootstrap) {
            new bootstrap.Modal(modal).show();
        }
    }

    /**
     * 渲染灯箱当前图片：顶部工具栏（上一张/计数/下一张）、
     * 中央缩略图（点击 openFullImage 打开原图）、底部下载原图。
     * 缩略图走 /api/preview-image（1200 短边），下载走原图接口。
     */
    window.showCurrentImage = function () {
        const image = previewImageFiles[currentImageIndex];
        if (!image) return;

        const previewUrl = "/api/preview-image/" + encodeURIComponent(image.path);
        const downloadUrl = "/api/download/" + encodeURIComponent(image.path);

        document.getElementById("previewContent").innerHTML = `
            <div class="lightbox-container">
                <div class="lightbox-toolbar">
                    <button class="btn btn-outline-secondary btn-sm" onclick="prevImage()">
                        ◀ 上一张
                    </button>
                    <div class="lightbox-info">
                        <span class="image-counter">
                            ${currentImageIndex + 1} / ${previewImageFiles.length}
                        </span>
                        <span class="image-name">${esc(image.name)}</span>
                    </div>
                    <button class="btn btn-outline-secondary btn-sm" onclick="nextImage()">
                        下一张 ▶
                    </button>
                </div>
                <div class="lightbox-image-wrapper">
                    <img src="${previewUrl}" class="lightbox-image"
                        onclick="openFullImage('${downloadUrl}')">
                </div>
                <div class="lightbox-footer">
                    <a href="${downloadUrl}" class="btn btn-primary" target="_blank">
                        ⬇ 下载原图
                    </a>
                </div>
            </div>`;
    };

    /** 灯箱图片点击：在新标签打开原图（绕过浏览器缓存/预览限制）。 */
    function openFullImage(url) {
        window.open(url, "_blank");
    }

    /** 上一张：索引减一，越界时回绕到末尾（环形切换）。 */
    function prevImage() {
        currentImageIndex--;
        if (currentImageIndex < 0) currentImageIndex = previewImageFiles.length - 1;
        showCurrentImage();
    }

    /** 下一张：索引加一，越界时回绕到开头（环形切换）。 */
    function nextImage() {
        currentImageIndex++;
        if (currentImageIndex >= previewImageFiles.length) currentImageIndex = 0;
        showCurrentImage();
    }

    /*
     * 键盘左右切换（仅当预览弹窗可见时生效，避免误触全局导航）：
     *   ← 上一张 / → 下一张
     */
    document.addEventListener("keydown", (e) => {
        const modal = document.getElementById("previewModal");
        if (!modal || !modal.classList.contains("show")) return;

        if (e.key === "ArrowLeft") prevImage();
        if (e.key === "ArrowRight") nextImage();
    });
})();
