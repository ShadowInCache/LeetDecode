"""FastAPI application entrypoint.

Run locally:
    uvicorn app.main:app --reload --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import health

settings = get_settings()

app = FastAPI(
    title="LeetDecode API",
    description="Translates LeetCode problem statements into plain English.",
    version="0.1.0",
)

# The Chrome extension popup calls this API from a `chrome-extension://<id>`
# origin, so CORS has to allow it. We don't use cookies or auth headers, so a
# wildcard origin with credentials disabled is safe here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

app.include_router(health.router)
