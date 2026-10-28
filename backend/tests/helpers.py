from pathlib import Path

from alembic.config import Config

BACKEND_DIR = Path(__file__).resolve().parent.parent


def alembic_config(database_url: str) -> Config:
    """Alembic config pointed at `database_url`.

    No config file name: avoids alembic's fileConfig() hijacking pytest logging.
    """
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    return config
