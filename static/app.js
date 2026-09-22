document.addEventListener("DOMContentLoaded", function () {
    const loginBtn = document.getElementById("loginBtn");
    const uploadBtn = document.getElementById("uploadBtn");
    const searchBox = document.getElementById("search");
    const theme = localStorage.getItem("theme");

    if (theme === "dark") document.body.classList.add("dark");
    if (loginBtn) loginBtn.addEventListener("click", login);
    if (uploadBtn) uploadBtn.addEventListener("click", uploadFile);
    if (searchBox) searchBox.addEventListener("keyup", searchFiles);
});

async function login() {
    const password = document.getElementById("password").value;
    const fd = new FormData();
    fd.append(
        "username",
        document.getElementById(
            "username"
        ).value
    );

    fd.append(
        "password",
        document.getElementById(
            "password"
        ).value
    );

    const response = await fetch("/api/login", {
        method: "POST",
        body: fd
    });

    const result = await response.json();

    if (result.success) {

        localStorage.setItem(
            "username",
            result.username
        );

        // localStorage.setItem(
        //     "role",
        //     result.role
        // );

        document.getElementById(
            "login-page"
        ).style.display = "none";

        document.getElementById(
            "main-page"
        ).style.display = "block";

        document.getElementById(
            "currentUser"
        ).innerText =
            result.username;

        // document.getElementById(
        //     "currentRole"
        // ).innerText =
        //     result.role;

        localStorage.setItem(
            "permissions",
            JSON.stringify(result.permissions)
        );

        initPermission();
        loadFolders();
        loadFiles();
    } else {
        alert("密码错误");
    }
}

function hasPermission(name) {
    const permissions =
        JSON.parse(
            localStorage.getItem("permissions") || "[]"
        );
    return permissions.includes(name);
}

function initPermission() {
    // const role = localStorage.getItem("role");
    const uploadArea = document.getElementById("uploadArea");
    const userManageBtn = document.getElementById("userManageBtn");
    const viewSwitchBtn = document.getElementById("viewSwitchBtn");

    // if (role === "viewer") {
    if (!hasPermission("upload")) {
        if (uploadArea) {
            uploadArea.style.display = "none";
        }
    }

    // if (role !== "admin") {
    if (!hasPermission("user_manage"))
        if (userManageBtn) {
            userManageBtn.style.display = "none";
        }

    if (!hasPermission("share_manage"))
        if (viewSwitchBtn) {
            viewSwitchBtn.style.display = "none";
        }

    // }

}

function showUserManager() {
    const page =
        document.getElementById(
            "userManagerPage"
        );

    if (
        page.style.display === "none" ||
        page.style.display === ""
    ) {
        page.style.display = "block";
        loadUsers();
    } else {
        page.style.display = "none";
    }
}

async function loadUsers() {

    const response =
        await fetch(
            "/api/users"
        );

    const users =
        await response.json();

    const table =
        document.getElementById(
            "userTable"
        );

    table.innerHTML = "";

    Object.keys(users).forEach(name => {

        table.innerHTML += `
        <tr>
            <td>
                ${name}
            </td>
            <!--
                <td>
                    ${users[name].role}
                </td>
            -->
            <td>
                <button
                    class="btn btn-danger btn-sm"
                    onclick="deleteUser('${name}')">

                    删除

                </button>
            </td>
        </tr>

        `;
    });
}

async function addUser() {

    const fd = new FormData();

    const permissions = [];

    document
        .querySelectorAll(
            ".permission-list input:checked"
        )
        .forEach(item => {

            permissions.push(
                item.value
            );

        });

    fd.append(
        "username",
        document.getElementById(
            "newUsername"
        ).value
    );

    fd.append(
        "password",
        document.getElementById(
            "newPassword"
        ).value
    );

    // fd.append(
    //     "role",
    //     document.getElementById(
    //         "newRole"
    //     ).value
    // );

    fd.append(
        "permissions",
        JSON.stringify(
            permissions
        )
    );

    await fetch(
        "/api/add-user",
        {
            method: "POST",
            body: fd
        }
    );

    loadUsers();
}

