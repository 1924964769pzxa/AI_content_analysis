"""
* @Description: 用于测试小红书笔记_group 接口 - 增加封面参数的输入
* @Author: rkwork
* @Date: 2025-09-04
* @LastEditTime: 2025-09-26
"""
# send_group_async.py
import asyncio
import json
import httpx
from typing import Any, Dict, Optional, Tuple, Union, List
from pathlib import Path
import time
from datetime import datetime

# =====================
# 全局变量配置（默认值，可被 main/async_main 覆盖）
# =====================
TASK_COMMIT_API_URL = "http://47.121.125.128:7002/v1/xhs_task/frontend_rbg_task_submit_group"    # 提交任务接口
TASK_STATUS_API_URL = "http://47.121.125.128:7002/v1/xhs_task/frontend_rbg_tasks_full/root"      # 任务查询接口
TASK_LISTENER_API_URL = "http://47.121.125.128:7000/api/v1/tasks/task_listener"                  # 监听器查询接口

GLOBAL_USER_NAME = "root"
GLOBAL_TASK_TYPE = "xhs_note_gen_group"
GLOBAL_JSON_PATH = "data_input.json"
GLOBAL_START_INDEX = 0
GLOBAL_END_INDEX = 0

POLLING_INTERVAL = 60          # 轮询间隔（秒）
MAX_POLLING_ATTEMPTS = 120     # 最大轮询次数（2小时）


# =====================
# 原有功能函数（略微增强健壮性）
# =====================
async def send_group(
    user_name: str = GLOBAL_USER_NAME,
    task_type: str = GLOBAL_TASK_TYPE,
    json_path: str = GLOBAL_JSON_PATH,
    start: int = GLOBAL_START_INDEX,
    end: Optional[int] = GLOBAL_END_INDEX
) -> Dict[str, Any]:
    """
    提交任务组
    """
    print(f"📝 开始提交任务...")
    print(f"   用户: {user_name}")
    print(f"   任务类型: {task_type}")
    print(f"   数据范围: [{start}:{end}]")

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 兼容 end=0 视为“到末尾”
    if end in (None, 0):
        end = len(data)

    payload = {
        "userName": user_name,
        "taskType": task_type,
        "data": data[start:end],
    }

    print(f"📊 提交数据条数: {len(payload['data'])}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(TASK_COMMIT_API_URL, json=payload)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")

        try:
            body = resp.json()
        except Exception:
            body = {"raw_text": resp.text}

        print("✅ 任务提交响应:", body)
        return body


async def get_task_listener_status(task_id: str) -> Dict[str, Any]:
    """
    根据 taskID 查询 task_listener 表数据
    注意：返回体中没有 ok 字段，需要根据实际响应结构判断
    """
    url = f"{TASK_LISTENER_API_URL}/{task_id}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError:
                return {"raw_text": resp.text, "error": "Invalid JSON response"}
        except httpx.HTTPStatusError as e:
            return {"error": True, "status": e.response.status_code, "message": e.response.text[:2000]}
        except httpx.RequestError as e:
            return {"error": True, "message": f"{type(e).__name__}: {e}"}


