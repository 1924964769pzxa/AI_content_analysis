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
    单个人设对象（注意：现在 data 数组中每个元素就是一个人设）
    - 关键字段：tag（列表），可选 task_id/name 等
    """
    tag: List[str] = Field(default_factory=list)
    persona_info: Dict = Field(default_factory=list)
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

class GenerateItem(BaseModel):
    """
    data 数组的每个元素：对应一个人设
    """
    task_id: Union[int, str]
    personas: Persona
    history_plans: Optional[List[HistoryPlan]] = Field(default_factory=list)

class GenerateRequest(BaseModel):
    data: List[GenerateItem]
