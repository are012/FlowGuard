from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from alembic import context
from flowguard.storage import DEFAULT_DATABASE_URL, Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
target_metadata = Base.metadata


def database_url() -> str:
    attribute_url = config.attributes.get("database_url")
    if isinstance(attribute_url, str) and attribute_url:
        return attribute_url
    configured_url = config.get_main_option("sqlalchemy.url")
    return os.getenv("DATABASE_URL") or configured_url or DEFAULT_DATABASE_URL


def run_migrations_offline() -> None:
    url = database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
