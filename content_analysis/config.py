# -*- coding: utf-8 -*-
"""
环境变量加载与全局配置（异步服务使用，避免在请求周期内频繁IO）
"""
import os
from dotenv import load_dotenv

# 加载 .env
load_dotenv()

# ===== Dify 工作流（内容评分阶段）=====
DIFY_SCORE_BASE_URL = os.getenv("DIFY_SCORE_BASE_URL", "http://47.113.149.192:8899")
DIFY_SCORE_TOKEN = os.getenv("DIFY_SCORE_TOKEN", "app-r37MXZS4qwpMwTNIJ9KbXtRE")
DIFY_SCORE_PATH = os.getenv("DIFY_SCORE_PATH", "/v1/workflows/run")
DIFY_SCORE_RESPONSE_MODE = os.getenv("DIFY_SCORE_RESPONSE_MODE", "blocking")

# ===== Dify 工作流（内容分析阶段）=====
DIFY_ANALYSIS_BASE_URL = os.getenv("DIFY_ANALYSIS_BASE_URL", "http://47.113.149.192:8899")
DIFY_ANALYSIS_TOKEN = os.getenv("DIFY_ANALYSIS_TOKEN", "app-b4doufhE1ECy7D2ehR2F5de1")
DIFY_ANALYSIS_PATH = os.getenv("DIFY_ANALYSIS_PATH", "/v1/workflows/run")
DIFY_ANALYSIS_RESPONSE_MODE = os.getenv("DIFY_ANALYSIS_RESPONSE_MODE", "blocking")

# ===== 回调地址 =====
CALLBACK_URL = os.getenv(
    "ANALYZE_CALLBACK_URL",
    "http://47.121.125.128:8010/material_library/analayze_result"
)

# 并发/超时配置
MAX_CONCURRENCY = int(os.getenv("ANALYSIS_MAX_CONCURRENCY", "8"))
HTTP_TIMEOUT = float(os.getenv("ANALYSIS_HTTP_TIMEOUT", "60"))
HTTP_RETRIES = int(os.getenv("ANALYSIS_HTTP_RETRIES", "2"))