async function deleteUser(username) {

    if (
        !confirm(
            "确定删除用户？"
        )
    ) {
        return;
    }

    const fd =
        new FormData();

    fd.append(
        "username",
        username
    );

    await fetch(
        "/api/delete-user",
        {
            method: "POST",
            body: fd
        }
    );

    loadUsers();

}

async function uploadFile() {
    const fileInput = document.getElementById("fileInput");
    if (!fileInput || fileInput.files.length === 0) {
        alert("请选择文件");
        return;
    }

    const fd = new FormData();
    fd.append(
        "folder",
        document.getElementById("folderSelect").value
    );

    const response = await fetch("/api/upload", {
        method: "POST",
        body: fd
    });

    const result = await response.json();
    if (result.success) {
        fileInput.value = "";
        loadFiles();
    }
}

let imageFiles = [];

async function loadFiles() {

    const response = await fetch(`/api/files?username=${localStorage.getItem("username")}`);
    const data = await response.json();
    const fileList = document.getElementById("fileList");

    fileList.innerHTML = "";

    Object.keys(data).forEach(folder => {
        // 根目录直接显示文件
        // if (folder === ".") {
        //     data[folder].forEach(file => {
        //         fileList.innerHTML += `
        //         <div class="folder-title">
        //         📁 ...
        //         </div>
        //         <div id="folder-root" class="folder-content">
        //             <div class="file-item">
        //                 <div class="file-name">
        //                     📄 ${file.name}
        //                 </div>
        //                 <div class="file-actions">
        //                     <a href="/api/download/${encodeURIComponent(file.path)}"
        //                     target="_blank"
        //                     class="btn-action">
        //                         下载
        //                     </a>
        //                     <button
        //                         onclick="deleteFile('${file.path}')"
        //                         class="btn-action danger">
        //                         删除
        //                     </button>
        //                 </div>
        //             </div>
        //         </div>
        //         `;
        //     });
        //     return;
        // }

        // 普通文件夹
        const folderDiv = document.createElement("div");
        const folderId = folder.replaceAll("/", "_").replaceAll("\\", "_");
        const folderShare = folder.replaceAll("\\", "/");

        folderDiv.className =
            "folder-card";

        folderDiv.innerHTML = `
            <div
                class="folder-title"
                data-folder="${folderId}"
                onclick="toggleFolder('${folderId}')">
                ▸ 📁 ${folder}
                <div class="folder-actions">
                    ${hasPermission("share") ?
                `
                    <button
                        class="btn-action"
                        onclick="shareFile('${folderShare}','folder')">
                        共享
                    </button>
                    ` : ""}
                    ${hasPermission("share_manage") ?
                `
                    <button
                        class="btn-action"
                        onclick="showFolderPermission('${folder}')">
                        权限
                    </button>
                    ` : ""}
                </div>
            </div>
            <div
                id="folder-${folderId}"
                class="folder-content"
                style="display:none">
            </div>
        `;

        fileList.appendChild(
            folderDiv
        );

        const content =
            folderDiv.querySelector(
                ".folder-content"
            );

        data[folder].forEach(file => {
            const ext =
                file.name
                    .split(".")
                    .pop()
                    .toLowerCase();

            if (["jpg","jpeg","png","gif","webp","bmp"].includes(ext)) {
                imageFiles.push({
                    path: file.path,
                    name: file.name
                });
            }

            content.innerHTML += `
            <div class="file-item">
                <span class="file-name" onclick="previewFile('${file.path}', imageFiles)">
                    ├─ 📄 ${file.name}
                </span>
                <span class="file-actions">
                    ${hasPermission("download") ?
                    `
                    <a href="/api/download/${encodeURIComponent(file.path)}"
                    target="_blank"
                    class="btn-action">
                        下载
                    </a>
                    ` : ""}

                    ${hasPermission("share") ?
                    `
                    <button
                        onclick="shareFile('${file.path}', 'file')"
                        class="btn-action">
                        共享
                    </button>
                    ` : ""}

                    ${hasPermission("delete") ?
                    `
                        <button
                            onclick="deleteFile('${file.path}')"
                            class="btn-action danger">
                            删除
                        </button>
                    ` : ""}
                </span>
            </div>
        `;
        });
    });
}

