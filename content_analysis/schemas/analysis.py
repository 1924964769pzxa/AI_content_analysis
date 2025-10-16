# -*- coding: utf-8 -*-
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

# ===== 入参模型 =====
class AnalyzeDataIn(BaseModel):
    task_id: str = Field(..., description="任务ID")
    content_info: List[Dict[str, Any]] = Field(..., description="待分析文章数组，每个元素是dict")
    keywords: str = Field("", description="关键词")

class AnalyzeRequest(BaseModel):
    data: AnalyzeDataIn

# ===== 出参/中间结构（用于组织与存库）=====
class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

class CESScore(BaseModel):
    ces: float = 0.0
    weighted_ces: float = 0.0
    rank: int = 0

class ContentScore(BaseModel):
    score: Optional[float] = None
    tags: Optional[List[str]] = None
    reason: Optional[str] = None
    deductions: Optional[Dict[str, Any]] = None

class ConsistencyCheck(BaseModel):
    result: Optional[bool] = None
    reason: Optional[str] = None

class NoteAnalysis(BaseModel):
    content_score: Dict[str, Any] = {}
    consistency_checker: Dict[str, Any] = {}
    CES_score: Dict[str, Any] = {}
    analysis: Dict[str, Any] = {}
    llm_usage: LLMUsage = LLMUsage()
    analyzed_at: Optional[str] = None
    note_id: Optional[str] = None

class AnalyzeItemOut(BaseModel):
    task_id: str
    original: Dict[str, Any]
    tags: str = ""
    created_at: str
    notes_analsys: NoteAnalysis  # 注意：按你的字段名拼写“notes_analsys”
