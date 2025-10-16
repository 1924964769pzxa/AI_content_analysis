# -*- coding: utf-8 -*-
"""
CES 评分服务：调用你提供的 ces_model 并进行排序与标注
"""
from typing import Any, Dict, List
from content_analysis.ces_model import CESFilterConfig, score_and_filter_notes

def _rank_and_label(enriched: List[Dict[str, Any]]) -> None:
    """为已排序的 enriched 列表添加 rank 字段（从1开始）"""
    for idx, item in enumerate(enriched, 1):
        item["rank"] = idx

async def score_and_sort_by_ces(notes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    调用 ces_model 进行评分（包含时间衰减），并按 (weighted_ces, ces) 二级排序。
    """
    cfg = CESFilterConfig(
        enable_time_decay=True,
        half_life_hours=48.0,
        min_ces=0.0,
        min_weighted_ces=0.0,
        yield_every=100
    )
    enriched = await score_and_filter_notes(notes, cfg)
    enriched.sort(key=lambda x: (x.get("weighted_ces", 0.0), x.get("ces", 0.0)), reverse=True)
    _rank_and_label(enriched)
    return enriched
