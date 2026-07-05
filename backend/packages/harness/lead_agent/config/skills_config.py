from pydantic import BaseModel


class SkillsConfig(BaseModel):
    container_path: str
