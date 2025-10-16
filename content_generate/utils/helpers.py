import json
import logging
import random
from typing import Iterable, List, Sequence

logger = logging.getLogger("content_generate.utils")

def dedup_preserve_order(items: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def pick_one(seq: Sequence):
    if not seq:
        return None
    return random.choice(list(seq))

def to_json_str(data) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return str(data)

def parse_image_list(image_list_str: str) -> List[str]:
    if not image_list_str:
        return []
    parts = [p.strip() for p in image_list_str.split(",")]
    return [p for p in parts if p and p.startswith("http")]

def get_title_from_material(m) -> str:
    title = (m.note_raw_data.title if m.note_raw_data else None) or ""
    if title.strip():
        return title.strip()
    if m.analysis_data and m.analysis_data.note_id:
        return f"参考文 {m.analysis_data.note_id}"
    return m.keyword or "参考标题"

def safe_first_image_url(m) -> str:
    imgs = parse_image_list(m.note_raw_data.image_list if (m.note_raw_data and m.note_raw_data.image_list) else "")
    return imgs[0] if imgs else ""
