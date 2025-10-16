# -*- coding: utf-8 -*-
"""
字符串中混入 <think> ... </think> 与 JSON 的清洗与解析工具。
"""
import json
import re
from typing import Any, Dict, Optional

THINK_RE = re.compile(r"<think>.*?</think>", flags=re.DOTALL)

def strip_think(text: str) -> str:
    """移除 <think>...</think> 段落；保留其后的 JSON/文本"""
    if not isinstance(text, str):
        return text
    cleaned = THINK_RE.sub("", text)
    return cleaned.strip()

def parse_json_from_mixed(s: Any) -> Optional[Dict[str, Any]]:
    """从混合字符串中提取JSON（移除<think>），并解析为dict"""
    if s is None:
        return None
    if not isinstance(s, str):
        # 已经是对象/字典直接返回
        return s if isinstance(s, dict) else None
    cleaned = strip_think(s)
    # 寻找第一个 { 与最后一个 }，做稳健解析
    try:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(cleaned[start:end+1])
    except Exception:
        return None
    return None
