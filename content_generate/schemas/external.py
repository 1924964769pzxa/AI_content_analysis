from typing import List, Optional, Any
from pydantic import BaseModel

# ---- 关键词 ----
class KeywordResp(BaseModel):
    code: int
    msg: str
    data: List[str]

# ---- 素材检索 ----
class MaterialNoteRaw(BaseModel):
    note_id: Optional[str] = None
    avatar: Optional[str] = None
    collected_count: Optional[str] = None
    comment_count: Optional[str] = None
    desc: Optional[str] = None
    image_list: Optional[str] = None
    liked_count: Optional[str] = None
    nickname: Optional[str] = None
    note_url: Optional[str] = None
    share_count: Optional[str] = None
    source_keyword: Optional[str] = None
    sticky: Optional[str] = None
    tag_list: Optional[str] = None
    title: Optional[str] = None
    type: Optional[str] = None
    user_id: Optional[str] = None

class MaterialAnalysis(BaseModel):
    tags: Optional[str] = None
    content_disassembly: Optional[Any] = None

class MaterialAnalysisData(BaseModel):
    note_id: Optional[str] = None
    analysis: Optional[MaterialAnalysis] = None

class MaterialItem(BaseModel):
    id: int
    note_raw_data: Optional[MaterialNoteRaw] = None
    analysis_data: Optional[MaterialAnalysisData] = None
    keyword: Optional[str] = None

class MaterialSearchResp(BaseModel):
    code: int
    msg: str
    data: List[MaterialItem]
