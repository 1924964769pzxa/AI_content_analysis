import asyncio
import json
import logging
import random
from typing import Dict, List, Optional, Any

from content_generate.clients.api import APIClient
from content_generate.schemas import (
    GenerateItem, GenerateResult, Persona,
    MaterialItem, MaterialAnalysisData, MaterialAnalysis, MaterialNoteRaw
)
from content_generate.utils.helpers import (
    dedup_preserve_order, pick_one, to_json_str, parse_image_list, get_title_from_material, safe_first_image_url
)
from content_generate.config import ALLOW_SINGLE_WHEN_ONLY_ONE_MATERIAL

logger = logging.getLogger("content_generate.service")


def to_material_item(obj: dict) -> MaterialItem:
    try:
        return MaterialItem.parse_obj(obj)
    except Exception:
        note_raw = obj.get("note_raw_data") or {}
        analysis_data = obj.get("analysis_data") or {}
        return MaterialItem(
            id=obj.get("id", -1),
            keyword=obj.get("keyword"),
            note_raw_data=MaterialNoteRaw(**note_raw) if isinstance(note_raw, dict) else None,
            analysis_data=MaterialAnalysisData(
                note_id=(analysis_data or {}).get("note_id"),
                analysis=MaterialAnalysis(**((analysis_data or {}).get("analysis") or {}))
                if isinstance((analysis_data or {}).get("analysis"), dict) else None,
            ) if isinstance(analysis_data, dict) else None
        )


class ContentGenerateService:
    """
    现在 data 数组中每个元素就是“一个人设”的任务。
    业务策略：
      - 为每个 item.personas 随机挑选 1 个 tag
      - 汇总去重后批量确保 keywords ready
      - 对每个 persona：随机 1 个 keyword -> 搜素材 -> 选 2 篇参考文 -> 类型判断 -> 写稿 -> 配图
      - 无素材则创建抓取任务并结束该 persona 流程
    """
    def __init__(self):
        self.client = APIClient()  # 复用同一 httpx 客户端

    async def shutdown(self):
        await self.client.close()

    # 被回调路由调用
    def mark_keyword_ready(self, tag: str):
        self.client.keyword_ready_flags[tag] = True
        logger.info(f"[callback] tag={tag} 标记 ready")

    async def run_batch(self, items: List[GenerateItem]) -> List[GenerateResult]:
        if not items:
            return []

        # Step1：从每个人设随机挑选 tag，做去重，准备关键词
        persona_selected_tag: Dict[int, str] = {}
        all_selected_tags: List[str] = []
        for idx, item in enumerate(items):
            tag = pick_one(item.personas.tag)
            if not tag:
                logger.warning(f"[item#{idx}] personas.tag 为空，跳过该人设")
                continue
            persona_selected_tag[idx] = tag
            all_selected_tags.append(tag)

        unique_tags = dedup_preserve_order(all_selected_tags)
        logger.info(f"[Step1] 批次选中 tags={unique_tags}")

        # 确保每个 tag 能检索到 keywords（先查 -> 补齐 -> 轮询/回调）
        tag_keywords_map = await self.client.ensure_keywords_ready(unique_tags)

        results: List[GenerateResult] = []

        # Step2~4：逐人设处理
        for idx, item in enumerate(items):
            if idx not in persona_selected_tag:
                continue

            tag = persona_selected_tag[idx]
            keywords = tag_keywords_map.get(tag, [])
            if not keywords:
                logger.error(f"[item#{idx}] tag={tag} 未获取到关键词，跳过")
                continue

            kw = random.choice(keywords)
            if not isinstance(kw, str):
                kw = str(kw or "")
            logger.info(f"[item#{idx}] tag={tag} -> keyword={kw}")

            # 素材检索
            mats_raw = await self.client.search_materials_by_keyword(kw)
            materials = [to_material_item(m) for m in mats_raw]

            if not materials:
                # 无数据：创建任务并结束
                await self.client.create_material_search_task(keyword=kw, kox_task_id=item.task_id)
                logger.info(f"[item#{idx}] 无素材，已创建抓取任务，流程结束")
                continue

            # 选择两篇参考文（不足 2 篇时按配置可走 single）
            pick_count = 2 if len(materials) >= 2 else (1 if ALLOW_SINGLE_WHEN_ONLY_ONE_MATERIAL else 0)
            if pick_count == 0:
                logger.info(f"[item#{idx}] 素材不足 2 篇且不允许 single 链路，结束")
                continue

            refs = random.sample(materials, k=pick_count) if pick_count == 2 else [materials[0]]
            if len(refs) == 1:
                refs = refs + [refs[0]]  # 复制一份，后续走 single

            # 类型判断（两篇首图）
            img1 = safe_first_image_url(refs[0])
            img2 = safe_first_image_url(refs[1])
            type_detect = await self.client.run_type_detect(img1_url=img1, img2_url=img2)
            type_one = (type_detect.get("data", {}).get("outputs", {}) or {}).get("type_one")
            type_two = (type_detect.get("data", {}).get("outputs", {}) or {}).get("type_two")
            logger.info(f"[item#{idx}] type_one={type_one} type_two={type_two}")

            route = "combination"
            if type_one and type_two and type_one == type_two:
                route = type_one
            if not (type_one and type_two) or type_one != type_two:
                route = "combination"  # 不一致统一 combination

            # 写稿入参
            character_str = to_json_str(item.personas.dict())

            def content_disassembly_of(m: MaterialItem) -> Any:
                return (m.analysis_data.analysis.content_disassembly
                        if (m.analysis_data and m.analysis_data.analysis) else None)

            if route == "single":
                chosen = random.choice(refs)
                cs = content_disassembly_of(chosen) or {}
                title = get_title_from_material(chosen)
                write_resp = await self.client.run_single_write(
                    character=character_str,
                    content_structure=to_json_str(cs),
                    title_list=to_json_str(title),
                )
            else:
                cs1 = content_disassembly_of(refs[0]) or {}
                cs2 = content_disassembly_of(refs[1]) or {}
                titles = [get_title_from_material(refs[0]), get_title_from_material(refs[1])]
                write_resp = await self.client.run_combo_write(
                    character=character_str,
                    content_structure=to_json_str([cs1, cs2]),
                    title_list=to_json_str(titles),
                )

            # 解析写稿结果
            out_title, out_content = "", ""
            try:
                out_payload = write_resp["data"]["outputs"]
                out_title = out_payload.get("title", "") or ""
                out_content = out_payload.get("content", "") or ""
            except Exception:
                logger.exception(f"[item#{idx}] 解析写稿返回失败")

            # Step3：配图
            img_list: List[str] = []
            if route == "single":
                chosen = random.choice(refs)
                all_urls = parse_image_list(
                    chosen.note_raw_data.image_list if (chosen.note_raw_data and chosen.note_raw_data.image_list) else ""
                )
                for u in all_urls:
                    oss = await self.client.run_single_image(u)
                    if oss:
                        img_list.append(oss)
                logger.info(f"[item#{idx}] 配图完成 {len(img_list)} 张")
            else:
                img_list = []  # TODO: combination 链路配图跳过

            # Step4：汇总
            result = GenerateResult(
                task_id=item.task_id,
                title=out_title,
                content=out_content,
                img_list=img_list,
            )
            # 控制台输出（按你的要求）
            print(json.dumps(result.dict(), ensure_ascii=False, indent=2))
            results.append(result)

        return results
