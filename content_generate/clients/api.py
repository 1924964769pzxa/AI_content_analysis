import asyncio
import logging
from typing import Dict, List, Optional, Any
from urllib.parse import quote

import httpx

from content_generate.config import (
    MAT_KEYWORDS_GET, KEYWORD_FILL_URL, MAT_SEARCH_BY_TAG, MAT_CREATE_SEARCH_TASK,
    DIFY_RUN_URL, DIFY_TOKEN_TYPE_DETECT, DIFY_TOKEN_SINGLE_WRITE, DIFY_TOKEN_COMBO_WRITE,
    DIFY_TOKEN_SINGLE_IMAGE, DIFY_TOKEN_KEYWORD_SELECT, HTTP_TIMEOUT, MAX_HTTP_CONCURRENCY,
    KEYWORD_POLL_MAX_SECONDS, KEYWORD_POLL_INTERVAL_SECONDS,
)
from content_generate.schemas.external import KeywordResp, MaterialSearchResp

logger = logging.getLogger("content_generate.clients")


class APIClient:
    def __init__(self):
        self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        self._sem = asyncio.Semaphore(MAX_HTTP_CONCURRENCY)
        # 供回调快速放行使用
        self.keyword_ready_flags: Dict[str, bool] = {}

    async def close(self):
        await self._client.aclose()

    # ---------- 关键词 ----------
    async def get_keywords(self, tag: str) -> List[str]:
        url = MAT_KEYWORDS_GET.format(tag=quote(tag, safe=""))
        async with self._sem:
            r = await self._client.get(url, headers={"accept": "application/json"})
        r.raise_for_status()
        body = r.json()
        keywords = body.get("data") or []
        if not isinstance(keywords, list):
            keywords = []
        return keywords

    async def generate_keywords(self, tags: List[str]) -> None:
        payload = {"tags": tags}
        logger.info(f"[keywords_generate] 提交补齐：{tags}")
        async with self._sem:
            r = await self._client.post(
                KEYWORD_FILL_URL, json=payload,
                headers={"accept": "application/json", "Content-Type": "application/json"}
            )
        r.raise_for_status()
        logger.info("[keywords_generate] 已提交，等待回填/轮询。")

    async def ensure_keywords_ready(self, tags: List[str]) -> Dict[str, List[str]]:
        """
        对每个 tag：先查；无则触发补齐并轮询（期间若回调触发，则立即重查）。
        返回映射：tag -> keywords
        """
        tags = list(tags)
        result: Dict[str, List[str]] = {}
        missing: List[str] = []

        # 首查
        for t in tags:
            kws = await self.get_keywords(t)
            if kws:
                result[t] = kws
                logger.info(f"[keywords] {t} => {len(kws)}")
            else:
                missing.append(t)
                logger.warning(f"[keywords] {t} => 空")

        if missing:
            await self.generate_keywords(missing)

            elapsed = 0.0
            while missing and elapsed < KEYWORD_POLL_MAX_SECONDS:
                await asyncio.sleep(KEYWORD_POLL_INTERVAL_SECONDS)
                elapsed += KEYWORD_POLL_INTERVAL_SECONDS

                still_missing = []
                for t in missing:
                    # 如收到回调标记则优先尝试重查
                    if self.keyword_ready_flags.get(t):
                        logger.info(f"[keywords][回调] tag={t} 标记 ready，立即重查")
                    kws = await self.get_keywords(t)
                    if kws:
                        result[t] = kws
                        logger.info(f"[keywords][回填完成] {t} => {len(kws)}")
                    else:
                        still_missing.append(t)
                missing = still_missing

            if missing:
                logger.error(f"[keywords] 超时仍未回填：{missing}")

        return result

    # ---------- 素材 ----------
    async def search_materials_by_keyword(self, keyword: str) -> List[dict]:
        payload = {"key_word": keyword}
        async with self._sem:
            r = await self._client.post(
                MAT_SEARCH_BY_TAG, json=payload,
                headers={"accept": "application/json", "Content-Type": "application/json"}
            )
        r.raise_for_status()
        data = MaterialSearchResp.parse_obj(r.json())
        return [x.dict() for x in (data.data or [])]

    async def create_material_search_task(self, keyword: str, kox_task_id: str) -> None:
        payload = {"keyword": keyword, "kox_task_id": kox_task_id}
        logger.info(f"[create_search_task] keyword={keyword} kox_task_id={kox_task_id}")
        async with self._sem:
            r = await self._client.post(
                MAT_CREATE_SEARCH_TASK, json=payload,
                headers={"accept": "application/json", "Content-Type": "application/json"}
            )
        r.raise_for_status()
        logger.info("[create_search_task] 已提交")

    # ---------- Dify 工作流 ----------
    async def run_type_detect(self, img1_url: str, img2_url: str) -> Dict[str, Any]:
        payload = {
            "inputs": {
                "img":  {"transfer_method": "remote_url", "url": img1_url, "type": "image"},
                "img2": {"transfer_method": "remote_url", "url": img2_url, "type": "image"},
            },
            "response_mode": "blocking", "user": "workflows"
        }
        async with self._sem:
            r = await self._client.post(
                DIFY_RUN_URL, json=payload,
                headers={"Authorization": f"Bearer {DIFY_TOKEN_TYPE_DETECT}", "Content-Type": "application/json"}
            )
        r.raise_for_status()
        return r.json()

    async def run_single_write(self, character: str, content_structure: str, title_list: str, 
                              language_style: str = "", title_requirement: str = "", 
                              content_requirement: str = "", creative_elements: List[Any] = None) -> Dict[str, Any]:
        payload = {
            "inputs": {
                "character": character,
                "content_structure": content_structure,
                "title_list": title_list,
                "language_style": language_style,
                "title_requirement": title_requirement,
                "content_requirement": content_requirement,
                "creative_elements": creative_elements or [],
            },
            "response_mode": "blocking", "user": "workflows"
        }
        async with self._sem:
            r = await self._client.post(
                DIFY_RUN_URL, json=payload,
                headers={"Authorization": f"Bearer {DIFY_TOKEN_SINGLE_WRITE}", "Content-Type": "application/json"}
            )
        r.raise_for_status()
        return r.json()

    async def run_combo_write(self, character: str, content_structure: str, title_list: str,
                             language_style: str = "", title_requirement: str = "", 
                             content_requirement: str = "", creative_elements: List[Any] = None) -> Dict[str, Any]:
        payload = {
            "inputs": {
                "character": character,
                "content_structure": content_structure,
                "title_list": title_list,
                "language_style": language_style,
                "title_requirement": title_requirement,
                "content_requirement": content_requirement,
                "creative_elements": creative_elements or [],
            },
            "response_mode": "blocking", "user": "workflows"
        }
        async with self._sem:
            r = await self._client.post(
                DIFY_RUN_URL, json=payload,
                headers={"Authorization": f"Bearer {DIFY_TOKEN_COMBO_WRITE}", "Content-Type": "application/json"}
            )
        r.raise_for_status()
        return r.json()

    async def run_single_image(self, img_url: str) -> Optional[str]:
        payload = {
            "inputs": {
                "img_url": img_url,
                "img": {"transfer_method": "remote_url", "url": img_url, "type": "image"},
            },
            "response_mode": "blocking", "user": "workflows"
        }
        async with self._sem:
            r = await self._client.post(
                DIFY_RUN_URL, json=payload,
                headers={"Authorization": f"Bearer {DIFY_TOKEN_SINGLE_IMAGE}", "Content-Type": "application/json"}
            )
        r.raise_for_status()
        data = r.json()
        try:
            return data["data"]["outputs"].get("oss_img_url")
        except Exception:
            logger.exception("解析单图配图返回失败")
            return None

    async def run_keyword_select(self, creative_elements: List[Any], keywords: List[str]) -> Optional[str]:
        """调用dify工作流进行关键词挑选"""
        payload = {
            "inputs": {
                "creative_elements": creative_elements,
                "keywords": keywords,
            },
            "response_mode": "blocking", "user": "workflows"
        }
        async with self._sem:
            r = await self._client.post(
                DIFY_RUN_URL, json=payload,
                headers={"Authorization": f"Bearer {DIFY_TOKEN_KEYWORD_SELECT}", "Content-Type": "application/json"}
            )
        r.raise_for_status()
        data = r.json()
        try:
            return data["data"]["outputs"].get("selected_keyword")
        except Exception:
            logger.exception("解析关键词挑选返回失败")
            return None

    async def run_xhs_cover_generation(self, title: str, content: str) -> Optional[Dict[str, Any]]:
        """调用大模型输出xhs_cover_data内容"""
        # TODO: 实现大模型调用逻辑
        return {
            "cover_style": "default",
            "color_scheme": "warm",
            "layout": "center"
        }
