
let currentShareId = "";

window.onload = async function () {

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

            renderFolder(data);

        // }

    }

}

let imageFiles = [];

function renderFolder(data) {

    imageFiles = [];

    document.querySelector(
        ".share-card"
    ).innerHTML = `
        <div class="folder-header">
            <h4>📁 ${data.name}</h4>
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
        else
        {
            full_path = data.name + "/" + file.name;

            const ext =
                file.name
                    .split(".")
                    .pop()
                    .toLowerCase();

            if (["jpg","jpeg","png","gif","webp","bmp"].includes(ext)) {
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

            <span class="share-file-name" onclick="previewFile('${full_path}', imageFiles)">

                📄 ${file.name}

            </span>

            <a href="/api/share-folder-download/${data.share_id}/${file.name}" class="btn btn-primary btn-sm">

                下载

            </a>

        </div>

        `;

    });

}

function downloadFolderZip(shareId){

    const modal =
        new bootstrap.Modal(
            document.getElementById(
                "zipLoadingModal"
            )
        );

    modal.show();

    fetch(
        "/api/share-folder-zip/" +
        shareId
    )
    .then(res => res.blob())
    .then(blob => {

        const url =
            window.URL.createObjectURL(
                blob
            );

        const a =
            document.createElement(
                "a"
            );

        a.href = url;

        a.download = "folder.zip";

        a.click();

        window.URL.revokeObjectURL(
            url
        );

        modal.hide();

    })
    .catch(() => {

        modal.hide();

        alert(
            "打包失败"
        );

    });

}