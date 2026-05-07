from dotenv import load_dotenv
load_dotenv()

from app.database import init_db
init_db()

from .agent import root_agent

__all__ = ["root_agent"]
