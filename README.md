# Xucz File Library

一个基于 FastAPI + HTML + JavaScript + Bootstrap 的轻量级个人文件库系统。

Xucz File Library是一个基于 FastAPI 开发的轻量级文件管理系统，集成用户认证、角色权限控制、文件上传下载、文件夹管理、文件搜索、用户管理以及深色模式等功能。系统采用前后端分离设计，支持管理员（Admin）、普通用户（User）和访客（Viewer）多级权限管理，实现文件资源的安全存储与共享。通过树形目录结构和直观的管理界面，用户能够方便地进行文件分类、浏览和维护，适用于个人知识库、团队文档库以及内部文件共享场景。支持密码加密存储、主题切换和多文件夹管理，可作为轻量级文档管理平台使用。

## 功能特性

- 用户登录
- 用户管理
- 角色权限控制
- 文件上传
- 文件下载
- 文件删除
- 文件夹管理
- 文件搜索
- 深色模式
- 密码加密
- 文件夹树形展示

## 项目结构

```text
FileLibraryPro
│
├── main.py
├── users.json
├── config.json
├── requirements.txt
│
├── uploads
│
└── static
    ├── index.html
    ├── style.css
    └── app.js
```

## 安装

```bash
pip install -r requirements.txt
```

## 启动

```bash
uvicorn main:app --reload
```

访问：

```text
http://127.0.0.1:8000
```

## 文件管理

- 上传文件
- 下载文件
- 删除文件
- 搜索文件
- 创建文件夹
- 上传到指定文件夹
- 文件夹树形展开/收起

## 用户管理

管理员可：

- 新增用户
- 删除用户
- 修改密码
- 查看用户列表

## 密码安全

使用 SHA256 对密码进行加密存储。

## 深色模式

支持亮色/深色模式切换，并自动保存用户主题设置。

## 权限矩阵

| 功能 | admin | user | viewer |
|------|-------|------|--------|
| 浏览文件 | ✅ | ✅ | ✅ |
| 下载文件 | ✅ | ✅ | ✅ |
| 上传文件 | ✅ | ✅ | ❌ |
| 删除文件 | ✅ | ❌ | ❌ |
| 创建文件夹 | ✅ | ✅ | ❌ |
| 用户管理 | ✅ | ❌ | ❌ |

## 技术栈

- FastAPI
- Python
- HTML
- CSS
- JavaScript
- Bootstrap 5