async def get_task_status(
    url: str = TASK_STATUS_API_URL,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """
    异步 GET 请求，返回 JSON（若非 JSON，则返回 raw_text）
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError:
                return {"raw_text": resp.text}
        except httpx.HTTPStatusError as e:
            return {"ok": False, "status": e.response.status_code, "error": e.response.text[:2000]}
        except httpx.RequestError as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def wait_for_task_completion(task_id: str) -> bool:
    """
    轮询等待任务完成
    """
    print(f"\n🔄 开始监控任务 {task_id} 的执行状态...")
    print(f"   轮询间隔: {POLLING_INTERVAL}秒")
    print(f"   最大等待时间: {MAX_POLLING_ATTEMPTS * POLLING_INTERVAL // 60}分钟")

    start_time = time.time()
    attempt = 0

    while attempt < MAX_POLLING_ATTEMPTS:
        attempt += 1
        current_time = datetime.now().strftime("%H:%M:%S")

        listener_data = await get_task_listener_status(task_id)

        if listener_data.get("error"):
            print(f"❌ [{current_time}] 第{attempt}次查询失败: {listener_data.get('message', '未知错误')}")
        else:
            task_status = str(listener_data.get("taskStatus", "unknown"))
            completion_count = int(listener_data.get("taskCompletionCount", 0))
            total_number = int(listener_data.get("taskWorkerTotalNumber", 0))

            # 进度条
            if total_number > 0:
                progress = (completion_count / total_number) * 100
                progress_bar = "█" * int(progress // 5) + "░" * (20 - int(progress // 5))
                progress_text = f"[{progress_bar}] {progress:.1f}% ({completion_count}/{total_number})"
            else:
                progress_text = f"完成数量: {completion_count}"

            print(f"📊 [{current_time}] 第{attempt}次查询 - 状态: {task_status} | {progress_text}")

            # 完成
            if task_status.lower() == "completed":
                elapsed_time = time.time() - start_time
                print(f"🎉 任务 {task_id} 已完成！")
                print(f"   总耗时: {elapsed_time // 60:.0f}分{elapsed_time % 60:.0f}秒")
                print(f"   完成任务数: {completion_count}/{total_number}")
                return True

            # 失败
            if task_status.lower() in {"failed", "error", "cancelled"}:
                print(f"❌ 任务 {task_id} 执行失败，状态: {task_status}")
                return False

        if attempt < MAX_POLLING_ATTEMPTS:
            print(f"⏰ 等待 {POLLING_INTERVAL} 秒后进行下次查询...")
            await asyncio.sleep(POLLING_INTERVAL)

    print(f"⏰ 任务 {task_id} 监控超时（超过 {MAX_POLLING_ATTEMPTS} 次查询）")
    return False


def update_global_config(**kwargs):
    """
    更新全局配置参数
    """
    global GLOBAL_USER_NAME, GLOBAL_TASK_TYPE, GLOBAL_JSON_PATH
    global GLOBAL_START_INDEX, GLOBAL_END_INDEX
    global POLLING_INTERVAL, MAX_POLLING_ATTEMPTS

    if 'user_name' in kwargs:
        globals()['GLOBAL_USER_NAME'] = kwargs['user_name']
        print(f"✅ 更新用户名: {GLOBAL_USER_NAME}")

    if 'task_type' in kwargs:
        globals()['GLOBAL_TASK_TYPE'] = kwargs['task_type']
        print(f"✅ 更新任务类型: {GLOBAL_TASK_TYPE}")

    if 'json_path' in kwargs:
        globals()['GLOBAL_JSON_PATH'] = kwargs['json_path']
        print(f"✅ 更新JSON路径: {GLOBAL_JSON_PATH}")

    if 'start_index' in kwargs:
        globals()['GLOBAL_START_INDEX'] = kwargs['start_index']
        print(f"✅ 更新开始索引: {GLOBAL_START_INDEX}")

    if 'end_index' in kwargs:
        globals()['GLOBAL_END_INDEX'] = kwargs['end_index']
        print(f"✅ 更新结束索引: {GLOBAL_END_INDEX}")

    if 'polling_interval' in kwargs:
        globals()['POLLING_INTERVAL'] = kwargs['polling_interval']
        print(f"✅ 更新轮询间隔: {POLLING_INTERVAL}秒")

    if 'max_polling_attempts' in kwargs:
        globals()['MAX_POLLING_ATTEMPTS'] = kwargs['max_polling_attempts']
        print(f"✅ 更新最大轮询次数: {MAX_POLLING_ATTEMPTS}")


# =====================
# 新增：数据预处理（将 data_input_task.json 标准化为 data_input_format.json）
# =====================
def build_formatted_input(
    src: Union[str, Path, List[Dict[str, Any]], Dict[str, Any]],
    dst_path: str,
    *,
    default_user_info: Optional[Dict[str, Any]] = None,
) -> str:
    """
    读取 src（可以是文件路径、JSON 字符串或 Python 对象 list/dict），
    格式化为发送接口需要的数组结构，写入 dst_path。
    返回写入后的 dst_path。
    """
    default_user_info = default_user_info or {
        "userName": "root",
        "taskType": "xhs_note_gen",
        "templateType": "generation",
        "xhs_cover_gen_command": {
            "expression_option": True,
            "xhs_cover_text_align_mode": "left",
        }
    }

    # 1) 统一把 src 解析为 Python 对象 raw
    raw: Union[List[Dict[str, Any]], Dict[str, Any]]
    if isinstance(src, (str, Path)):
        p = Path(src)
        if p.exists():  # 1.1 文件路径
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
        else:  # 1.2 不是文件，则尝试把它当 JSON 字符串解析
            if isinstance(src, Path):
                raise ValueError(f"路径不存在：{src}")
            try:
                raw = json.loads(src)
            except Exception as e:
                raise ValueError(
                    "传入的 src 既不是存在的文件路径，也不是合法的 JSON 字符串。"
                ) from e
    elif isinstance(src, (list, dict)):  # 1.3 已经是 Python 对象
        raw = src
    else:
        raise TypeError("src 必须是文件路径/JSON 字符串/list/dict 之一")

    # 2) 规整为 list[dict]
    if isinstance(raw, dict):
        if isinstance(raw.get("data"), list):
            items = raw["data"]
        elif isinstance(raw.get("items"), list):
            items = raw["items"]
        else:
            # 单对象也支持：自动包装成列表
            items = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("解析后的 JSON 不是 list 或 dict")

    # 3) 构造标准化数组
    formatted: List[Dict[str, Any]] = []
    for item in items:
        # 容错：确保 item 是 dict
        if not isinstance(item, dict):
            raise ValueError("列表元素必须是对象（dict）")
        formatted.append({
            "userInfo": default_user_info,
            "xhs_cover_bg": item.get("xhs_cover_bg", ""),
            "xhs_cover_data": item.get("xhs_cover_data", {}),
            "xhs_data": {
                "xhs_title": item.get("xhs_title", ""),
                "xhs_topic": item.get("xhs_topic", ""),
                "xhs_content": item.get("xhs_content", ""),
                "xhs_note_ID": item.get("xhs_note_ID", ""),
                "xhs_account_name": item.get("xhs_account_name", ""),
            }
        })

    Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
    with open(dst_path, "w", encoding="utf-8") as f:
        json.dump(formatted, f, ensure_ascii=False, indent=4)

    print(f"🧩 已生成标准化输入文件: {dst_path}（{len(formatted)} 条）")
    return dst_path

# =====================
# 新增：对外可复用的入口
# =====================
async def async_main(
    *,
    # 1) 数据预处理（可选）
    src_json: Optional[Union[str, Path, List[Dict[str, Any]], Dict[str, Any]]] = None,  # ← 支持路径/JSON串/Python对象
    dst_json: Optional[str] = None,
    default_user_info: Optional[Dict[str, Any]] = None,

    # 2) 直接使用已格式化好的文件
    json_path: Optional[str] = None,

    # 3) 任务配置
    task_type: str = "xhs_note_gen_group",
    user_name: str = "root",
    start_index: int = 0,
    end_index: Optional[int] = 0,
    polling_interval: int = 60,
    max_polling_attempts: int = 240,

    # 4) 返回更多信息
    return_final_status: bool = True,
) -> Dict[str, Any]:
    """
    异步入口：可供已有事件循环（如 FastAPI、Jupyter）直接 await 调用
    返回：{'success': bool, 'task_id': str|None, 'final_status': dict|None}
    """
    use_json_path = json_path
    if src_json is not None:
        target = dst_json or json_path or "data_input_format.json"
        use_json_path = build_formatted_input(src_json, target, default_user_info=default_user_info)

    if not use_json_path:
        raise ValueError("必须提供 json_path 或 src_json 用于构建发送数据")

    # 以下逻辑原样保留
    update_global_config(
        user_name=user_name,
        task_type=task_type,
        json_path=use_json_path,
        start_index=start_index,
        end_index=end_index,
        polling_interval=polling_interval,
        max_polling_attempts=max_polling_attempts
    )
    submit_result = await send_group(
        GLOBAL_USER_NAME,
        GLOBAL_TASK_TYPE,
        GLOBAL_JSON_PATH,
        GLOBAL_START_INDEX,
        GLOBAL_END_INDEX
    )
    task_id = submit_result.get("taskID")
    if not task_id:
        print("❌ 任务提交失败，未获取到 taskID")
        return {"success": False, "task_id": None, "final_status": None}

    print(f"✅ 任务提交成功，获得任务ID: {task_id}")
    success = await wait_for_task_completion(task_id)

    final_status = None
    if success and return_final_status:
        print("\n📋 获取最终任务状态...")
        final_status = await get_task_listener_status(task_id)
        if not final_status.get("error"):
            print("📊 最终任务详情:")
            print(f"   任务ID: {final_status.get('taskID')}")
            print(f"   状态: {final_status.get('taskStatus')}")
            print(f"   完成数量: {final_status.get('taskCompletionCount')}")
            print(f"   总任务数: {final_status.get('taskWorkerTotalNumber')}")
            if final_status.get('taskResult'):
                print(f"   任务结果: {final_status.get('taskResult')}")

    return {"success": success, "task_id": task_id, "final_status": final_status}


def main(
    *,
    src_json: Optional[Union[str, Path, List[Dict[str, Any]], Dict[str, Any]]] = None,  # ← 同步入口也放开类型
    dst_json: Optional[str] = None,
    default_user_info: Optional[Dict[str, Any]] = None,
    json_path: Optional[str] = None,
    task_type: str = "xhs_note_gen_group",
    user_name: str = "root",
    start_index: int = 0,
    end_index: Optional[int] = 0,
    polling_interval: int = 30,
    max_polling_attempts: int = 240,
    return_final_status: bool = True,
    return_task_if_loop_running: bool = False,
) -> Union[Dict[str, Any], asyncio.Task]:
    coro = async_main(
        src_json=src_json,
        dst_json=dst_json,
        default_user_info=default_user_info,
        json_path=json_path,
        task_type=task_type,
        user_name=user_name,
        start_index=start_index,
        end_index=end_index,
        polling_interval=polling_interval,
        max_polling_attempts=max_polling_attempts,
        return_final_status=return_final_status,
    )
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    else:
        if return_task_if_loop_running:
            return loop.create_task(coro)
        raise RuntimeError(
            "main() 在已有事件循环中被调用。请改用：\n"
            "  1) await async_main(...)\n"
            "  2) 或 main(..., return_task_if_loop_running=True) 并自行 await 返回的 task。"
        )
