from typing import Dict, List, Optional, Any, Union
from pydantic import BaseModel, Field, validator

class HistoryStats(BaseModel):
    likes: int = 0
    favorites: int = 0
    comments: int = 0
    shares: int = 0

class HistoryPlan(BaseModel):
    date: str
    tags: str
    source_keyword: str
    title: str
    stats: HistoryStats

class Persona(BaseModel):
    """
    人设对象
    """
    tag: List[str] = Field(default_factory=list)
    persona_info: Dict = Field(default_factory=dict)
    # 兼容某些传入把 "tags" 当做字段名的情况
    @validator("tag", pre=True, always=True)
    def _ensure_tag(cls, v, values, **kwargs):
        if v:
            return v
        # 如果没有 tag 字段，尝试从 values["tags"] 读取
        if "tags" in values and isinstance(values["tags"], list):
            return values["tags"]
        return []

    class Config:
        extra = "allow"  # 允许携带更多属性

class PersonalData(BaseModel):
    """
    个人数据对象
    """
    personas: Persona
    history_plans: Optional[List[HistoryPlan]] = Field(default_factory=list)

class GenerateItem(BaseModel):
    """
    创作任务对象
    """
    content_type: str = Field(..., description="涨粉/种草")
    product_info: List[Any] = Field(default_factory=list, description="产品信息")
    language_style: str = Field(default="", description="语言风格")
    platform: str = Field(default="xhs", description="平台")
    creative_elements: List[Any] = Field(default_factory=list, description="创作要素/项目信息")
    title_requirement: str = Field(default="", description="标题要求")
    content_requirement: str = Field(default="", description="内容要求")
    articles: int = Field(default=1, description="生成篇数")
    content_creation_id: int = Field(..., description="内容创作ID")
    personal_data: List[PersonalData] = Field(default_factory=list, description="个人数据")

class GenerateRequest(BaseModel):
    data: List[GenerateItem]
