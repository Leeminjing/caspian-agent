"""
本文件对外提供 `router`（APIRouter 实例），定义文件上传接口 `POST /api/threads/{thread_id}/uploads`。

对外提供:
    router: APIRouter — 已注册上传路由的 FastAPI Router，挂载到 /api/threads 前缀

输入:
    upload_files:
        thread_id: str — 请求路径参数，目标 thread ID
        files: list[UploadFile] — multipart/form-data 上传的文件列表
        request: Request — FastAPI Request 对象（用于获取 user_id）

输出:
    JSONResponse — { files: [{ filename, size }] }
    size 单位为字节

具体工作流:
    (1) 从 request.state.current_user.id 获取 user_id
    (2) 构造沙箱 uploads 目录真实路径: .caspian/users/{user_id}/threads/{thread_id}/user-data/uploads/
    (3) 若目录不存在则自动创建
    (4) 遍历上传的文件:
        (a) 生成安全文件名（保留扩展名）
        (b) 处理文件名冲突：同名不覆盖，追加 " (1)" 后缀
        (c) 异步写入文件到 uploads 目录
        (d) 收集 { filename, size } 元数据
    (5) 返回 { files: [...] }

示例:
    from backend.app.gateway.routers.uploads import router
    app.include_router(router, prefix="/api/threads")
"""

import os
import re
from pathlib import Path

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import JSONResponse

from caspian.sandbox.path_utils import REAL_ROOT

router = APIRouter()


def _safe_filename(filename: str) -> str:
    """清理文件名中的路径分隔符和危险字符。

    输入:
        filename: str — 原始文件名

    输出:
        str — 清理后的安全文件名（保留扩展名）

    工作流:
        (1) 分离文件名和扩展名
        (2) 去除路径分隔符
        (3) 重新组装
    """
    base, ext = os.path.splitext(filename)
    base = base.replace("/", "_").replace("\\", "_")
    base = re.sub(r'[\x00-\x1f]', '', base)
    if not base:
        base = "file"
    return base + ext


def _resolve_upload_path(user_id: str, thread_id: str, filename: str) -> str:
    """根据 user_id + thread_id + filename 构造上传文件的真实磁盘路径，处理同名冲突。

    输入:
        user_id: str — 当前用户 ID
        thread_id: str — 目标 thread ID
        filename: str — 安全化后的文件名

    输出:
        str — 最终保存的完整磁盘路径

    工作流:
        (1) 格式化 REAL_ROOT 模板得到 uploads 目录路径
        (2) os.makedirs 确保目录存在
        (3) 检查目标路径是否已存在:
            - 不存在 → 直接返回该路径
            - 已存在 → 在文件名后追加 " (N)" 后缀直到不冲突（模仿 Windows），返回新路径

    示例:
        _resolve_upload_path("uuid-xxx", "th-001", "report.md")
        → .caspian/users/uuid-xxx/threads/th-001/user-data/uploads/report.md
    """
    uploads_dir = os.path.abspath(REAL_ROOT.format(user_id=user_id, thread_id=thread_id) + "/uploads")
    os.makedirs(uploads_dir, exist_ok=True)

    target = os.path.join(uploads_dir, filename)
    if not os.path.exists(target):
        return target

    base, ext = os.path.splitext(filename)
    counter = 1
    while True:
        new_name = f"{base} ({counter}){ext}"
        target = os.path.join(uploads_dir, new_name)
        if not os.path.exists(target):
            return target
        counter += 1


@router.post("/{thread_id}/uploads")
async def upload_files(thread_id: str, request: Request):
    """POST /api/threads/{thread_id}/uploads — 上传文件到指定 thread 的沙箱 uploads 目录。

    输入:
        thread_id: str — 目标 thread ID
        request: Request — FastAPI Request（从 request.state.current_user.id 取 user_id，
                 从 request.form() 读取 multipart 文件）

    输出:
        JSONResponse — { files: [{ filename, size }] }
    """
    user_id = str(request.state.current_user.id)

    form = await request.form()
    uploaded: list[UploadFile] = []
    for field_value in form.values():
        if hasattr(field_value, "filename"):
            uploaded.append(field_value)

    files_meta: list[dict] = []
    for file in uploaded:
        safe_name = _safe_filename(file.filename or "unnamed")
        content = await file.read()
        file_size = len(content)

        save_path = _resolve_upload_path(user_id, thread_id, safe_name)
        actual_filename = os.path.basename(save_path)

        with open(save_path, "wb") as f:
            f.write(content)

        files_meta.append({
            "filename": actual_filename,
            "size": file_size,
        })

    return JSONResponse(content={"files": files_meta})
