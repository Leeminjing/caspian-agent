from pydantic import BaseModel


class ToolGroupConfig(BaseModel):
    name: str
