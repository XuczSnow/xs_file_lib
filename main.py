from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from datetime import datetime, timedelta

import os
import json
import uuid
import shutil
import hashlib
import zipfile
import tempfile

app = FastAPI()

UPLOAD_DIR = "./uploads"
USER_CONF = "./config/users.json"
SHARE_CONF = "./config/shares.json"
FPRMS_CONF = "./config/folder_permissions.json"
ACCESS_LOG_CONF = "./config/share_access_log.json"
UPLOAD_LINK_CONF = "./config/upload_links.json"

os.makedirs(
    UPLOAD_DIR,
    exist_ok=True
)

def hash_password(password: str):
    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()

def write_share_access_log(data):

    try:

        with open(
            ACCESS_LOG_CONF,
            "r",
            encoding="utf-8"
        ) as f:

            logs = json.load(f)

    except:

        logs = []

    logs.append(data)

    with open(
        ACCESS_LOG_CONF,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            logs,
            f,
            indent=4,
            ensure_ascii=False
        )

def init_config():

    # 用户配置

    if not os.path.exists(USER_CONF):
        with open(USER_CONF,"w",encoding="utf-8") as f:
            json.dump(
                {
                    "admin": {

                        "password":
                            hash_password(
                                "admin123"
                            ),

                        "permissions": [
                            "upload",
                            "download",
                            "delete",
                            "share",
                            "share_manage",
                            "user_manage"
                        ]

                    }
                },
                f,
                indent=4,
                ensure_ascii=False
            )

    # 共享配置
    if not os.path.exists(SHARE_CONF):
        with open(SHARE_CONF,"w",encoding="utf-8") as f:
            json.dump(
                {},
                f,
                indent=4,
                ensure_ascii=False
            )

    # 文件夹权限配置
    if not os.path.exists(FPRMS_CONF):
        with open(FPRMS_CONF,"w",encoding="utf-8") as f:
            json.dump(
                {},
                f,
                indent=4,
                ensure_ascii=False
            )
    
    # 文件共享log   
    if not os.path.exists(ACCESS_LOG_CONF):
        with open(
            ACCESS_LOG_CONF,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                [],
                f,
                indent=4,
                ensure_ascii=False
            )
            
    # 上传配置
    if not os.path.exists(UPLOAD_LINK_CONF):
        with open(
            UPLOAD_LINK_CONF,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump({},f)

app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static"
)

init_config()

@app.get("/")
def home():
    return FileResponse(
        "static/index.html"
    )


@app.post("/api/login")
def login(
    username: str = Form(...),
    password: str = Form(...)
):

    with open(
        USER_CONF,
        "r",
        encoding="utf-8"
    ) as f:

        users = json.load(f)

    if username not in users:
        return {"success": False}

    if (
        users[username]["password"]
        != hash_password(password)
    ):
        return {
            "success": False
        }

    return {
        "success": True,
        "username": username,
        # "role": users[username]["role"],
        "permissions":
            users[username]
            .get(
                "permissions",
                []
            )
    }
    
@app.get("/api/users")
def get_users():
    with open(USER_CONF,"r",encoding="utf-8") as f:
        return json.load(f)
    
@app.post("/api/change-password")
def change_password(
    username: str = Form(...),
    new_password: str = Form(...)
):
    with open(USER_CONF,"r",encoding="utf-8") as f:
        users = json.load(f)
    users[username]["password"] = hash_password(new_password)
    
    with open(USER_CONF,"w",encoding="utf-8") as f:
        json.dump(
            users,
            f,
            indent=4,
            ensure_ascii=False
        )

    return {
        "success": True
    }

@app.post("/api/add-user")
def add_user(
    username: str = Form(...),
    password: str = Form(...),
    # role: str = Form(...)
    permissions: str = Form("[]")
):

    with open(USER_CONF,"r",encoding="utf-8") as f:
        users = json.load(f)

    users[username] = {
        "password": hash_password(password),
        # "role": role,
        "permissions":
            json.loads(
                permissions
            )
    }

    with open(USER_CONF,"w",encoding="utf-8") as f:
        json.dump(
            users,
            f,
            indent=4,
            ensure_ascii=False
        )

    return {
        "success": True
    }

@app.post("/api/delete-user")
def delete_user(
    username: str = Form(...)
):

    with open(USER_CONF,"r",encoding="utf-8") as f:
        users = json.load(f)

    if username in users:
        del users[username]

    with open(USER_CONF,"w",encoding="utf-8") as f:
        json.dump(
            users,
            f,
            indent=4,
            ensure_ascii=False
        )

    return {
        "success": True
    }
    
