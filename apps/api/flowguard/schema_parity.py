"""Verify that an existing database matches the current SQLAlchemy metadata."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from dotenv import load_dotenv
from sqlalchemy import create_engine

from flowguard.storage import DEFAULT_DATABASE_URL, Base


def schema_differences(database_url: str | None = None) -> list[Any]:
    """Return schema differences without requiring an Alembic version stamp."""

    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    url = database_url or os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            return compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()


def main(database_url: str | None = None) -> int:
    differences = schema_differences(database_url)
    if differences:
        print(
            "Schema does not match the current FlowGuard metadata "
            f"({len(differences)} change group(s))."
        )
        print("Do not stamp this database. Back it up and resolve the schema differences first.")
        return 1
    print("Schema matches the current FlowGuard metadata and is safe to baseline-stamp.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
