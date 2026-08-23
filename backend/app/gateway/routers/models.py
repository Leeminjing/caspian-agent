"""模型列表接口。

对外提供:
    router: APIRouter — GET /api/models 返回 config.yaml 中声明的可用模型

输入: 无

输出:
    dict — {"models": [{"name": ..., "display_name": ...}, ...]}

工作流:
    (1) 调用 get_app_config("config.yaml") 读取配置
    (2) 遍历 models 提取 name 与 display_name

示例:
    GET /api/models
    => {"models": [{"name": "deepseek-v4-flash", "display_name": "Other OpenAI-compatible / deepseek-v4-flash"}]}
"""

from fastapi import APIRouter

from caspian.config.app_config import get_app_config

router = APIRouter(prefix="/api")


@router.get("/models")
async def list_models() -> dict:
    cfg = get_app_config("config.yaml")
    return {
        "models": [
            {"name": m.name, "display_name": m.display_name}
            for m in cfg.models
        ]
    }