function toggleFolder(folderId) {

    const content =
        document.getElementById(
            "folder-" + folderId
        );

    const title =
        document.querySelector(
            '[data-folder="' +
            folderId +
            '"]'
        );

    if (content.style.display === "none") {
        content.style.display = "block";
        title.innerHTML =
            title.innerHTML.replace(
                "▸",
                "▾"
            );
    } else {
        content.style.display = "none";
        title.innerHTML =
            title.innerHTML.replace(
                "▾",
                "▸"
            );
    }
}

async function deleteFile(name) {
    if (!confirm("确定删除文件吗？")) return;

    await fetch("/api/delete/" + encodeURIComponent(name), {
        method: "DELETE"
    });

    loadFiles();
}

function searchFiles() {

    const keyword =
        document.getElementById("search")
            .value
            .trim()
            .toLowerCase();

    document
        .querySelectorAll(".folder-card")
        .forEach(folder => {
            const files = folder.querySelectorAll(".file-item");

            let folderMatched = false;

            files.forEach(file => {
                const matched = file.innerText.toLowerCase().includes(keyword);
                file.style.display = matched ? "" : "none";
                if (matched) {
                    folderMatched = true;
                }
            });

            if (keyword === "") {
                folder.style.display = "";
            } else {
                folder.style.display = folderMatched ? "" : "none";
            }
        });
}

function toggleDarkMode() {

    document.body.classList.toggle(
        "dark"
    );

    localStorage.setItem(
        "theme",
        document.body.classList.contains(
            "dark"
        )
            ? "dark"
            : "light"
    );

}

async function loadFolders() {

    const response = await fetch("/api/folders");
    const folders = await response.json();
    const select = document.getElementById("folderSelect");

    select.innerHTML = '<option value="">.</option>';

    folders.forEach(folder => {
        select.innerHTML +=
            `<option value="${folder}">
                ${folder}
            </option>`;
    });
}

async function createFolder() {

    const folderName =
        document.getElementById(
            "folderName"
        ).value;

    if (!folderName) {

        alert("请输入文件夹名称");

        return;
    }

    const formData =
        new FormData();

    formData.append(
        "folder_name",
        folderName
    );

    const response =
        await fetch(
            "/api/create-folder",
            {
                method: "POST",
                body: formData
            }
        );

    const result =
        await response.json();

    if (result.success) {

        alert("文件夹创建成功");

        document.getElementById(
            "folderName"
        ).value = "";

        loadFolders();
        loadFiles();

    } else {

        alert("创建失败");

    }
}

let currentShareFile = "";
let currentShareType = "";

function shareFile(path, type) {
    currentShareFile = path;
    currentShareType = type;
    const modal =
        new bootstrap.Modal(document.getElementById("shareModal"));
    modal.show();
}

document.addEventListener(
    "DOMContentLoaded",
    () => {

        const shareBtn =
            document.getElementById(
                "createShareBtn"
            );

        if (shareBtn) {

            shareBtn.addEventListener(
                "click",
                createShareLink
            );

        }

    }
);

async function createShareLink() {

    const days = document.getElementById("shareDays").value;
    const password = document.getElementById("sharePassword").value
    const fd = new FormData();

    fd.append("filepath", currentShareFile);
    fd.append("share_type", currentShareType);
    fd.append("days", days);
    fd.append("password", password);

    const response =
        await fetch(
            "./api/share",
            {
                method: "POST",
                body: fd
            }
        );

    const result =
        await response.json();

    if (result.success) {
        const shareUrl =
            window.location.origin +
            "/share/" +
            result.share_id;

        navigator.clipboard.writeText(
            shareUrl
        );

        alert(
            "共享链接已复制\n\n" +
            shareUrl +
            "\n\n有效期至：\n" +
            result.expire +
            "\n\n密码：" +
            password
        );


        bootstrap.Modal
            .getInstance(
                document.getElementById(
                    "shareModal"
                )
            )
            .hide();

    }
}

