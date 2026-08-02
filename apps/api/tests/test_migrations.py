from __future__ import annotations

from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from alembic import command
from flowguard.main import create_app
from flowguard.schema_parity import main as schema_parity_main
from flowguard.schema_parity import schema_differences
from flowguard.storage import Base, FlowGuardRepository

API_DIRECTORY = Path(__file__).parents[1]


def _config(database_url: str) -> Config:
    config = Config(str(API_DIRECTORY / "alembic.ini"))
    config.attributes["database_url"] = database_url
    config.attributes["configure_logger"] = False
    return config


def test_initial_migration_creates_current_schema_without_metadata_drift(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    config = _config(database_url)

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    assert set(Base.metadata.tables).issubset(set(inspect(engine).get_table_names()))
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, Base.metadata) == []

    repository = FlowGuardRepository(engine=engine, create_schema=False)
    repository.upsert_records("user-1", "accounts", [{"account_id": "account-1"}])
    assert repository.current_state_revision("user-1") == "rev-1"

    command.check(config)
    engine.dispose()


def test_existing_current_schema_can_be_stamped_after_parity_check(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'existing.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()

    assert schema_differences(database_url) == []
    assert schema_parity_main(database_url) == 0

    config = _config(database_url)
    command.stamp(config, "head")
    command.check(config)


def test_classification_audit_migration_upgrades_and_downgrades_independently(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'classification-migration.db'}"
    config = _config(database_url)

    command.upgrade(config, "20260802_0001")
    engine = create_engine(database_url)
    assert "ai_classification_runs" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert "ai_classification_runs" in inspector.get_table_names()
    assert {
        "ix_ai_classification_runs_user_id",
        "ix_ai_classification_runs_import_id",
        "ix_ai_classification_runs_request_id",
    }.issubset({item["name"] for item in inspector.get_indexes("ai_classification_runs")})
    assert ("idempotency_key",) in {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("ai_classification_runs")
    }
    engine.dispose()

    command.downgrade(config, "20260802_0001")
    engine = create_engine(database_url)
    assert "ai_classification_runs" not in inspect(engine).get_table_names()
    assert "current_records" in inspect(engine).get_table_names()
    engine.dispose()


def test_investigation_audit_migration_upgrades_and_downgrades_independently(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'investigation-migration.db'}"
    config = _config(database_url)

    command.upgrade(config, "20260802_0002")
    engine = create_engine(database_url)
    assert "ai_investigation_runs" not in inspect(engine).get_table_names()
    assert "ai_investigation_turns" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert {"ai_investigation_runs", "ai_investigation_turns"}.issubset(inspector.get_table_names())
    assert {
        "ix_ai_investigation_runs_analysis_id",
        "ix_ai_investigation_runs_request_id",
        "ix_ai_investigation_runs_snapshot_id",
        "ix_ai_investigation_runs_status",
        "ix_ai_investigation_runs_user_id",
    }.issubset({item["name"] for item in inspector.get_indexes("ai_investigation_runs")})
    assert ("idempotency_key",) in {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("ai_investigation_runs")
    }
    assert ("investigation_id", "turn_sequence") in {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("ai_investigation_turns")
    }
    engine.dispose()

    command.downgrade(config, "20260802_0002")
    engine = create_engine(database_url)
    table_names = inspect(engine).get_table_names()
    assert "ai_investigation_runs" not in table_names
    assert "ai_investigation_turns" not in table_names
    assert "ai_classification_runs" in table_names
    engine.dispose()


def test_schema_parity_rejects_an_unversioned_database_with_drift(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'drifted.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE tool_executions")
    engine.dispose()

    assert schema_differences(database_url)
    assert schema_parity_main(database_url) == 1
    assert "Do not stamp this database" in capsys.readouterr().out


def test_initial_migration_downgrade_removes_application_tables(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'downgrade.db'}"
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, "base")

    engine = create_engine(database_url)
    assert not set(Base.metadata.tables).intersection(inspect(engine).get_table_names())
    engine.dispose()


def test_application_uses_an_alembic_managed_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'application.db'}"
    command.upgrade(_config(database_url), "head")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("FLOWGUARD_AUTO_CREATE_SCHEMA", "false")

    app = create_app()

    assert app.state.repository.list_records("user-1", "accounts") == []
