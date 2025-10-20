import asyncio
import logging
import random
from typing import List, Any, Dict
from asyncio import Semaphore

from content_generate.clients.api import APIClient
from content_generate.schemas import (
    GenerateItem, GenerateResult, Persona, PersonalData,
    MaterialItem, MaterialAnalysisData, MaterialAnalysis, MaterialNoteRaw
)
from content_generate.utils.helpers import (
    to_json_str, get_title_from_material, safe_first_image_url
)

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
    业务逻辑：
    - 支持涨粉与种草两种 content_type
    - 并发控制（Semaphore=10），避免阻塞主线程
    - 涨粉流程：关键词 -> 写稿 ->（仅 combo 类型）批量配图
    - 变更点：
        1) 配图仅适用于 combo 类型；single 类型保留 TODO，不做配图
        2) 配图按批次，每批最多 20 篇文章，超出自动分片多次请求
        3) xhs_account_name 为空；xhs_topic 使用大模型返回的 topic（多重容错）
    """
    def __init__(self):
        self.client = APIClient()
        self.semaphore = Semaphore(10)

        # ---- 批处理配图缓冲（仅存 combo 类型） ----
        # key: task_id(str), val: dict(xhs_title/xhs_content/xhs_topic/xhs_cover_data/...)
        self._picture_buffer: Dict[str, Dict[str, Any]] = {}
        self._buffer_lock = asyncio.Lock()

    async def shutdown(self):
        await self.client.close()

    def mark_keyword_ready(self, tag: str):
        self.client.keyword_ready_flags[tag] = True
        logger.info(f"[callback] tag={tag} 标记 ready")

    async def run_batch(self, items: List[GenerateItem]) -> List[GenerateResult]:
        """
        批处理入口：
        1) 并发执行各 item 的写稿流程（仅 combo 类型将配图数据写入缓冲）
        2) 全部写稿完成后，统一从缓冲拉取待配图的文章，按 20 条/批 分片调用配图
        3) 若你的配图任务有返回/落库，可在回填区写回 GenerateResult.img_list
        """
        if not items:
            return []

        # --- Step 1: 并发写稿（不立即配图） ---
        tasks = [asyncio.create_task(self._process_single_item(item)) for item in items]
        results_or_ex = await asyncio.gather(*tasks, return_exceptions=True)

        results: List[GenerateResult] = []
        for r in results_or_ex:
            if isinstance(r, GenerateResult):
                results.append(r)
            else:
                logger.exception(f"任务执行异常: {r}")

        # --- Step 2: 统一批量配图（仅 combo 类型进入缓冲） ---
        async with self._buffer_lock:
            picture_data_all = []
            for _, data in self._picture_buffer.items():
                # 每个 dict 即一篇文章
                picture_data_all.append({
                    "xhs_note_ID": data.get("xhs_note_ID", 1),
                    "xhs_title": data.get("xhs_title", ""),
                    "xhs_content": data.get("xhs_content", ""),
                    # 置空账号名
                    "xhs_account_name": "",
                    # topic 使用大模型返回（若为空则留空字符串）
                    "xhs_topic": data.get("xhs_topic", ""),
                    "xhs_cover_bg": data.get("xhs_cover_bg", ""),
                    "xhs_cover_data": data.get("xhs_cover_data", {})  # 允许为空字典
                })

        # 分片逻辑：每批最多 20 条
        BATCH_LIMIT = 20
        if picture_data_all:
            try:
                from content_generate.xhs_notes_gen_group_task_commit_demo_v3 import async_main as picture

                # 将待配图文章按 20 条一组切片
                for start in range(0, len(picture_data_all), BATCH_LIMIT):
                    batch = picture_data_all[start:start + BATCH_LIMIT]

                    # 异步调用你的配图任务（不阻塞主线程）
                    await picture(
                        src_json=batch,
                        dst_json="data_input_format.json",
                        task_type="xhs_note_gen_group",
                        user_name="root",
                        start_index=0,
                        end_index=len(batch),
                        polling_interval=30,
                        max_polling_attempts=240,
                        return_final_status=True,
                    )
                    logger.info(f"配图完成：第 {start // BATCH_LIMIT + 1} 批，共 {len(batch)} 篇")

                    # === 回填区（可选）===
                    # 如果 picture 任务有可用返回或已落库，请在此处查询并把图片地址写回对应的 GenerateResult：
                    # pic_map = {title: ["img1.png", "img2.png"]}
                    # for r in results:
                    #     if r.title in pic_map:
                    #         r.img_list = pic_map[r.title]

            except Exception as e:
                logger.exception(f"批量配图失败: {e}")

        return results

    async def _process_single_item(self, item: GenerateItem) -> GenerateResult:
        """单个任务：仅写稿 + 准备（combo）配图所需数据，不直接配图"""
        async with self.semaphore:
            try:
                if item.content_type == "涨粉":
                    return await self._process_fan_growth(item)
                elif item.content_type == "种草":
                    return await self._process_product_recommendation(item)
                else:
                    logger.warning(f"未知的content_type: {item.content_type}")
                    return GenerateResult(
                        task_id=str(item.content_creation_id),
                        title="",
                        content="",
                        img_list=[]
                    )
            except Exception as e:
                logger.exception(f"处理任务失败: {e}")
                return GenerateResult(
                    task_id=str(item.content_creation_id),
                    title="",
                    content="",
                    img_list=[]
                )

    async def _process_fan_growth(self, item: GenerateItem) -> GenerateResult:
        """
        涨粉逻辑（写稿 + 准备配图数据）：
        - 仅当 route == "combination"（即 combo）时，将配图所需数据写入缓冲
        - single 类型：保留 TODO，不做配图
        """
        logger.info(f"开始处理涨粉任务: {item.content_creation_id}")

        # 1. 人设兜底
        if not item.personal_data or not item.personal_data[0].personas.tag:
            default_persona = Persona(tag=["通用"])
            default_personal_data = PersonalData(personas=default_persona)
            item.personal_data = [default_personal_data]

        # 2. 关键词收集
        all_keywords = []
        for personal_data in item.personal_data:
            for tag in personal_data.personas.tag:
                keywords = await self.client.get_keywords(tag)
                all_keywords.extend(keywords)

        if not all_keywords:
            logger.warning(f"未获取到关键词，任务结束: {item.content_creation_id}")
            return GenerateResult(task_id=str(item.content_creation_id), title="", content="", img_list=[])

        # 3. 关键词挑选
        selected_keyword = await self.client.run_keyword_select(
            creative_elements=item.creative_elements,
            keywords=all_keywords
        )
        if not selected_keyword:
            logger.warning(f"关键词挑选失败，任务结束: {item.content_creation_id}")
            return GenerateResult(task_id=str(item.content_creation_id), title="", content="", img_list=[])

        logger.info(f"选中关键词: {selected_keyword}")

        # 4. 搜索素材并转模型
        mats_raw = await self.client.search_materials_by_keyword(selected_keyword)
        materials = [to_material_item(m) for m in mats_raw]
        if not materials:
            logger.warning(f"无素材，任务结束: {item.content_creation_id}")
            return GenerateResult(task_id=str(item.content_creation_id), title="", content="", img_list=[])

        # 5. 参考文挑选
        pick_count = min(2, len(materials))
        refs = random.sample(materials, k=pick_count)
        if len(refs) == 1:
            refs = refs + [refs[0]]

        # 6. 类型判断
        img1 = safe_first_image_url(refs[0])
        img2 = safe_first_image_url(refs[1])
        type_detect = await self.client.run_type_detect(img1_url=img1, img2_url=img2)
        type_one = (type_detect.get("data", {}).get("outputs", {}) or {}).get("type_one")
        type_two = (type_detect.get("data", {}).get("outputs", {}) or {}).get("type_two")

        route = "combination"
        if type_one and type_two and type_one == type_two:
            route = type_one
        if not (type_one and type_two) or type_one != type_two:
            route = "combination"

        # 7. 写稿
        character_str = to_json_str(item.personal_data[0].personas.dict())

        def content_disassembly_of(m: MaterialItem) -> Any:
            return (m.analysis_data.analysis.content_disassembly
                    if (m.analysis_data and m.analysis_data.analysis) else None)

        if route == "single":
            # --- single 类型：照常写稿，但配图不做，标为 TODO ---
            chosen = random.choice(refs)
            cs = content_disassembly_of(chosen) or {}
            title = get_title_from_material(chosen)
            write_resp = await self.client.run_single_write(
                character=character_str,
                content_structure=to_json_str(cs),
                title_list=to_json_str(title),
                language_style=item.language_style,
                title_requirement=item.title_requirement,
                content_requirement=item.content_requirement,
                creative_elements=item.creative_elements
            )
        else:
            cs1 = content_disassembly_of(refs[0]) or {}
            cs2 = content_disassembly_of(refs[1]) or {}
            titles = [get_title_from_material(refs[0]), get_title_from_material(refs[1])]
            write_resp = await self.client.run_combo_write(
                character=character_str,
                content_structure=to_json_str([cs1, cs2]),
                title_list=to_json_str(titles),
                language_style=item.language_style,
                title_requirement=item.title_requirement,
                content_requirement=item.content_requirement,
                creative_elements=item.creative_elements
            )

        # 解析写稿结果（容错）
        out_title, out_content, out_topic = "", "", ""
        try:
            outputs = (write_resp or {}).get("data", {}).get("outputs", {})
            if isinstance(outputs, dict):
                out_title = outputs.get("title") or ""
                out_content = outputs.get("content") or ""
                out_topic = outputs.get("topic") or ""  # 若写稿已经提供 topic，优先使用
        except Exception:
            logger.exception("解析写稿返回失败（已兜底为空）")

        # 8. 仅当 combo 时，准备配图所需数据；single 标为 TODO，不入缓冲
        if route == "combination":
            xhs_cover_data = {}
            try:
                cover_resp = await self.client.run_xhs_cover_generation(out_title, out_content)
                cover_outputs = (cover_resp or {}).get("data", {}).get("outputs", {})
                if isinstance(cover_outputs, dict):
                    xhs_cover_data = cover_outputs
                    # 若写稿未给到 topic，这里做兜底
                    if not out_topic:
                        out_topic = str(cover_outputs.get("topic", "")) if "topic" in cover_outputs else ""
            except Exception:
                logger.exception("封面生成数据失败（xhs_cover_data 置为空字典）")
                xhs_cover_data = {}

            # 写入缓冲（仅 combo 入缓冲）
            async with self._buffer_lock:
                self._picture_buffer[str(item.content_creation_id)] = {
                    "xhs_note_ID": 1,
                    "xhs_title": out_title or "",
                    "xhs_content": out_content or "",
                    "xhs_topic": out_topic or "",
                    "xhs_account_name": "",  # 按需置空
                    "xhs_cover_bg": "",
                    "xhs_cover_data": xhs_cover_data or {}
                }
        else:
            # single 类型：此处显式标注 TODO，不参与配图批处理
            logger.info(f"任务 {item.content_creation_id} 为 single 类型，配图逻辑暂为 TODO，未加入批量配图。")

        # 返回写稿结果；img_list 等配图完成后若需要可在 run_batch 回填
        result = GenerateResult(
            task_id=str(item.content_creation_id),
            title=out_title,
            content=out_content,
            img_list=[]
        )
        logger.info(f"涨粉任务(写稿完成){'(combo已入配图缓冲)' if route=='combination' else '(single配图TODO)'}: {item.content_creation_id}")
        return result

    async def _process_product_recommendation(self, item: GenerateItem) -> GenerateResult:
        """种草逻辑（待实现）"""
        logger.info(f"种草逻辑待实现: {item.content_creation_id}")
        return GenerateResult(task_id=str(item.content_creation_id), title="", content="", img_list=[])
