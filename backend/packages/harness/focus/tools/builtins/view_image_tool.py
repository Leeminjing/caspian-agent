"""
本文件对外提供 view_image_tool，读取指定图片文件并将图片内容登记到 viewed_images 状态。

内部辅助:
    _is_allowed_image_virtual_path — 判断图片路径是否位于允许访问的虚拟目录下，返回 bool
    _detect_image_mime — 根据图片二进制头部识别真实 MIME 类型，返回 str | None
    _sanitize_image_error — 对错误信息脱敏，避免暴露本地真实路径，返回 str

输入:
    image_path: str — 图片文件路径，必须是 /mnt/user-data 下的虚拟路径，允许目录: workspace / uploads / outputs
    ToolRuntime — LangGraph 运行时注入，提供 state、config

输出:
    Command — 成功时更新 viewed_images 和 messages，失败时返回错误 ToolMessage

具体工作流:
    (1) view_image_tool 接收 ToolRuntime、image_path
    (2) 检查 image_path 是否位于允许访问的虚拟目录中
    (3) 校验 image_path 是否符合本地工具访问规则
    (4) 从 runtime 获取 user_id 和 thread_id，将虚拟路径解析为后端真实文件路径
    (5) 检查真实路径是否存在
    (6) 检查真实路径是否是文件（非目录）
    (7) 根据文件扩展名判断图片格式是否受支持
    (8) 根据 mimetypes 和扩展名确定期望 MIME 类型
    (9) 读取文件大小，拒绝超过最大限制
    (10) 读取图片二进制内容
    (11) 根据二进制头部识别真实 MIME 类型
    (12) 校验真实 MIME 类型与扩展名对应 MIME 类型是否一致
    (13) 将图片二进制内容编码为 base64
    (14) 构造 viewed_images 状态数据
    (15) 返回包含 viewed_images 和成功 ToolMessage 的 Command
    (16) 任一步骤失败则经 _sanitize_image_error 脱敏后返回错误 ToolMessage

示例:
    runtime: ToolRuntime = ...
    result = view_image_tool(
        image_path="/mnt/user-data/uploads/example.png",
        runtime=runtime,
    )
    输出 Command:
    {
        "viewed_images": {
            "/mnt/user-data/uploads/example.png": {"base64": "...", "mime_type": "image/png"}
        },
        "messages": [ToolMessage("Successfully read image")]
    }
"""

import base64
import mimetypes
import os

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from focus.sandbox.path_utils import VRROOT

_ALLOWED_DIRS = [
    VRROOT + "/workspace",
    VRROOT + "/uploads",
    VRROOT + "/outputs",
]

_SUPPORTED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})

_MAX_FILE_SIZE = 20 * 1024 * 1024

_MIME_BY_EXTENSION = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

_JPEG_MAGIC = (b"\xff\xd8\xff",)
_PNG_MAGIC = (b"\x89\x50\x4e\x47",)
_WEBP_MAGIC = (b"RIFF",)  # RIFF .... WEBP


def _is_allowed_image_virtual_path(image_path: str) -> bool:
    for allowed in _ALLOWED_DIRS:
        if image_path == allowed or image_path.startswith(allowed + "/"):
            return True
    return False


def _detect_image_mime(data: bytes) -> str | None:
    if data[:3] in _JPEG_MAGIC:
        return "image/jpeg"
    if data[:4] == _PNG_MAGIC[0]:
        return "image/png"
    if data[:4] == _WEBP_MAGIC[0] and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _sanitize_image_error(error: Exception, real_path: str = "", image_path: str = "") -> str:
    msg = str(error)
    if real_path and image_path:
        msg = msg.replace(real_path, image_path)
    if real_path:
        msg = msg.replace(real_path.replace("/", "\\"), image_path)
    msg = msg.replace("\\", "/")
    return f"view_image 失败: {msg}"


