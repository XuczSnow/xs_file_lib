
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

        if (data.type === "file") {

            window.location =
                "/api/share-download/" +
                currentShareId;

        }
        else {

            renderFolder(data);

        }

    }

}

function renderFolder(data) {

    document.querySelector(
        ".share-card"
    ).innerHTML = `

        <div class="file-icon">

            📁

        </div>

        <h3 class="text-center">

            文件夹共享

        </h3>

        <div
            class="file-name">

            ${data.folder}

        </div>

        <div id="folderFiles">

        </div>

    `;

    const container =
        document.getElementById(
            "folderFiles"
        );

    data.files.forEach(file => {

        container.innerHTML += `

        <div
            style="
                display:flex;
                justify-content:space-between;
                align-items:center;
                padding:10px;
                border-bottom:1px solid #e5e7eb;
            ">

            <span>

                📄 ${file.name}

            </span>

            <a href="/api/share-folder-download/${data.share_id}/${file.name}" class="btn btn-primary btn-sm">

                下载

            </a>

        </div>

        `;

    });

}
