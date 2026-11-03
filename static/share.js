
let currentShareId = "";

async function loadSiteSetting() {

    const response =
        await fetch(
            "/api/settings"
        );

    const settings = await response.json();

    document.getElementById("pageTitle")
        .innerText = settings.site_name;
}

window.onload = async function () {

    loadSiteSetting()

    currentShareId =
        window.location.pathname
            .split("/")
            .pop();

    const response =
        await fetch(
            "/api/share-info/" +
            currentShareId
        );

    const result =
        await response.json();

    if (!result.success) {

        document.getElementById(
            "shareFileName"
        ).innerText =
            "共享链接不存在";

        return;
    }

    document.getElementById(
        "shareFileName"
    ).innerText =
        result.name;



    document.getElementById(
        "shareExpire"
    ).innerText =
        "⏰ 有效期至：" +
        result.expire;

    document.querySelector(
        ".file-icon"
    ).innerText =
        result.type === "folder" ? "📁" : "📄";

    document.querySelector(
        ".text-center"
    ).innerText =
        result.type === "folder" ? "文件夹共享" : "文件共享";

    document.getElementById(
        "sharePasswordStatus"
    ).innerText =
        result.has_password
            ? "🔒 已启用提取码"
            : "🔓 无需密码";

    if (!result.has_password) {

        document
            .getElementById(
                "sharePassword"
            )
            .style.display =
            "none";

    }

    if (result.expired) {

        document.getElementById(
            "errorText"
        ).innerText =
            "共享链接已过期";

        document.querySelector(
            ".download-btn"
        ).disabled = true;
    }

};

async function verifyShare() {

    const fd =
        new FormData();

    fd.append(
        "share_id",
        currentShareId
    );

    fd.append(
        "password",
        document.getElementById(
            "sharePassword"
        ).value
    );

    const response =
        await fetch(
            "/api/share-check",
            {
                method: "POST",
                body: fd
            }
        );

    const result =
        await response.json();

    if (result.success) {

        const info =
            await fetch(
                "/api/share-content/" +
                currentShareId
            );

        const data =
            await info.json();

        // if (data.type === "file") {

        //     window.location =
        //         "/api/share-download/" +
        //         currentShareId;

        // }
        // else {

        if (data.share_mode === "album")
            renderAlbum(data);
        else
            renderFolder(data);

        // }

    }

}

let imageFiles = [];

function formatSize(bytes) {
    if (bytes < 1024)
        return bytes + " B";

    if (bytes < 1024 * 1024)
        return (bytes / 1024).toFixed(1) + " KB";

    if (bytes < 1024 * 1024 * 1024)
        return (bytes / 1024 / 1024).toFixed(1) + " MB";

    return (bytes / 1024 / 1024 / 1024).toFixed(1) + " GB";
}

function renderFolder(data) {

    imageFiles = [];

    const share_card = document.getElementById("shareCard");
    const file_card = document.getElementById("fileCard");

    share_card.style.display = "none";
    file_card.style.display = "block";

    file_card.innerHTML = `
        <div class="folder-header">
            <h4>${data.name}</h4>
        </div>

        <div class="folder-download">
            <button class="btn btn-primary btn-sm"
                onclick="downloadFolderZip('${data.share_id}')">
                📦 打包下载所有文件
            </button>
        </div>

        <div id="folderFiles">

        </div>
    `;

    const container =
        document.getElementById(
            "folderFiles"
        );

    data.files.forEach(file => {
        let full_path = ""
        if (data.type === "file")
            full_path = data.path;
        else {
            full_path = data.name + "/" + file.name;

            const ext =
                file.name
                    .split(".")
                    .pop()
                    .toLowerCase();

            if (["jpg", "jpeg", "png", "gif", "webp", "bmp"].includes(ext)) {
                imageFiles.push({
                    path: full_path,
                    name: file.name
                });
            }
        }

        container.innerHTML += `

        <div
            style="
                display:flex;
                justify-content:space-between;
                align-items:center;
                padding:10px;
                border-bottom:1px solid #e5e7eb;
            ">

            <div class="share-file-name" onclick="previewFile('${full_path}', imageFiles)">
                📄 ${file.name}
            </div>

            <div class="share-file-tags">
                <div class="share-file-tag">
                    ${formatSize(file.size)}
                </div>
            </div>

            <a href="/api/share-folder-download/${data.share_id}/${file.name}" class="btn btn-primary btn-sm">
                下载
            </a>

        </div>

        `;

    });

}

async function downloadFolderZip(shareId) {

    const modal =
        new bootstrap.Modal(
            document.getElementById(
                "zipLoadingModal"
            )
        );

    modal.show();

    const res =
        await fetch(
            "/api/share-folder-zip-task/" +
            shareId,
            {
                method: "POST"
            }
        );

    const data = await res.json();

    checkZipStatus(data.task_id);
}

