from pydantic import BaseModel


class ModelConfig(BaseModel):
    name: str
    display_name: str
    use: str
    model: str
    api_key: str
    base_url: str
