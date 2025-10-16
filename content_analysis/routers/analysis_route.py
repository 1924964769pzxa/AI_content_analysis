# -*- coding: utf-8 -*-
"""
数据分析主路由：
- 校验 Token
- CES 评分与排序
- Dify 内容评分（过滤：score>80 且 result=true）
- Dify 内容分析（tags + content_disassembly）
- 汇总并回调
- 全链路异步 + 限并发；MongoDB 记录
"""
import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
import logging
import httpx
from fastapi import APIRouter, HTTPException, BackgroundTasks

from content_analysis.config import CALLBACK_URL
from content_analysis.schemas.analysis import (
    AnalyzeRequest, AnalyzeItemOut, NoteAnalysis, LLMUsage
)
from content_analysis.services.ces_service import score_and_sort_by_ces
from content_analysis.services.dify_service import call_dify_score, call_dify_analysis

logger = logging.getLogger(__name__)

# 使用 route 导出，方便你的“路由分发”架构
route = APIRouter(prefix="/v1/analysis", tags=["DataAnalysis"])

def now_iso():
    # 使用本地时间存“created_at”，UTC时间存“analyzed_at”
    return datetime.now().isoformat(timespec="microseconds")

def utc_now_isoz():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

async def _callback(task_id: str, data_list: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """回调上报：将分析结果推送到回调接口"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                CALLBACK_URL,
                headers={"Content-Type": "application/json", "accept": "application/json"},
                json={"task_id": task_id, "data": data_list}
            )
            ok = 200 <= resp.status_code < 300
            return ok, resp.text
    except Exception as e:
        return False, str(e)


async def handle_task(req: AnalyzeRequest):
    task_id = req.data.task_id
    notes = req.data.content_info or []
    keywords = req.data.keywords or ""

    if not task_id:
        raise HTTPException(status_code=400, detail="task_id 不能为空")
    if not isinstance(notes, list) or len(notes) == 0:
        raise HTTPException(status_code=400, detail="content_info 必须是非空数组")

    # ====== Step 1: CES 评分并排序 ======
    logger.info(f"Step 1: CES 评分并排序，task_id: {task_id}, 笔记数量: {len(notes)}, 关键词: {keywords}")
    ces_enriched = await score_and_sort_by_ces(notes)

    # 写入 Mongo：CES初评快照（异步，不阻塞主流程）

    # ====== Step 2: Dify 内容评分（过滤）======
    async def _score_one(note: Dict[str, Any]):
        logger.info(f"Step 2: Dify 内容评分，task_id: {task_id}")
        outputs, usage = await call_dify_score(note, keywords)
        # 过滤条件：score>80 且 result==true
        cs = outputs.get("content_score", {}) or {}
        cc = outputs.get("consistency_checker", {}) or {}

        score = cs.get("score", None)
        result_flag = cc.get("result", None)

        keep = False
        try:
            keep = (float(score) > 80.0) and (bool(result_flag) is True)
        except Exception:
            keep = False

        return {
            "note": note,
            "keep": keep,
            "content_score": cs,
            "consistency_checker": cc,
            "usage": usage
        }

    # 并发评分（限流在 service 内部通过 Semaphore 控制）
    scored_results = await asyncio.gather(*[_score_one(n) for n in ces_enriched])

    # 只保留满足阈值的
    filtered_items = [r for r in scored_results if r["keep"]]

    # ====== Step 3: Dify 内容分析（tags + content_disassembly）======
    async def _analyze_one(item: Dict[str, Any]):
        logger.info(f"Step 3: Dify 内容分析，task_id: {task_id}")
        analysis_outputs, usage2 = await call_dify_analysis(item["note"])
        # 合并 tokens
        u1 = item["usage"]
        usage_merged = LLMUsage(
            input_tokens=(u1.get("input_tokens", 0) + usage2.get("input_tokens", 0)),
            output_tokens=(u1.get("output_tokens", 0) + usage2.get("output_tokens", 0)),
            total_tokens=(u1.get("total_tokens", 0) + usage2.get("total_tokens", 0)),
        )
        # 组装 notes_analsys
        # 从 CES enriched 中补充 rank/ces/weighted_ces
        note = item["note"]
        ces_score = {
            "ces": note.get("ces", 0.0),
            "weighted_ces": note.get("weighted_ces", 0.0),
            "rank": note.get("rank", 0)
        }
        na = NoteAnalysis(
            content_score=item["content_score"],
            consistency_checker=item["consistency_checker"],
            CES_score=ces_score,
            analysis=analysis_outputs,
            llm_usage=usage_merged,
            analyzed_at=utc_now_isoz(),
            note_id=str(note.get("note_id") or note.get("id") or "")
        )
        # 顶层 tags：用分析阶段的 tags（字符串）
        top_tags = analysis_outputs.get("tags", "")

        ai = AnalyzeItemOut(
            task_id=task_id,
            original=note,
            tags=top_tags,
            created_at=now_iso(),
            notes_analsys=na
        )
        return ai.dict()

    analyzed_data: List[Dict[str, Any]] = []
    if filtered_items:
        analyzed_data = await asyncio.gather(*[_analyze_one(i) for i in filtered_items])

    # ====== Step 4: 回调上报 ======
    logger.info(f"Step 4: 回调上报，task_id: {task_id}")
    ok, cb_resp = await _callback(task_id, analyzed_data)

    # 异步存库整个任务结果

    return {
        "task_id": task_id,
        "notes_in": len(notes),
        "kept": len(analyzed_data),
        "callback_ok": ok
    }


@route.post("/content_analyze", summary="批量内容分析（CES筛选 + 内容评分过滤 + 内容分析 + 回调）")
async def content_analyze(
    req: AnalyzeRequest,
    bakground_tasks: BackgroundTasks
):
    bakground_tasks.add_task(handle_task, req)
    return {"message": "Task started"}