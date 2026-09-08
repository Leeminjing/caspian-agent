from pydantic import BaseModel


class ModelConfig(BaseModel):
    name: str
    display_name: str
    use: str
    model: str
    api_key: str
    base_url: str
    # 模型是否支持多模态图片输入；用于前端/网关判断是否允许粘贴图片并直接发为 image_url。
    vision: bool = False