@tool(parse_docstring=True)
def view_image_tool(
    image_path: str,
    runtime: ToolRuntime,
) -> Command:
    """Read an image file.

Use this tool to read an image file and make it available for display.

When to use the view_image tool:
- When you need to view an image file.

When NOT to use the view_image tool:
- For non-image files (use present_files instead)
- For multiple files at once (use present_files instead)

Args:
    image_path: Absolute /mnt/user-data virtual path to the image file. Common formats supported: jpg, jpeg, png, webp.
"""
    real_path = ""
    try:
        # (2) 检查 image_path 是否位于允许的虚拟目录
        if not _is_allowed_image_virtual_path(image_path):
            raise ValueError(
                f"图片路径不允许访问: '{image_path}'，"
                f"仅允许 workspace、uploads、outputs 目录下的图片"
            )

        # (3) 校验本地工具访问规则 — 确保以 /mnt/user-data/ 开头
        if not image_path.startswith(VRROOT + "/"):
            raise ValueError(f"图片路径必须以 {VRROOT}/ 开头")

        # (4) 虚拟路径解析为后端真实文件路径
        from focus.sandbox.path_utils import resolve_path

        thread_id = None
        if runtime.execution_info is not None:
            thread_id = runtime.execution_info.thread_id
        if thread_id is None:
            raise ValueError("无法获取当前线程 ID")

        user_id = None
        try:
            ctx = runtime.context
            if ctx and isinstance(ctx, dict):
                user_id = ctx.get("user_id")
        except Exception:
            pass
        if user_id is None:
            raise ValueError("无法获取 user_id")

        real_path = resolve_path(image_path, user_id, thread_id)

        # (5) 检查真实路径是否存在
        if not os.path.exists(real_path):
            raise ValueError("图片文件不存在")

        # (6) 检查真实路径是否是文件
        if not os.path.isfile(real_path):
            raise ValueError("路径指向的不是文件")

        # (7) 根据文件扩展名判断图片格式是否受支持
        _, ext = os.path.splitext(image_path)
        ext = ext.lower()
        if ext not in _SUPPORTED_EXTENSIONS:
            raise ValueError(
                f"不支持的图片格式: '{ext}'，仅支持 jpg、jpeg、png、webp"
            )

        # (8) 根据 mimetypes 和扩展名确定期望 MIME 类型
        expected_mime = _MIME_BY_EXTENSION.get(ext)
        if expected_mime is None:
            mime_type, _ = mimetypes.guess_type(image_path)
            expected_mime = mime_type
        if expected_mime is None:
            raise ValueError(f"无法根据扩展名确定 MIME 类型: '{ext}'")

        # (9) 读取文件大小，拒绝超过最大限制
        file_size = os.path.getsize(real_path)
        if file_size > _MAX_FILE_SIZE:
            raise ValueError(
                f"图片文件过大: {file_size} 字节，最大允许 {_MAX_FILE_SIZE} 字节"
            )

        # (10) 读取图片二进制内容
        with open(real_path, "rb") as f:
            image_data = f.read()

        # (11) 根据二进制头部识别真实 MIME 类型
        detected_mime = _detect_image_mime(image_data)
        if detected_mime is None:
            raise ValueError("无法识别的图片格式，文件内容不是有效的 jpg、png 或 webp")

        # (12) 校验真实 MIME 类型与扩展名对应 MIME 类型是否一致
        if detected_mime != expected_mime:
            raise ValueError(
                f"文件扩展名与真实内容不一致: 扩展名对应 {expected_mime}，实际内容为 {detected_mime}"
            )

        # (13) 将图片二进制内容编码为 base64
        base64_content = base64.b64encode(image_data).decode("ascii")

        # (14) 构造 viewed_images 状态数据
        viewed_entry = {
            "base64": base64_content,
            "mime_type": detected_mime,
        }

        # (15) 返回包含 viewed_images 和成功 ToolMessage 的 Command
        return Command(
            update={
                "viewed_images": {image_path: viewed_entry},
                "messages": [
                    ToolMessage(
                        content="Successfully read image",
                        tool_call_id=runtime.tool_call_id,
                    )
                ],
            }
        )

    except Exception as e:
        # (16) 失败则经 _sanitize_image_error 脱敏后返回错误 ToolMessage
        sanitized = _sanitize_image_error(e, real_path=real_path, image_path=image_path)
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=sanitized,
                        tool_call_id=runtime.tool_call_id,
                    )
                ]
            }
        )
