from pydantic import BaseModel, ConfigDict


class ToolConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    group: str
    use: str
    max_results: int | None = None
