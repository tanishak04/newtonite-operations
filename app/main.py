from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api import auth, work_items

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Newtonite Operations Under Pressure — Real-time Operational Work Items Platform",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

app.include_router(auth.router, prefix=settings.API_V1_PREFIX)
app.include_router(work_items.router, prefix=settings.API_V1_PREFIX)

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def root():
    index_file = os.path.join(static_dir, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"status": "ok", "app": settings.PROJECT_NAME}

@app.get("/health")
async def health_check():
    return {"status": "ok", "app": settings.PROJECT_NAME}