@app.get("/api/folder-permissions")
def get_folder_permissions():
    try:
        with open(FPRMS_CONF, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}
    
@app.post("/api/folder-permissions")
def set_folder_permissions(
    folder: str = Form(...),
    users: str = Form(...)

):
    try:
        with open(FPRMS_CONF,"r",encoding="utf-8") as f:
            permissions = json.load(f)
    except:
        permissions = {}
        
    if len(users) == 0:
        permissions.pop(folder, None)
    else:
        permissions[folder] = json.loads(users)

    with open(FPRMS_CONF,"w",encoding="utf-8") as f:
        json.dump(
            permissions,
            f,
            indent=4,
            ensure_ascii=False
        )
        
    return {"success": True}

@app.get("/api/files")
def get_files(username: str):

    result = {}
    
    with open(FPRMS_CONF,"r",encoding="utf-8") as f:
        folder_permissions = json.load(f)

    for root, dirs, files in os.walk(UPLOAD_DIR):
        rel_dir = os.path.relpath(root,UPLOAD_DIR)
        
        folder_name = rel_dir#.split("/")[0]
        
        if folder_name in folder_permissions:
            allowed_users = folder_permissions[folder_name]

            if allowed_users and username not in allowed_users:
                continue

        # if rel_dir == ".":
        #     rel_dir_name = "根目录"
        # else:
        #     rel_dir_name = rel_dir

        result[rel_dir] = {}
        
        folder_size = 0
        file_count = 0
        folder_count = len(dirs)
        result[rel_dir]["files"] = []

        for file in files:
            full_path = os.path.join(root, file)
            folder_size += os.path.getsize(full_path)
            file_count += 1
            result[rel_dir]["files"].append({
                "name": file,
                "path": os.path.join(rel_dir, file).replace("\\", "/"),
                "size": os.path.getsize(full_path),
                "modified": datetime.fromtimestamp(os.path.getmtime(full_path))
                                    .strftime("%Y-%m-%d %H:%M"),
                "extension": os.path.splitext(file)[1][1:].lower()
            })
            
        result[rel_dir]["_meta"] = {
            "file_count": file_count,
            "folder_count": folder_count,
            "size": folder_size
            }

    return result


@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    folder: str = Form("")
):

    target_dir = os.path.join(
        UPLOAD_DIR,
        folder
    )

    os.makedirs(
        target_dir,
        exist_ok=True
    )

    save_path = os.path.join(
        target_dir,
        file.filename
    )

    with open(save_path,"wb") as buffer:
        shutil.copyfileobj(
            file.file,
            buffer
        )

    return {
        "success": True
    }


@app.delete(
    "/api/delete/{filepath:path}"
)
def delete(filepath: str):

    path = os.path.join(
        UPLOAD_DIR,
        filepath
    )

    if os.path.exists(path):
        os.remove(path)

    return {
        "success": True
    }


@app.get("/api/download/{filepath:path}")
def download(filepath: str):

    return FileResponse(
        os.path.join(
            UPLOAD_DIR,
            filepath
        )
    )

@app.get("/api/folders")
def get_folders():

    folders = []

    for item in os.listdir(UPLOAD_DIR):

        path = os.path.join(UPLOAD_DIR,item)

        if os.path.isdir(path):
            folders.append(item)

    return folders
    
@app.post("/api/create-folder")
def create_folder(folder_name: str = Form(...)):

    path = os.path.join(
        UPLOAD_DIR,
        folder_name
    )

    os.makedirs(
        path,
        exist_ok=True
    )

    return {
        "success": True
    }
    
@app.post("/api/share")
def create_share(
    filepath: str = Form(...),
    share_type: str = Form(...),
    days: int = Form(...),
    password: str = Form("")
):

    share_id = str(uuid.uuid4())[:8]
    expire_time = datetime.now() + timedelta(days=days)

    try:
        with open(SHARE_CONF,"r",encoding="utf-8") as f:
            shares = json.load(f)
    except:
        shares = {}

    shares[share_id] = {
        "path": filepath,
        "type": share_type,
        "view_count": 0,
        "expire":
            expire_time.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        "password":
            hash_password(password)
            if password
            else ""
        }
    
    for sid, share in shares.items():
        if share["path"] == filepath:

            expire_time = datetime.strptime(
                share["expire"],
                "%Y-%m-%d %H:%M:%S"
            )

            if expire_time > datetime.now():
                return {
                    "success": True,
                    "share_id": sid,
                    "expire": share["expire"],
                    "duplicate": True
                }

    with open(SHARE_CONF,"w",encoding="utf-8") as f:
        json.dump(shares,f,indent=4,ensure_ascii=False)

    return {
        "success": True,
        "share_id": share_id,
        "expire":
            shares[share_id]["expire"]
    }


