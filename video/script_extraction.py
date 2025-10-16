import asyncio
import os
import re
import json
from typing import Optional, Any, Dict

from fastapi import FastAPI, HTTPException, APIRouter, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
import httpx
from starlette.responses import StreamingResponse

# ====== 共享实例与模型映射：来自 helper_examples（已初始化好的全局共享对象） ======
# 注意：遵循你的工程约定，不重复新建 mongodb_helper / client / semaphore 实例
from helper_examples import  client, semaphore, MODEL_MAPPING  # :contentReference[oaicite:2]{index=2}

# ----------------- 常量区 -----------------
XHS_EXTRACT_URL = "https://test2.api.moglas.cn/script/extract_script/anonymous"
CALLBACK_URL = "https://test2.api.moglas.cn/script/callback"

# 大模型系统提示词
SYSTEM_PROMPT = "你是一个视频脚本拆解师，根据传入的视频解析出视频脚本。"

# 默认选择一个支持视觉的视频理解模型（你也可以切到 Doubao-VL）
MODEL_TO_USE = "Doubao-VL"   # 视觉多模态模型键名，必须存在于 MODEL_MAPPING 中

# ----------------- FastAPI 路由 -----------------
route = APIRouter(prefix="/v1/script_extraction", tags=["脚本提取"])

class ExtractRequest(BaseModel):
    note_url: HttpUrl
    platform_type: str = "xhs"

class ProcessRequest(BaseModel):
    note_url: HttpUrl


# ============== 工具函数 ==============

def _extract_first_json_block(text: str) -> Optional[Dict[str, Any]]:
    """
    从大模型输出里尽量提取第一段 {...} JSON（容错解析），若失败返回 None。
    适合模型返回“部分说明 + JSON”的场景。
    """
    if not text:
        return None
    try:
        # 粗略匹配第一段花括号体
        m = re.search(r"\{.*?\}", text, flags=re.S)
        if not m:
            return None
        candidate = m.group(0)
        # 进一步做括号配平（简单策略：扩大到最后一个'}'）
        last_r = text.rfind("}")
        if last_r != -1:
            candidate = text[text.find("{"): last_r + 1]
        return json.loads(candidate)
    except Exception:
        return None


async def call_xhs_extract(note_url: str, platform_type: str = "xhs") -> Dict[str, Any]:
    """
    调用脚本提取接口，拿到 {meta, video_url...}
    """
    payload = {"note_url": note_url, "platform_type": platform_type}
    headers = {"accept": "application/json", "Content-Type": "application/json"}
    # 高超时 + 读取超时
    timeout = httpx.Timeout(3000.0, read=3000.0)
    async with httpx.AsyncClient(timeout=timeout) as session:
        r = await session.post(XHS_EXTRACT_URL, headers=headers, json=payload)
        try:
            data = r.json()
        except Exception:
            raise HTTPException(status_code=502, detail="上游提取接口返回了非 JSON 响应")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"上游提取接口错误: {r.status_code}")
        if not isinstance(data, dict) or data.get("code") != 0:
            raise HTTPException(status_code=502, detail=f"脚本提取失败: {data}")
        return data


async def call_doubao_vision_for_script(
    video_url: str,
    model_key: Optional[str] = None,
    fps: float = 1.0,
) -> Dict[str, Any]:
    """
    使用 Doubao 视觉模型对视频进行脚本解析。
    - 遵守共享实例与并发控制：
        * client 来自 helper_examples
        * semaphore 控并发
        * client.chat.completions.create 为同步函数，需放入线程池
    - 返回：{ "raw_text": str, "parsed_json": dict|None, "usage": dict|None }
    """
    model_key = model_key or MODEL_TO_USE
    if model_key not in MODEL_MAPPING:
        raise HTTPException(status_code=400, detail=f"模型键名 {model_key} 不在 MODEL_MAPPING 中")

    # 视觉消息体：传入视频 URL + 抽帧帧率
    user_content = [
        {
            "type": "video_url",
            "video_url": {
                "url": video_url,
                "fps": fps,  # 每秒抽取若干帧用于理解
            }
        },
        {"type": "text", "text": "请解析该视频的脚本要点、分镜、台词/旁白（如有），并结构化输出。"}
    ]

    # 线程池执行同步 SDK，避免阻塞主线程
    async with semaphore:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=MODEL_MAPPING[model_key],
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ]
        )

    # 支持深度推理链内容（如模型有返回）
    reasoning_content = getattr(response.choices[0].message, "reasoning_content", None)
    raw_text = response.choices[0].message.content or ""

    # 解析 usage（统计 token）
    usage = getattr(response, "usage", None)
    total_tokens = getattr(usage, "total_tokens", None) if usage else None


    parsed_json = _extract_first_json_block(raw_text)

    return {
        "raw_text": raw_text,
        "parsed_json": parsed_json,
        "usage": dict(usage) if usage else None,
        "total_tokens": total_tokens
    }


