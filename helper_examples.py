# import os
#
# from dotenv import load_dotenv
#
# from mongodb_helper import MongoDBHelper
# from coze_helper import CozeHelper
# from interface.mongodb import MongoDBHelper as synchronous_MongoDBHelper
#
#
# load_dotenv()
# mongodb_helper = MongoDBHelper("member_system")
# coze_helper = CozeHelper(os.getenv("COZE_API_KEY"))
# synchronous_mongodb_helper = synchronous_MongoDBHelper()
import asyncio
import os

import httpx
from dotenv import load_dotenv
from volcenginesdkarkruntime import Ark

# 你自己的封装
from mongodb_helper import MongoDBHelper
# from interface.mongodb import MongoDBHelper as SynchronousMongoDBHelper

load_dotenv()

# --- 1) 先创建(但不连接) ---
# 假设异步的 MongoDBHelper 里可以不在 __init__ 里立即连数据库，而只是存一下 db_name 等参数
mongodb_helper = MongoDBHelper(
    db_name="member_system",
    uri=None,  # 暂时不传URI或者传个None占位
    max_pool_size=1000
)
# synchronous_mongodb_helper = SynchronousMongoDBHelper()


# --- 2) 在这里封装 init & close 方法 ---

async def init_all():
    """
    在 FastAPI startup 时调用：
    - 让 mongodb_helper 连接到真正的 MongoDB
    - 同步版本也做连接
    """
    # 这里你可以从 .env 或其他地方加载 URI
    async_uri = os.getenv("ASYNC_MONGO_URI", "mongodb://localhost:27017")
    # 调用你 MongoDBHelper 内部的逻辑（如果你的 __init__ 已经连了，那可以省略这步）
    mongodb_helper._client = None  # 如果需要先重置一下
    mongodb_helper.__init__(db_name="member_system", uri=async_uri, max_pool_size=5000)

    # 同步版本
    # sync_uri = os.getenv("SYNC_MONGO_URI", "mongodb://localhost:27017")
    # synchronous_mongodb_helper.__init__("member_system")

    # 如果 coze_helper 或其他 helper 需要做初始化，也可以在这进行
    print("All helpers initialized.")


# 初始化火山引擎
client = Ark(
    timeout=httpx.Timeout(timeout=1800),
    base_url="https://ark.cn-beijing.volces.com/api/v3",
    api_key=os.environ.get("DouBao_API_KEY")
)
# 并发控制
semaphore = asyncio.Semaphore(10)

MODEL_MAPPING = {
    "Deepseek-V3": "ep-20250213103842-dnh76",
    "Deepseek-V3.1": "ep-20250822181551-lvk76",
    "Deepseek-R1": "ep-20250213103815-8kjbw",
    "Doubao-Pro": "ep-20250206103948-6z5n6",
    "Doubao-VL": "ep-20250602004606-zr8hb",
    "Doubao-Thinking": "ep-20250613001745-rwbd7",
    "Doubao-Text-Video": "ep-20250613001355-dfnkb",
    "Doubao-Image-Video": "ep-20250613001027-flkgg",
    "Doubao-Text-Image": "ep-20250613001248-xjqs4",
    "Doubao-1.6": "ep-20250626114140-ssfn5",
    "Doubao-Flash": "ep-20250714150550-8d27t",
}

async def close_all():
    """
    在 FastAPI shutdown 时调用：
    - 关闭异步 MongoDB
    - 关闭同步 MongoDB
    - 等其他需要清理的资源
    """
    await mongodb_helper.close()
    # synchronous_mongodb_helper.close()
    print("All helpers closed.")
