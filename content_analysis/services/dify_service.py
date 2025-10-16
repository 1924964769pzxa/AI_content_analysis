# -*- coding: utf-8 -*-
"""
Dify 调用封装（异步 + 超时 + 重试 + 并发控制）
"""
import asyncio
import json
from typing import Any, Dict, Optional, Tuple

import httpx

from content_analysis.config import (
    DIFY_SCORE_BASE_URL, DIFY_SCORE_PATH, DIFY_SCORE_TOKEN, DIFY_SCORE_RESPONSE_MODE,
    DIFY_ANALYSIS_BASE_URL, DIFY_ANALYSIS_PATH, DIFY_ANALYSIS_TOKEN, DIFY_ANALYSIS_RESPONSE_MODE,
    HTTP_TIMEOUT, HTTP_RETRIES, MAX_CONCURRENCY
)
from content_analysis.utils.text_json import parse_json_from_mixed, strip_think

# 限制外部服务并发，防止压垮对端或被限流
_sem = asyncio.Semaphore(MAX_CONCURRENCY)

async def _post_json_with_retry(url: str, headers: Dict[str, str], data: Dict[str, Any]) -> Dict[str, Any]:
    """统一的 POST JSON 重试封装"""
    for attempt in range(HTTP_RETRIES + 1):
        try:
            async with _sem:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                    resp = await client.post(url, headers=headers, json=data)
                    resp.raise_for_status()
                    return resp.json()
        except Exception as e:
            if attempt >= HTTP_RETRIES:
                return {"error": str(e)}
            await asyncio.sleep(0.5 * (attempt + 1))
    return {"error": "unknown"}

# ======================
# 1) 内容评分工作流
# ======================
async def call_dify_score(content_info: Dict[str, Any], keywords: str) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """
    调用内容评分工作流；返回 (解析后的outputs, token_usage)
    - outputs 里至少包含：content_score(去think后的JSON)、consistency_checker(去think后的JSON)
    - token_usage: {"input_tokens":xx, "output_tokens":xx, "total_tokens":xx}
    """
    url = f"{DIFY_SCORE_BASE_URL.rstrip('/')}{DIFY_SCORE_PATH}"
    headers = {
        "Authorization": f"Bearer {DIFY_SCORE_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "inputs": {
            "content_info": json.dumps(content_info, ensure_ascii=False),  # dict 转字符串
            "keywords": keywords or ""
        },
        "response_mode": DIFY_SCORE_RESPONSE_MODE,
        "user": "content_analysis"
    }
    raw = await _post_json_with_retry(url, headers, payload)
    data = raw.get("data", {})
    outputs = (data or {}).get("outputs", {}) or raw.get("outputs", {})

    # 清洗并解析
    content_score_raw = outputs.get("content_score")
    consistency_checker_raw = outputs.get("consistency_checker")

    content_score = parse_json_from_mixed(content_score_raw) or {}
    consistency_checker = parse_json_from_mixed(consistency_checker_raw) or {}

    # token 统计（兼容不同版本字段）
    usage = {
        "input_tokens": data.get("input_tokens", raw.get("input_tokens", 0)) or 0,
        "output_tokens": data.get("output_tokens", raw.get("output_tokens", 0)) or 0,
        "total_tokens": data.get("total_tokens", raw.get("total_tokens", 0)) or 0,
    }

    clean_outputs = {
        "content_score": content_score,
        "consistency_checker": consistency_checker,
    }
    return clean_outputs, usage

# ======================
# 2) 内容分析工作流
# ======================
async def call_dify_analysis(content_info: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """
    调用内容分析工作流；返回 (analysis_outputs, token_usage)
    - analysis_outputs: {"tags": [... or str], "content_disassembly": {...}}
    """
    url = f"{DIFY_ANALYSIS_BASE_URL.rstrip('/')}{DIFY_ANALYSIS_PATH}"
    headers = {
        "Authorization": f"Bearer {DIFY_ANALYSIS_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "User-Agent": "PostmanRuntime-ApipostRuntime/1.1.0"
    }
    payload = {
        "inputs": {
            "content_info": json.dumps(content_info, ensure_ascii=False)
        },
        "response_mode": DIFY_ANALYSIS_RESPONSE_MODE,
        "user": "workflow"
    }
    raw = await _post_json_with_retry(url, headers, payload)
    data = raw.get("data", {})
    outputs = (data or {}).get("outputs", {}) or raw.get("outputs", {})

    # 兼容 tags 与 content_disassembly 返回是“JSON格式字符串”
    tags_obj = parse_json_from_mixed(outputs.get("tags"))
    content_disassembly_obj = parse_json_from_mixed(outputs.get("content_disassembly"))

    # tags 字段可能是 {"tags": "..."} 或 [".."] 或 "..."
    tags_value = ""
    if isinstance(tags_obj, dict) and "tags" in tags_obj:
        v = tags_obj["tags"]
        if isinstance(v, list):
            tags_value = "，".join([str(x) for x in v])
        else:
            tags_value = str(v)
    elif isinstance(tags_obj, list):
        tags_value = "，".join([str(x) for x in tags_obj])
    elif isinstance(tags_obj, str):
        tags_value = tags_obj

    usage = {
        "input_tokens": data.get("input_tokens", raw.get("input_tokens", 0)) or 0,
        "output_tokens": data.get("output_tokens", raw.get("output_tokens", 0)) or 0,
        "total_tokens": data.get("total_tokens", raw.get("total_tokens", 0)) or 0,
    }

    analysis_outputs = {
        "tags": tags_value,
        "content_disassembly": content_disassembly_obj or {}
    }
    return analysis_outputs, usage