function checkZipStatus(taskId) {

    const timer =
        setInterval(
            async () => {
                const res =
                    await fetch(
                        "/api/share-folder-zip-status/" +
                        taskId
                    );

                const data = await res.json();

                if (data.status === "completed") {

                    clearInterval(timer);

                    bootstrap.Modal
                        .getInstance(
                            document.getElementById(
                                "zipLoadingModal"
                            )
                        )
                        .hide();

                    window.location =
                        "/api/share-folder-zip-download/" +
                        taskId;

                }

                else if (data.status === "failed") {

                    clearInterval(timer);
                    alert(data.message);
                }
            },
            1000
        );
}

function renderAlbum(data) {

    imageFiles = []

    const share_card = document.getElementById("shareCard");
    const container = document.getElementById("albumCard");

    share_card.style.display = "none";
    container.style.display = "block";

    let imagesHtml = "";
    let videosHtml = "";

    let imageCount = 0;
    let videoCount = 0;

    data.files.forEach(file => {

        const ext =
            file.name
                .split(".")
                .pop()
                .toLowerCase();

        const url =
            "/api/share-folder-download/" +
            data.share_id +
            "/" +
            encodeURIComponent(
                file.name
            );

        const filePath =
            data.name +
            "/" +
            file.name;

        const imageTypes = [
            "jpg",
            "jpeg",
            "png",
            "gif",
            "webp",
            "bmp"
        ];

        const videoTypes = [
            "mp4",
            "webm",
            "ogg"
        ];

        if (imageTypes.includes(ext)) {
            imageCount++;
            const thumbUrl =
                "/api/thumbnail/" +
                encodeURIComponent(
                    data.name +
                    "/" +
                    file.name
                );

            imageFiles.push({
                path: filePath,
                name: file.name
            });

            imagesHtml += `

            <div
                class="album-item"
                onclick="
                    previewFile('${filePath}', imageFiles)
                ">

                <img
                    src="${thumbUrl}"
                    loading="lazy">

                <div class="album-item-title">
                ${file.name}
                </div>

            </div>

            `;
        }

        else if (videoTypes.includes(ext)) {
            videoCount++;
            const thumbUrl =
                "/api/video-thumbnail/" +
                encodeURIComponent(
                    data.name +
                    "/" +
                    file.name
                );

            videosHtml += `

            <div
                class="video-card",
                onclick="
                    window.open(
                    '/share/video/' +
                    '${data.share_id}' +
                    '/' +
                    '${encodeURIComponent(file.name)}',
                    '_blank')
                ">

                <img
                    src="${thumbUrl}"
                    loading="lazy">

                <div
                    class="video-title">

                    🎬 ${file.name}

                </div>

            </div>

            `;
        }

    });

    // container.innerHTML = `

    //     <div class="album-header">

    //         <h2>

    //             ${data.name}

    //         </h2>

    //     </div>

    let coverUrl = "";

    if (imageFiles.length > 0) {
        const coverFile =
            imageFiles[Math.floor(Math.random() * imageFiles.length)];

        coverUrl = "/api/preview-image/" +
            encodeURIComponent(coverFile.path);
    }

    container.innerHTML = `

        <div class="album-banner">
            <img
                class="album-cover-image"
                src="${coverUrl}">

            <div class="album-overlay">
                <div class="album-title">
                    ${data.name}
                </div>

                <div class="album-stats">
                    ${imageCount}张图片
                    ·
                    ${videoCount}个视频
                </div>
            </div>
        </div>
        <div class="album-switch">
            <div
                id="showPhotosBtn"
                class="album-entry active"
                onclick="showPhotos()">

                <div class="entry-icon">
                    🖼
                </div>
                <div class="entry-title">
                    图片
                </div>
                <div class="entry-count">
                    ${imageCount} 张
                </div>
            </div>

            <div
                id="showVideosBtn"
                class="album-entry"
                onclick="showVideos()">

                <div class="entry-icon">
                    🎬
                </div>
                <div class="entry-title">
                    视频
                </div>
                <div class="entry-count">
                    ${videoCount} 个
                </div>
            </div>
        </div>
        ${imagesHtml
            ?
            `
                <div class="album-section-title" id="photoSection">

                    🖼 图片

                </div>

                <div class="album-grid">

                    ${imagesHtml}

                </div>
                `
            :
            ""
        }

        ${videosHtml
            ?
            `
                <div class="album-section-title" id="videoSection">

                    🎬 视频

                </div>

                <div class="video-grid">

                    ${videosHtml}

                </div>
                `
            :
            ""
        }

    `;

}

function showPhotos() {

    document
        .getElementById("photoSection")
        .scrollIntoView({
            behavior: "smooth",
            block: "start"
        });
}

function showVideos() {
    document
        .getElementById("videoSection")
        .scrollIntoView({
            behavior: "smooth",
            block: "start"
        });
}