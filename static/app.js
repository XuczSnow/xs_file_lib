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

        localStorage.setItem(
            "role",
            result.role
        );

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

        document.getElementById(
            "currentRole"
        ).innerText =
            result.role;

        initPermission();
        loadFolders();
        loadFiles();
    } else {
        alert("密码错误");
    }
}

function initPermission() {
    const role =
        localStorage.getItem("role");
    const uploadArea =
        document.getElementById(
            "uploadArea"
        );
    const userManageBtn =
        document.getElementById(
            "userManageBtn"
        );

    if (role === "viewer") {

        if (uploadArea) {
            uploadArea.style.display =
                "none";
        }

    }

    if (role !== "admin") {

        if (userManageBtn) {
            userManageBtn.style.display =
                "none";
        }

    }

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
            <td>
                ${users[name].role}
            </td>
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

    fd.append(
        "role",
        document.getElementById(
            "newRole"
        ).value
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

async function loadFiles() {

    const response = await fetch("/api/files");
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
        const folderDiv =document.createElement("div");
        const folderId = folder.replaceAll("/", "_").replaceAll("\\", "_");

        folderDiv.className =
            "folder-card";

        folderDiv.innerHTML = `
            <div
                class="folder-title"
                data-folder="${folderId}"
                onclick="toggleFolder('${folderId}')">
                ▸ 📁 ${folder}
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
            content.innerHTML += `
            <div class="file-item">
                <span class="file-name">
                    ├─ 📄 ${file.name}
                </span>
                <span class="file-actions">
                    <a href="/api/download/${encodeURIComponent(file.path)}"
                    target="_blank"
                    class="btn-action">
                        下载
                    </a>

                    ${localStorage.getItem("role") === "admin" ?
                    `
                        <button
                            onclick="deleteFile('${file.path}')"
                            class="btn-action danger">
                            删除
                        </button>
                        `
                    :
                    ""
                }
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

    } else {

        alert("创建失败");

    }
}