@app.get("/share/{share_id}")
def access_share(share_id: str):

    with open(SHARE_CONF,"r",encoding="utf-8") as f:
        shares = json.load(f)

    if share_id not in shares:
        return {"success": False}

    return FileResponse("static/share.html")

@app.get(
    "/api/share-content/{share_id}"
)
def share_content(share_id: str):
    files = []
    with open(SHARE_CONF,"r",encoding="utf-8") as f:
        shares = json.load(f)

    share = shares[share_id]

    if share["type"] == "file":
        files.append({
            "name": os.path.basename(share["path"]),
            "size": os.path.getsize(share["path"])
            })
        return {
            "success": True,
            "type":"file",
            "share_id": share_id,
            "path": share["path"],
            "name": os.path.basename(share["path"]),
            "files": files
        }

    folder_path = os.path.join(
        UPLOAD_DIR,
        share["path"]
    )

    for file in os.listdir(folder_path):

        if os.path.isfile(
            os.path.join(folder_path,file)):

            files.append({
                "name": file,
                "size": os.path.getsize(os.path.join(folder_path,file))
                })

    return {
        "success": True,
        "type": "folder",
        "share_id": share_id,
        "path": share["path"],
        "name": share["path"],
        "files": files
    }

@app.get(
"/api/share-folder-download/{share_id}/{filename:path}"
)
def share_folder_download(
    share_id: str,
    filename: str
):

    with open(SHARE_CONF,"r",encoding="utf-8") as f:
        shares = json.load(f)

    share = shares[share_id]
    file_path = os.path.join(UPLOAD_DIR,share["path"],filename)

    return FileResponse(file_path)

@app.post("/api/share-check")
def share_check(
    share_id: str = Form(...),
    password: str = Form("")
):

    with open(SHARE_CONF,"r",encoding="utf-8") as f:
        shares = json.load(f)

    if share_id not in shares:

        return {
            "success": False,
            "message": "共享不存在"
        }

    share = shares[share_id]
    
    if share["password"] == "":
        return {
            "success": True
        }

    if share["password"] != hash_password(password):

        return {
            "success": False,
            "message": "密码错误"
        }

    return {
        "success": True
    }
    
@app.get("/api/share-download/{share_id}")
def share_download(
    share_id: str
):

    with open(
        SHARE_CONF,
        "r",
        encoding="utf-8"
    ) as f:

        shares = json.load(f)

    if share_id not in shares:

        return {
            "success": False,
            "message": "共享不存在"
        }

    share = shares[share_id]

    expire_time = datetime.strptime(
        share["expire"],
        "%Y-%m-%d %H:%M:%S"
    )

    if datetime.now() > expire_time:

        return {
            "success": False,
            "message": "共享已过期"
        }

    return FileResponse(
        os.path.join(
            UPLOAD_DIR,
            share["path"]
        ),
        filename=os.path.basename(
            share["path"]
        )
    )
    
@app.get("/api/shares")
def get_shares():
    with open(SHARE_CONF, "r", encoding="utf-8") as f:
        return json.load(f)
    
@app.post("/api/delete-share")
def delete_share(
    share_id: str = Form(...)
):

    with open(
        SHARE_CONF,
        "r",
        encoding="utf-8"
    ) as f:

        shares = json.load(f)

    if share_id in shares:

        del shares[share_id]

    with open(
        SHARE_CONF,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            shares,
            f,
            indent=4,
            ensure_ascii=False
        )

    return {
        "success": True
    }

from datetime import datetime

@app.get("/api/share-info/{share_id}")
def share_info(
    share_id: str,
    request: Request
):

    try:
        with open(SHARE_CONF,"r",encoding="utf-8") as f:
            shares = json.load(f)

    except:
        return {"success": False}

    if share_id not in shares:
        return {
            "success": False,
            "message": "共享不存在"
        }

    share = shares[share_id]
    share["view_count"] = share.get("view_count",0) + 1
    
    with open(SHARE_CONF,"w",encoding="utf-8") as f:
        json.dump(
            shares,
            f,
            indent=4,
            ensure_ascii=False
        )

    
    write_share_access_log({
        "share_id": share_id,
        "path": share["path"],
        "visit_time":
            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        "ip":
            request.client.host,
        "user_agent":
            request.headers.get(
                "User-Agent",
                ""
            )
    })

    expire_time = datetime.strptime(
        share["expire"],
        "%Y-%m-%d %H:%M:%S"
    )

    expired = (
        datetime.now()
        > expire_time
    )
    
    return {

        "success": True,
        
        "type":
            share.get("type","file"),

        "name": share["path"],
            # os.path.basename(share["path"]),

        "expire":
            share["expire"],

        "expired":
            expired,

        "has_password":
            bool(share.get("password",""))

    }
    
