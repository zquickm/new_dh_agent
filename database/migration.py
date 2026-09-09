from alembic import command
from alembic.config import Config

from config.settings import PROJECT_DIR, get_settings
from database.runtime import build_database_url


def upgrade_to_head() -> None:
    config = Config(str(PROJECT_DIR / "alembic.ini"))
    config.attributes["configure_logger"] = False
    config.attributes["connection_url"] = build_database_url(
        get_settings().persistence
    )
    command.upgrade(config, "head")
