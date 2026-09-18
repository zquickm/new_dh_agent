import importlib
import runpy
import unittest
from unittest.mock import MagicMock, patch

import sqlalchemy as sa
from alembic import context
from alembic.config import Config
from alembic.runtime.migration import MigrationContext

from config.settings import PROJECT_DIR


class PromptVersionMigrationTest(unittest.TestCase):
    def test_fresh_partial_and_completed_schema(self):
        migration = importlib.import_module("database.migrations.versions.0009_add_prompt_version_to_run")
        names = {"system_prompt_version_id", "system_prompt_version_no", "system_prompt_sha256"}
        for present, complete in ((set(), False), ({"system_prompt_version_id"}, False), (names, True)):
            with self.subTest(present=present):
                inspector = MagicMock()
                inspector.get_columns.return_value = [{"name": name} for name in present]
                inspector.get_indexes.return_value = ([{"name": "ix_agent_run_system_prompt_version"}] if complete else [])
                inspector.get_foreign_keys.return_value = ([{"name": "fk_agent_run_system_prompt_version"}] if complete else [])
                with patch.object(migration.context, "is_offline_mode", return_value=False), \
                     patch.object(migration.op, "get_bind"), \
                     patch.object(migration.sa, "inspect", return_value=inspector), \
                     patch.object(migration.op, "execute") as execute:
                    migration.upgrade()
                if complete:
                    execute.assert_not_called()
                else:
                    execute.assert_called_once()
                    sql = str(execute.call_args.args[0])
                    for name in names:
                        self.assertEqual(f"ADD COLUMN `{name}`" in sql, name not in present)
                    self.assertIn("ADD CONSTRAINT", sql)
                    self.assertIn("ADD INDEX", sql)


class RunEventReplayMigrationTest(unittest.TestCase):
    def test_partial_schema_only_creates_missing_parts(self):
        migration = importlib.import_module(
            "database.migrations.versions.0014_add_run_event_replay"
        )
        inspector = MagicMock()
        inspector.get_table_names.return_value = []
        inspector.get_columns.side_effect = (
            [{"name": "last_event_sequence"}],
            [],
        )
        with patch.object(migration.context, "is_offline_mode", return_value=False), \
             patch.object(migration.op, "get_bind"), \
             patch.object(migration.sa, "inspect", return_value=inspector), \
             patch.object(migration.op, "execute") as execute:
            migration.upgrade()

        sql = "\n".join(str(call.args[0]) for call in execute.call_args_list)
        self.assertNotIn("ADD COLUMN `last_event_sequence`", sql)
        self.assertIn("ADD COLUMN `stream_event_sequence`", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS `agent_run_event`", sql)

    def test_complete_schema_is_noop(self):
        migration = importlib.import_module(
            "database.migrations.versions.0014_add_run_event_replay"
        )
        inspector = MagicMock()
        inspector.get_table_names.return_value = ["agent_run_event"]
        inspector.get_columns.side_effect = (
            [{"name": "last_event_sequence"}],
            [{"name": "stream_event_sequence"}],
        )
        with patch.object(migration.context, "is_offline_mode", return_value=False), \
             patch.object(migration.op, "get_bind"), \
             patch.object(migration.sa, "inspect", return_value=inspector), \
             patch.object(migration.op, "execute") as execute:
            migration.upgrade()
        execute.assert_not_called()


class RunCancellationMigrationTest(unittest.TestCase):
    def test_partial_and_complete_schema(self):
        migration = importlib.import_module(
            "database.migrations.versions.0016_add_run_cancellation"
        )
        names = {"cancel_requested_at", "cancelled_at", "cancel_reason"}
        for present in (set(), {"cancel_requested_at"}, names):
            with self.subTest(present=present):
                inspector = MagicMock()
                inspector.get_columns.return_value = [
                    {"name": name} for name in present
                ]
                with patch.object(migration.context, "is_offline_mode", return_value=False), \
                     patch.object(migration.op, "get_bind"), \
                     patch.object(migration.sa, "inspect", return_value=inspector), \
                     patch.object(migration.op, "execute") as execute:
                    migration.upgrade()
                if present == names:
                    execute.assert_not_called()
                else:
                    execute.assert_called_once()
                    sql = str(execute.call_args.args[0])
                    for name in names:
                        self.assertEqual(
                            f"ADD COLUMN `{name}`" in sql,
                            name not in present,
                        )


class MigrationTransactionTest(unittest.IsolatedAsyncioTestCase):
    async def test_lock_autobegin_does_not_rollback_revision_on_disconnect(self):
        config = Config(str(PROJECT_DIR / "alembic.ini"))
        config.attributes.update(configure_logger=False, connection_url="unused")
        # Load env without executing its deployment entrypoint.
        with patch.object(context, "config", config, create=True), \
             patch.object(context, "is_offline_mode", return_value=False), \
             patch("asyncio.run", side_effect=lambda coro: coro.close()):
            env = runpy.run_path(str(PROJECT_DIR / "database/migrations/env.py"))
        run = env["run_migrations_online"]
        engine = sa.create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            connection.execute(sa.text("INSERT INTO alembic_version VALUES ('0008')"))
        lock_events = []

        class Connection:
            async def __aenter__(self):
                self.sync = engine.connect()
                return self

            async def __aexit__(self, *args):
                self.sync.close()

            async def execute(self, statement, params):
                lock_events.append(str(statement))
                # A read, like GET_LOCK, starts a real SQLAlchemy transaction.
                return self.sync.execute(sa.text("SELECT 1"))

            async def commit(self):
                self.sync.commit()

            async def run_sync(self, callback):
                self.assert_clean_transaction()
                return callback(self.sync)

            def assert_clean_transaction(self):
                assert not self.sync.in_transaction(), "GET_LOCK transaction leaked into Alembic"

        class Engine:
            def connect(self):
                return Connection()

            async def dispose(self):
                pass

        def migrate(connection):
            migration = MigrationContext.configure(connection, opts={
                "transaction_per_migration": True, "transactional_ddl": False,
            })
            with migration.begin_transaction(_per_migration=True):
                connection.execute(sa.text("UPDATE alembic_version SET version_num='0009'"))

        run.__globals__["create_async_engine"] = lambda *args, **kwargs: Engine()
        run.__globals__["do_run_migrations"] = migrate
        try:
            await run()
            with engine.connect() as connection:
                self.assertEqual(connection.scalar(sa.text("SELECT version_num FROM alembic_version")), "0009")
            self.assertIn("GET_LOCK", lock_events[0])
            self.assertIn("RELEASE_LOCK", lock_events[-1])
        finally:
            engine.dispose()
