import io
import importlib
import unittest

from alembic import command
from alembic.config import Config

from config.settings import PROJECT_DIR, PersistenceSettings
from database.base import Base
from database.models import (
    ConversationMessageModel,
    ConversationModel,
    ConversationRunModel,
)
from database.runtime import build_database_url
from repositories.mysql_conversation_repository import MySQLConversationRepository
from repositories.repository import conversation_repository


class PersistenceSchemaTest(unittest.TestCase):
    def test_runtime_repository_is_always_mysql(self):
        self.assertIsInstance(conversation_repository, MySQLConversationRepository)
        self.assertNotIn("enabled", PersistenceSettings.model_fields)

    def test_models_have_tenant_and_ordering_constraints(self):
        self.assertEqual(ConversationModel.__tablename__, "agent_conversation")
        self.assertEqual(
            ConversationMessageModel.__tablename__, "agent_conversation_message"
        )
        conversation = Base.metadata.tables["agent_conversation"]
        message = Base.metadata.tables["agent_conversation_message"]
        run = Base.metadata.tables["agent_conversation_run"]

        self.assertIn("user_id", conversation.c)
        self.assertIn("map_context", conversation.c)
        self.assertIn("message_count", conversation.c)
        self.assertIn("run_id", message.c)
        self.assertIn("status", message.c)
        self.assertIn("error_code", message.c)
        self.assertIn("updated_at", message.c)
        self.assertIn("sequence_no", message.c)
        self.assertIn("user_id", message.c)
        self.assertTrue(
            any(
                constraint.name == "fk_agent_message_conversation_owner"
                for constraint in message.foreign_key_constraints
            )
        )
        self.assertEqual(ConversationRunModel.__tablename__, "agent_conversation_run")
        self.assertIn("heartbeat_at", run.c)
        self.assertIn("duration_ms", run.c)
        self.assertIn("error_message", run.c)
        self.assertTrue(
            any(
                constraint.name == "fk_agent_run_conversation_owner"
                for constraint in run.foreign_key_constraints
            )
        )

    def test_initial_migration_compiles_as_mysql_sql(self):
        settings = PersistenceSettings(
            host="127.0.0.1",
            port=3306,
            user="agent",
            password="password",
            name="dh_agent",
        )
        output = io.StringIO()
        config = Config(str(PROJECT_DIR / "alembic.ini"), output_buffer=output)
        config.attributes["configure_logger"] = False
        config.attributes["connection_url"] = build_database_url(settings)

        command.upgrade(config, "head", sql=True)

        sql = output.getvalue()
        self.assertIn("CREATE TABLE agent_conversation", sql)
        self.assertIn("CREATE TABLE agent_conversation_message", sql)
        self.assertEqual(sql.count("ALTER TABLE `agent_conversation_message`"), 1)
        self.assertIn("ADD COLUMN `run_id`", sql)
        self.assertIn("ADD INDEX `ix_agent_message_run`", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS `agent_conversation_run`", sql)
        self.assertIn("INSERT INTO `agent_conversation_run`", sql)
        self.assertIn("alembic_version", sql)

    def test_stream_state_migration_resumes_after_partial_mysql_ddl(self):
        migration = importlib.import_module(
            "database.migrations.versions.0002_add_message_stream_state"
        )

        clauses = migration._upgrade_clauses({"run_id"}, set())

        self.assertFalse(any("`run_id` VARCHAR" in clause for clause in clauses))
        self.assertTrue(any("`status`" in clause for clause in clauses))
        self.assertTrue(any("`error_code`" in clause for clause in clauses))
        self.assertTrue(any("`updated_at`" in clause for clause in clauses))
        self.assertTrue(any("`ix_agent_message_run`" in clause for clause in clauses))

    def test_stream_state_migration_is_noop_when_schema_is_complete(self):
        migration = importlib.import_module(
            "database.migrations.versions.0002_add_message_stream_state"
        )

        clauses = migration._upgrade_clauses(
            {"run_id", "status", "error_code", "updated_at"},
            {"ix_agent_message_run"},
        )

        self.assertEqual(clauses, [])

    def test_database_url_escapes_credentials(self):
        url = build_database_url(
            PersistenceSettings(
                user="agent@internal",
                password="p@ss/word",
                name="dh_agent",
            )
        )
        rendered = url.render_as_string(hide_password=False)
        self.assertIn("agent%40internal", rendered)
        self.assertIn("p%40ss%2Fword", rendered)
        self.assertNotIn("p@ss/word", rendered)

    def test_database_name_rejects_sql_identifiers(self):
        with self.assertRaises(ValueError):
            PersistenceSettings(name="dh_agent`; DROP DATABASE dh_auth")
