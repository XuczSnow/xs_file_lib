from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from datetime import datetime, timedelta

import os
import json
import uuid
import shutil
import hashlib

app = FastAPI()

UPLOAD_DIR = "./uploads"
USER_CONF = "./config/users.json"
SHARE_CONF = "./config/shares.json"


os.makedirs(
    UPLOAD_DIR,
    exist_ok=True
)

app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static"
)

def hash_password(password: str):
    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()


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
        "role": users[username]["role"]
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
    role: str = Form(...)
):

    with open(USER_CONF,"r",encoding="utf-8") as f:
        users = json.load(f)

    users[username] = {
        "password": hash_password(password),
        "role": role
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

@app.get("/api/files")
def get_files():

    result = {}

    for root, dirs, files in os.walk(UPLOAD_DIR):
        rel_dir = os.path.relpath(root,UPLOAD_DIR)

        # if rel_dir == ".":
        #     rel_dir_name = "根目录"
        # else:
        #     rel_dir_name = rel_dir

        result[rel_dir] = []

        for file in files:
            result[rel_dir].append({
                "name": file,
                "path": os.path.join(
                    rel_dir,
                    file
                ).replace("\\", "/"),
                "size": os.path.getsize(
                    os.path.join(root, file)
                )
            })

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
    days: int = Form(...),
    password: str = Form("")
):

    share_id = str(uuid.uuid4())[:8]

    expire_time = datetime.now() + timedelta(days=days)

    try:

        with open(
            SHARE_CONF,
            "r",
            encoding="utf-8"
        ) as f:

            shares = json.load(f)

    except:

        shares = {}

    shares[share_id] = {

        "path": filepath,

        "created_by":
            "system",

        "expire":
            expire_time.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            
        "password":
            hash_password(password)
            if password
            else ""
        }

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

        "success": True,

        "share_id": share_id,

        "expire":
            shares[share_id]["expire"]

    }

# @app.get("/share/{share_id}")
# def access_share(
#     share_id: str
# ):

#     try:

#         with open(
#             SHARE_CONF,
#             "r",
#             encoding="utf-8"
#         ) as f:

#             shares = json.load(f)

#     except:

#         shares = {}

#     if share_id not in shares:

#         return {
#             "success": False,
#             "message": "共享不存在"
#         }

#     share = shares[share_id]

#     expire_time = datetime.strptime(
#         share["expire"],
#         "%Y-%m-%d %H:%M:%S"
#     )

#     if datetime.now() > expire_time:

#         return {
#             "success": False,
#             "message": "共享已过期"
#         }

#     filepath = share["path"]

#     return FileResponse(
#         os.path.join(
#             UPLOAD_DIR,
#             filepath
#         ),
#         filename=os.path.basename(filepath)
#     )

@app.get("/share/{share_id}")
def access_share(
    share_id: str
):

    return FileResponse(
        "static/share.html"
    )

@app.post("/api/share-check")
def share_check(
    share_id: str = Form(...),
    password: str = Form(...)
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
    
@app.get(
    "/api/share-download/{share_id}"
)
def share_download(
    share_id: str
):

    with open(
        SHARE_CONF,
        "r",
        encoding="utf-8"
    ) as f:

        shares = json.load(f)

    share = shares[share_id]

    return FileResponse(

        os.path.join(
            UPLOAD_DIR,
            share["path"]
        ),

        filename=
            os.path.basename(
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
