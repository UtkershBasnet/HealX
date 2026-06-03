"""
HealX — FastAPI Application Entry Point.

Configures the app with:
- Lifespan handler (DB init on startup)
- Structured logging via structlog
- Webhook router
- Job status API endpoints
- Health check
"""

import uuid
from contextlib import asynccontextmanager

import logging

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.jobs import router as jobs_router
from app.api.stats import router as stats_router
from app.api.timeline import router as timeline_router
from app.config import settings
from app.models.db import Base, engine
from app.models.schemas import HealthResponse
from app.observability.langfuse_client import flush_langfuse
from app.webhook.router import router as webhook_router

# ─── Structured Logging ───

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(settings.log_level.upper())
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


# ─── Lifespan ───


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — runs on startup and shutdown."""
    # Startup
    logger.info(
        "healx_starting",
        environment=settings.app_env,
        database=settings.database_url.split("@")[-1] if "@" in settings.database_url else "***",
    )

    # Create tables (in dev only — use Alembic in production)
    if not settings.is_production:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("database_tables_created")

    yield

    # Shutdown
    logger.info("healx_shutting_down")
    flush_langfuse()
    await engine.dispose()


# ─── App ───

app = FastAPI(
    title="HealX",
    description="Autonomous Self-Healing CI/CD Infrastructure with Multi-Agent AI Verification",
    version="0.1.0",
    lifespan=lifespan,
)

# ─── CORS ───

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Routers ───

app.include_router(webhook_router)
app.include_router(jobs_router)
app.include_router(stats_router)
app.include_router(timeline_router)

# ─── Dashboard (server-rendered HTML + polling) ───

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/dashboard", include_in_schema=False)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/dashboard/jobs/{job_id}", include_in_schema=False)
async def dashboard_job(request: Request, job_id: uuid.UUID):
    return templates.TemplateResponse(
        "job_detail.html",
        {"request": request, "job_id": str(job_id)},
    )


# ─── Health Check ───


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health_check():
    """Health check endpoint."""
    return HealthResponse(
        status="ok",
        version="0.1.0",
        environment=settings.app_env,
    )


