import logging

import uvicorn
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from content_analysis.routers.analysis_route import route as analysis_route
from content_generate.routes.generate import router as content_router, service_singleton
from video.script_extraction import route as script_extraction
app = FastAPI()
app.include_router(analysis_route)
app.include_router(content_router)
app.include_router(script_extraction)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有HTTP方法
    allow_headers=["*"]  # 允许所有请求头
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8801)
