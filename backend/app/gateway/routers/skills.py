from fastapi import APIRouter, Request
from pydantic import BaseModel

from caspian.agents.lead.agent import build_enabled_skill_catalog

router = APIRouter(prefix="/api")


class SkillSummary(BaseModel):
    name: str
    description: str


class SkillCatalogResponse(BaseModel):
    skills: list[SkillSummary]


@router.get("/skills", response_model=SkillCatalogResponse)
async def list_skills(request: Request) -> SkillCatalogResponse:
    user_id = str(request.state.current_user.id)
    by_name = {
        skill.name: SkillSummary(name=skill.name, description=skill.description)
        for skill in build_enabled_skill_catalog(user_id=user_id).skills
    }
    return SkillCatalogResponse(
        skills=[by_name[name] for name in sorted(by_name)]
    )