async def post_callback(task_id: int, meta: Dict[str, Any], doubao_text: str) -> Dict[str, Any]:
    """
    按你给的回调协议，把结果回调到指定接口。
    """
    payload = {
        "task_id": task_id or 0,
        "meta": meta or {},
        "dify_text": doubao_text or ""  # 字段名保持你的协议（虽然我们不用 Dify）
    }
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Content-Type": "application/json",
        "User-Agent": "FastAPI-Client/1.0"
    }
    timeout = httpx.Timeout(60.0, read=60.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as session:
        r = await session.post(CALLBACK_URL, headers=headers, json=payload)
        try:
            data = r.json()
        except Exception:
            raise HTTPException(status_code=502, detail="回调返回了非 JSON 响应")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail={"message": "回调失败", "response": data})
        return data


# ============== 业务路由实现 ==============

@route.post("/api/process",summary="脚本提取接口")
async def process(req: ProcessRequest):
    """
    1) 调用脚本提取接口获取 meta & video_url
    2) 使用 Doubao 视觉模型解析视频脚本
    3) 将解析文本拼成最终输出（此处直接使用模型输出，你也可二次规整化）
    4) 回调到指定接口（带上 task_id / meta / dify_text）
    5) 返回服务端确认
    """
    # 1) 提取
    extract_resp = await call_xhs_extract(str(req.note_url), "xhs")
    data = extract_resp.get("data") or {}
    video_url: Optional[str] = data.get("video_url")
    task_id: Optional[int] = data.get("task_id")
    if not video_url:
        raise HTTPException(status_code=400, detail="未能从提取结果中获取 video_url")

    # 2) 调 Doubao 视觉模型
    doubao_result = await call_doubao_vision_for_script(
        video_url=video_url,
        model_key=MODEL_TO_USE,
        fps=1.0,
    )
    # 3) 拼接为最终输出（这里直接使用 raw_text；若 parsed_json 存在，你也可以转 Markdown 等再拼接）
    final_text = doubao_result["raw_text"] or ""

    # 4) 回调
    callback_resp = await post_callback(
        task_id=task_id,
        meta=data,
        doubao_text=final_text
    )

    # 5) 返回
    return {
        "code": 0,
        "msg": "ok",
        "xhs_meta": data,
        "doubao_text": final_text,
        "callback_result": callback_resp,
        "model_used": req.model_key or MODEL_TO_USE,
        "total_tokens": doubao_result.get("total_tokens"),
    }


@route.get("/api/health")
async def health():
    return {"status": "ok"}


@route.get("/api/proxy/video")
async def proxy_video(url: str, request: Request):
    """
    以代理方式拉取远程视频，解决某些 CDN 防盗链 / CORS 播放问题。
    简化的 Range 支持：转发 Range 头到上游，回填关键响应头。
    """
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="invalid url")

    range_header = request.headers.get("range")
    headers = {}
    if range_header:
        headers["Range"] = range_header

    timeout = httpx.Timeout(60.0, read=60.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}) as session:
        try:
            r = await session.get(url, headers=headers, follow_redirects=True)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"获取视频失败: {e}")

        # 透传的关键响应头
        passthrough = {}
        for h in ["Content-Type", "Content-Length", "Accept-Ranges", "Content-Range", "Content-Disposition"]:
            if h in r.headers:
                passthrough[h] = r.headers[h]

        status_code = 206 if r.status_code == 206 or range_header else 200

        async def iter_bytes():
            async for chunk in r.aiter_bytes():
                yield chunk

        return StreamingResponse(
            iter_bytes(),
            status_code=status_code,
            headers=passthrough,
            media_type=r.headers.get("Content-Type", "video/mp4")
        )
