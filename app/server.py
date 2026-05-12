"""
FastAPI server entry point.

Start with:  uvicorn app.server:api --reload --port 8000
"""
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI

from agent_service.email_agent.database import init_db
from .routers.slack import router as slack_router
from .routers.gmail_push import router as gmail_push_router
from .util import router as util_router

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("SQLite action_logs initialised.")
    try:
        from agent_service.email_agent.services.gmail_watch_service import renew_if_needed
        renew_if_needed()
    except Exception as exc:
        logger.warning("Gmail watch registration skipped: %s", exc)

    yield


api = FastAPI(title="Email Agent", version="0.3.0", lifespan=lifespan)
api.include_router(slack_router)
api.include_router(gmail_push_router)
api.include_router(util_router)
