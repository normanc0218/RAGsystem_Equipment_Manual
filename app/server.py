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
from .util import router as util_router

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("SQLite action_logs initialised.")
    yield


api = FastAPI(title="Email Agent", version="0.3.0", lifespan=lifespan)
api.include_router(slack_router)
api.include_router(util_router)
