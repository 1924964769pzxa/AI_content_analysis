import json
import logging
from typing import Dict, List

from fastapi import APIRouter, Body, HTTPException
from content_generate.schemas import GenerateRequest, GenerateItem, GenerateResult
from content_generate.service import ContentGenerateService

router = APIRouter(prefix="/content", tags=["ContentGenerator"])
logger = logging.getLogger("content_generate.routes")

# 单例服务，保持关键词回调标记等状态
service_singleton = ContentGenerateService()


@router.post("/generate", response_model=List[GenerateResult], summary="批量内容生成")
async def generate_endpoint(req: GenerateRequest = Body(...)):
    """
    主入口：
        批量生成内容：获取人设-获取keyword-生成图文
    """
    try:
        results = await service_singleton.run_batch(req.data)
        # 也打印一个总汇，便于排查
        print(json.dumps([r.dict() for r in results], ensure_ascii=False, indent=2))
        return results
    except Exception as e:
        logger.exception("生成失败")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/callbacks/keywords",summary="Keyword获取回调")
async def keywords_callback(payload: Dict):
    """
    关键词填充回调（可选）：将 tag 或 tags 回传到此路由，服务会标记为 ready 并在轮询中尽快放行。
    允许 payload: {"tag": "户外"} 或 {"tags": ["户外","美食"]}
    """
    tags = []
    if "tag" in payload and isinstance(payload["tag"], str):
        tags = [payload["tag"]]
    elif "tags" in payload and isinstance(payload["tags"], list):
        tags = payload["tags"]

    for t in tags:
        service_singleton.mark_keyword_ready(t)

    return {"code": 0, "msg": "ok", "data": {"received": tags}}
