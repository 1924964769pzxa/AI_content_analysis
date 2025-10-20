import os
from typing import Optional

# ===== 外部接口配置 =====

# 素材库相关
MAT_BASE = os.getenv("MAT_BASE", "http://47.121.125.128:8010")
MAT_KEYWORDS_GET = f"{MAT_BASE}/material_library/keywords/{{tag}}"
MAT_SEARCH_BY_TAG = f"{MAT_BASE}/material_library/search/tags"
MAT_CREATE_SEARCH_TASK = f"{MAT_BASE}/material_library/create_search_task"

# 关键词补全
KEYWORD_FILL_URL = os.getenv("KEYWORD_FILL_URL", "http://47.113.149.192:8801/keywords_generate")

# Dify 工作流统一入口
DIFY_RUN_URL = os.getenv("DIFY_RUN_URL", "http://47.113.149.192:8899/v1/workflows/run")

# Dify Token（按你的要求）
DIFY_TOKEN_TYPE_DETECT = os.getenv("DIFY_TOKEN_TYPE_DETECT", "app-F2lUcds0PI2N8PVdIl3mqUCK")
DIFY_TOKEN_SINGLE_WRITE = os.getenv("DIFY_TOKEN_SINGLE_WRITE", "app-HuDlTCF6JF28FXNiZNQyWOLp")
DIFY_TOKEN_COMBO_WRITE = os.getenv("DIFY_TOKEN_COMBO_WRITE", "app-iSMKBMkFZAHaA90LglrwKZNC")
DIFY_TOKEN_SINGLE_IMAGE = os.getenv("DIFY_TOKEN_SINGLE_IMAGE", "app-79NkADlqjBrNnADZG3uMEMW5")
DIFY_TOKEN_KEYWORD_SELECT = os.getenv("DIFY_TOKEN_KEYWORD_SELECT", "app-NXejPAZZsWxMzvVXn7BQeT3r")

# ===== 超时与并发控制 =====

# 关键词轮询（“等待回调后再检索”——这里用最多等待秒数+间隔做兜底；若配置回调可提前结束）
KEYWORD_POLL_MAX_SECONDS = int(os.getenv("KEYWORD_POLL_MAX_SECONDS", "120"))
KEYWORD_POLL_INTERVAL_SECONDS = float(os.getenv("KEYWORD_POLL_INTERVAL_SECONDS", "3"))

# HTTP 超时（秒）
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))

# 最大并发请求（避免压垮对端）
MAX_HTTP_CONCURRENCY = int(os.getenv("MAX_HTTP_CONCURRENCY", "8"))

# ===== 其他可选配置 =====

# 当素材仅有 1 篇时，是否允许以 single 链路继续（True：继续，False：直接结束）
ALLOW_SINGLE_WHEN_ONLY_ONE_MATERIAL = True

# 日志文件（如果需要文件日志，可打开）
LOG_FILE: Optional[str] = os.getenv("LOG_FILE", None)
