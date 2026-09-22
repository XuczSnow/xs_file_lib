let currentImageIndex = 0;

function previewFile(path, imageFiles) {

    const ext =
        path
            .split(".")
            .pop()
            .toLowerCase();

    const url =
        "/api/download/" +
        encodeURIComponent(path);

    const preview =
        document.getElementById(
            "previewContent"
        );

    if (!preview) {
        console.error(
            "previewContent not found"
        );
        return;
    }

    preview.innerHTML = "";

    if (["jpg", "jpeg", "png", "gif", "webp", "bmp"].includes(ext)) {

        if (imageFiles != [] || imageFile != null) {
            currentImageIndex =
                imageFiles.findIndex(
                    img => img.path === path
                );
            showCurrentImage();

            const modalElement = document.getElementById("previewModal");
            bootstrap.Modal.getOrCreateInstance(modalElement).show();

            return;
        } else {
            preview.innerHTML = `
                <div class="text-center">
                    <img src="${url}" class="img-fluid rounded" />
                    <div class="mt-3">
                        <a href="${url}" class="btn btn-primary" target="_blank">
                            下载图片
                        </a>
                    </div>
                </div>`;
        }

    } else if (ext === "pdf") {

        preview.innerHTML = `
            <iframe src="${url}" width="100%" height="700"></iframe>`;

    } else if (["mp4", "webm", "ogg"].includes(ext)) {

        preview.innerHTML = `
            <video controls width="100%" class="rounded">
                <source src="${url}">
            </video>`;

    } else if (["mp3", "wav", "m4a", "flac"].includes(ext)) {

        preview.innerHTML = `
            <audio controls style="width:100%">
                <source src="${url}">
            </audio>`;

    } else if (["txt", "log", "md", "json", "csv"].includes(ext)) {

        fetch(url)
            .then(r => r.text())
            .then(text => {
                preview.innerHTML = `
                    <pre style="max-height:700px;overflow:auto;white-space:pre-wrap;">${text}</pre>`;
            });

    } else {

        preview.innerHTML = `
            <div class="alert alert-warning">
                当前文件类型 (${ext}) 不支持在线预览。<br><br>
                <a href="${url}" class="btn btn-primary">
                    下载文件
                </a>
            </div>`;

    }

    const modalElement =
        document.getElementById(
            "previewModal"
        );

    if (modalElement && window.bootstrap) {

        const modal =
            new bootstrap.Modal(
                modalElement
            );

        modal.show();
    }
}

function showCurrentImage() {
    const image = imageFiles[currentImageIndex];
    if (!image) return;

    const url = '/api/download/' + encodeURIComponent(image.path);
    const preview = document.getElementById('previewContent');

    preview.innerHTML = `
        <div class="text-center">
            <div class="mb-3">
                <button class="btn btn-secondary" onclick="prevImage()">← 上一张</button>
                <span class="mx-3">${currentImageIndex + 1} / ${imageFiles.length}</span>
                <button class="btn btn-secondary" onclick="nextImage()">下一张 →</button>
            </div>
            <img src="${url}" class="img-fluid rounded">
            <div class="mt-3">${image.name}</div>
        </div>`;

    // const modalElement = document.getElementById('previewModal');
    // if (modalElement && window.bootstrap) {
    //     new bootstrap.Modal(modalElement).show();
    // }
}

function prevImage() {
    currentImageIndex--;
    if (currentImageIndex < 0) currentImageIndex = imageFiles.length - 1;
    showCurrentImage();
}

function nextImage() {
    currentImageIndex++;
    if (currentImageIndex >= imageFiles.length) currentImageIndex = 0;
    showCurrentImage();
}

document.addEventListener('keydown', function(e) {
    const modal = document.getElementById('previewModal');
    if (!modal || !modal.classList.contains('show')) return;

    if (e.key === 'ArrowLeft') prevImage();
    if (e.key === 'ArrowRight') nextImage();
});