let currentView = "file";

function toggleShareView() {

    const filePage =
        document.getElementById(
            "filePage"
        );

    const sharePage =
        document.getElementById(
            "shareManagerPage"
        );

    const btn =
        document.getElementById(
            "viewSwitchBtn"
        );

    if (currentView === "file") {

        filePage.style.display =
            "none";

        sharePage.style.display =
            "block";

        btn.innerHTML =
            "📁 返回文件";

        currentView =
            "share";

        loadShares();

    } else {

        sharePage.style.display =
            "none";

        filePage.style.display =
            "block";

        btn.innerHTML =
            "🔗 共享管理";

        currentView =
            "file";

    }

}

async function loadShares() {

    const response =
        await fetch("./api/shares");

    const shares =
        await response.json();

    document.getElementById(
        "shareCount"
    ).innerText =

        Object.keys(shares).length +
        " 个共享";

    const list =
        document.getElementById(
            "shareList"
        );

    list.innerHTML = "";

    Object.keys(shares).forEach(id => {

        const share =
            shares[id];

        const expired =

            new Date(
                share.expire
            ) < new Date();

        list.innerHTML += `

        <div class="share-card">

            <div class="share-card-top">

                <div class="share-id">

                    ID: ${id}

                </div>

                <div class="
                    share-status
                    ${expired
                ? "share-expired"
                : "share-active"
            }
                ">

                    ${expired
                ? "已过期"
                : "有效"
            }

                </div>

            </div>

            <div class="share-path">

                ${share.type === "folder" ? "📁" : "📄"} ${share.path}

            </div>

            <div class="share-meta">

                ⏰ 到期时间：
                ${share.expire}

            </div>

            <div class="share-meta">

                🔒 ${share.password
                ? "已设置密码"
                : "无需密码"
            }

            </div>

            <div class="share-actions">

                <button
                    class="btn btn-primary btn-sm"
                    onclick="copyShare('${id}')">

                    复制链接

                </button>

                <button
                    class="btn btn-danger btn-sm"
                    onclick="deleteShare('${id}')">

                    取消共享

                </button>

            </div>

        </div>

        `;

    });

}

function copyShare(id) {

    const url =

        window.location.origin +

        "/share/" +

        id;

    navigator.clipboard
        .writeText(url);

    alert("已复制");

}

async function deleteShare(id) {

    if (
        !confirm("删除共享？")
    ) {
        return;
    }

    const fd =
        new FormData();

    fd.append(
        "share_id",
        id
    );

    await fetch(
        "./api/delete-share",
        {
            method: "POST",
            body: fd
        }
    );

    loadShares();
}

let currentFolder = "";

async function showFolderPermission(folder) {

    currentFolder = folder;

    const users =
        await fetch(
            "./api/users"
        ).then(
            r => r.json()
        );

    let html = "";

    Object.keys(users)
        .forEach(name => {

            html += `

        <label>

            <input
                type="checkbox"
                class="folder-user"
                value="${name}">

            ${name}

        </label>

        <br>

        `;

        });

    document.getElementById(
        "folderPermissionUsers"
    ).innerHTML = html;

    new bootstrap.Modal(
        document.getElementById(
            "folderPermissionModal"
        )
    ).show();
}

async function saveFolderPermission() {

    const selected = [];

    document
        .querySelectorAll(
            ".folder-user:checked"
        )
        .forEach(item => {

            selected.push(
                item.value
            );

        });

    const fd =
        new FormData();

    fd.append(
        "folder",
        currentFolder
    );

    fd.append(
        "users",
        JSON.stringify(
            selected
        )
    );

    await fetch(
        "./api/folder-permissions",
        {
            method: "POST",
            body: fd
        }
    );

    alert(
        "权限保存成功"
    );

}