@app.get("/api/share-access-log/{share_id}")
def get_share_access_log(
    share_id: str
):
    try:
        with open(
            ACCESS_LOG_CONF,
            "r",
            encoding="utf-8"
        ) as f:
            logs = json.load(f)

    except:
        return []

    return [
        item
        for item in logs
        if item["share_id"] == share_id
    ]
    
@app.get(
    "/api/share-folder-zip/{share_id}"
)
def share_folder_zip(
    share_id: str
):

    with open(
        SHARE_CONF,
        "r",
        encoding="utf-8"
    ) as f:

        shares = json.load(f)

    if share_id not in shares:

        return {
            "success": False,
            "message": "共享不存在"
        }

    share = shares[share_id]

    if share["type"] != "folder":

        return {
            "success": False,
            "message": "不是文件夹共享"
        }

    folder_path = os.path.join(
        UPLOAD_DIR,
        share["path"]
    )

    if not os.path.exists(folder_path):

        return {
            "success": False,
            "message": "文件夹不存在"
        }

    temp_zip = tempfile.NamedTemporaryFile(
        delete=False,
        suffix=".zip"
    )

    with zipfile.ZipFile(
        temp_zip.name,
        "w",
        zipfile.ZIP_DEFLATED
    ) as zipf:

        for root, dirs, files in os.walk(folder_path):

            for file in files:

                full_path = os.path.join(
                    root,
                    file
                )

                arc_name = os.path.relpath(
                    full_path,
                    folder_path
                )

                zipf.write(
                    full_path,
                    arc_name
                )

    return FileResponse(
        temp_zip.name,
        filename=
            os.path.basename(
                share["path"]
            ) + ".zip",
        media_type="application/zip"
    )
    
@app.post(
    "/api/create-upload-link"
)
def create_upload_link(

    folder: str = Form(...),

    days: int = Form(...),

    password: str = Form("")

):

    upload_id = str(
        uuid.uuid4()
    )[:8]

    expire_time = (
        datetime.now()
        + timedelta(days=days)
    )

    try:

        with open(
            UPLOAD_LINK_CONF,
            "r",
            encoding="utf-8"
        ) as f:

            links = json.load(f)

    except:

        links = {}

    links[upload_id] = {

        "folder": folder,

        "expire":
            expire_time.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "password":
            hash_password(password)
            if password
            else "",

        "upload_count": 0

    }

    with open(
        UPLOAD_LINK_CONF,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            links,
            f,
            indent=4,
            ensure_ascii=False
        )

    return {

        "success": True,

        "upload_id":
            upload_id

    }
    
@app.get(
    "/upload/{upload_id}"
)
def upload_page(
    upload_id: str
):

    return FileResponse(
        "static/upload.html"
    )
    
@app.get(
    "/api/upload-info/{upload_id}"
)
def upload_info(
    upload_id: str
):

    with open(
        UPLOAD_LINK_CONF,
        "r",
        encoding="utf-8"
    ) as f:

        links = json.load(f)

    if upload_id not in links:

        return {
            "success": False
        }

    return {

        "success": True,

        "folder":
            links[upload_id]["folder"],

        "expire":
            links[upload_id]["expire"],

        "has_password":
            bool(
                links[upload_id][
                    "password"
                ]
            )

    }
    
@app.post(
    "/api/guest-upload"
)
async def guest_upload(
    upload_id: str = Form(...),
    password: str = Form(""),
    file: UploadFile = File(...)
):

    with open(
        UPLOAD_LINK_CONF,
        "r",
        encoding="utf-8"
    ) as f:

        links = json.load(f)

    if upload_id not in links:

        return {
            "success": False
        }

    item = links[upload_id]

    if item["password"]:

        if item["password"] != hash_password(password):

            return {

                "success": False,

                "message":
                    "密码错误"

            }

    target_dir = os.path.join(
        UPLOAD_DIR,
        item["folder"]
    )

    os.makedirs(
        target_dir,
        exist_ok=True
    )

    save_path = os.path.join(
        target_dir,
        file.filename
    )

    with open(
        save_path,
        "wb"
    ) as buffer:

        shutil.copyfileobj(
            file.file,
            buffer
        )

    item["upload_count"] += 1

    with open(
        UPLOAD_LINK_CONF,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            links,
            f,
            indent=4,
            ensure_ascii=False
        )

    return {

        "success": True

    }
