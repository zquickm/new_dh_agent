import io
import unittest

from alembic import command
from alembic.config import Config

from config.settings import PROJECT_DIR, PersistenceSettings
from database.base import Base
from database.models import ConversationMessageModel, ConversationModel
from database.runtime import build_database_url


class PersistenceSchemaTest(unittest.TestCase):
    def test_models_have_tenant_and_ordering_constraints(self):
        self.assertEqual(ConversationModel.__tablename__, "agent_conversation")
        self.assertEqual(
            ConversationMessageModel.__tablename__, "agent_conversation_message"
        )
        conversation = Base.metadata.tables["agent_conversation"]
        message = Base.metadata.tables["agent_conversation_message"]

        self.assertIn("user_id", conversation.c)
        self.assertIn("map_context", conversation.c)
        self.assertIn("message_count", conversation.c)
        self.assertIn("sequence_no", message.c)
        self.assertIn("user_id", message.c)
        self.assertTrue(
            any(
                constraint.name == "fk_agent_message_conversation_owner"
                for constraint in message.foreign_key_constraints
            )
        )

    def test_initial_migration_compiles_as_mysql_sql(self):
        settings = PersistenceSettings(
            enabled=True,
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
        self.assertIn("alembic_version", sql)

    def test_database_url_escapes_credentials(self):
        url = build_database_url(
            PersistenceSettings(
                enabled=True,
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
