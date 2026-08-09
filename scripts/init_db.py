import logging
import sqlite3
from pathlib import Path

from jo_pipeline.config import REPO_ROOT, load_config
from jo_pipeline.logging_setup import configure_logging

LOGGER = logging.getLogger(__name__)
SCHEMA_DIR = REPO_ROOT / "schema"


def apply_schema(database_path: Path):
    if database_path.exists():
        raise FileExistsError(f"database already exists at {database_path}, remove it before reinitialising")

    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        for script in sorted(SCHEMA_DIR.glob("*.sql")):
            LOGGER.info(f"applying schema script {script.name}")
            connection.executescript(script.read_text())
        connection.commit()
    finally:
        connection.close()
    LOGGER.info(f"schema applied to {database_path}")


def main():
    config = load_config()
    configure_logging(config.log_level)
    apply_schema(config.database_path)


if __name__ == "__main__":
    main()
