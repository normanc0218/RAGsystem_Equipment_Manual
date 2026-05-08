from dotenv import load_dotenv
load_dotenv()

from .database import init_db
init_db()

from .email_agent import root_agent

__all__ = ["root_agent"]
