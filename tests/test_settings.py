import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config.settings import (
    ApplicationDotEnvSettingsSource,
    Settings,
    clear_settings_cache,
    get_active_profile,
    get_settings,
)


class SettingsTest(unittest.TestCase):
    def tearDown(self):
        clear_settings_cache()

    def test_profile_yaml_and_nested_environment_override_are_merged(self):
        with patch.dict(
            os.environ,
            {
                "APP_PROFILE": "dev",
                "NEW_DH_LLM__API_KEY": "test-key",
                "NEW_DH_REACT__MAX_ROUNDS": "7",
                "NEW_DH_LOGGING__LEVEL": "DEBUG",
            },
            clear=False,
        ):
            clear_settings_cache()
            settings = get_settings()
            self.assertEqual(settings.app_profile, "dev")
            self.assertEqual(settings.server.host, "127.0.0.1")
            self.assertTrue(settings.server.reload)
            self.assertEqual(settings.react.max_rounds, 7)
            self.assertEqual(settings.logging.level, "DEBUG")
            self.assertEqual(settings.llm.model, "dsv4")
            self.assertEqual(settings.internal_request.header_name, "X-DH-Internal-Token")
            self.assertTrue(settings.internal_request.token.get_secret_value())
            self.assertTrue(hasattr(settings, "sensitive_words"))
            sensitive_words = settings.sensitive_words
            self.assertTrue(sensitive_words.enabled)
            self.assertEqual(
                settings.dictionaries.resolved_cache_directory(),
                Path(__file__).resolve().parent.parent
                / "data"
                / "dictionaries",
            )
            self.assertTrue(settings.dictionaries.refresh.enabled)
            self.assertEqual(settings.dictionaries.refresh.interval_seconds, 300)
            self.assertEqual(settings.dictionaries.refresh.jitter_seconds, 30)

    def test_system_environment_profile_has_priority(self):
        with patch.dict(os.environ, {"APP_PROFILE": "prod"}, clear=False):
            self.assertEqual(get_active_profile(), "prod")

    def test_mysql_persistence_configuration_applies_in_dev_profile(self):
        with patch.dict(
            os.environ,
            {
                "APP_PROFILE": "dev",
                "DH_AGENT_DB_HOST": "dev-mysql",
                "DH_AGENT_DB_PORT": "3307",
                "DH_AGENT_DB_NAME": "dev_agent",
                "DH_AGENT_DB_USERNAME": "dev-user",
                "DH_AGENT_DB_PASSWORD": "dev-password",
            },
            clear=False,
        ):
            clear_settings_cache()
            settings = get_settings()

            self.assertEqual(settings.persistence.host, "dev-mysql")
            self.assertEqual(settings.persistence.port, 3307)
            self.assertEqual(settings.persistence.name, "dev_agent")
            self.assertEqual(settings.persistence.user, "dev-user")
            self.assertEqual(
                settings.persistence.password.get_secret_value(), "dev-password"
            )

    def test_prod_profile_uses_same_host_service_ports(self):
        with patch.dict(
            os.environ,
            {
                "APP_PROFILE": "prod",
                "DH_INTERNAL_TOKEN": "test-internal-token",
                "DH_BUSINESS_DB_USERNAME": "test-reader",
                "BUSINESS_DB_PASSWORD": "test-password",
                "DH_AGENT_DB_USERNAME": "test-agent",
                "DH_AGENT_DB_PASSWORD": "test-agent-password",
            },
            clear=False,
        ):
            clear_settings_cache()
            settings = get_settings()

            self.assertEqual(settings.server.host, "0.0.0.0")
            self.assertEqual(settings.server.port, 8000)
            self.assertEqual(
                settings.dictionaries.base_url,
                "http://127.0.0.1:8082",
            )
            self.assertEqual(settings.persistence.name, "dh_agent")

    def test_prod_profile_addresses_and_accounts_can_be_overridden(self):
        with patch.dict(
            os.environ,
            {
                "APP_PROFILE": "prod",
                "DH_INTERNAL_TOKEN": "changed-internal-token",
                "DH_AGENT_BIND_HOST": "10.0.0.10",
                "DH_AGENT_PORT": "8100",
                "DH_KNOWLEDGE_HOST": "10.0.0.11",
                "DH_KNOWLEDGE_PORT": "8182",
                "DH_LLM_HOST": "10.0.0.12",
                "DH_LLM_PORT": "8190",
                "DH_BUSINESS_DB_HOST": "10.0.0.13",
                "DH_BUSINESS_DB_PORT": "9130",
                "DH_BUSINESS_DB_USERNAME": "changed-reader",
                "BUSINESS_DB_PASSWORD": "changed-password",
                "DH_AGENT_DB_HOST": "10.0.0.14",
                "DH_AGENT_DB_PORT": "3307",
                "DH_AGENT_DB_NAME": "changed_agent",
                "DH_AGENT_DB_USERNAME": "changed-agent-user",
                "DH_AGENT_DB_PASSWORD": "changed-agent-password",
            },
            clear=False,
        ):
            clear_settings_cache()
            settings = get_settings()

            self.assertEqual(settings.server.host, "10.0.0.10")
            self.assertEqual(settings.server.port, 8100)
            self.assertEqual(settings.dictionaries.base_url, "http://10.0.0.11:8182")
            self.assertEqual(settings.llm.base_url, "http://10.0.0.12:8190/v1")
            self.assertEqual(settings.tools.database.host, "10.0.0.13")
            self.assertEqual(settings.tools.database.port, 9130)
            self.assertEqual(settings.tools.database.user, "changed-reader")
            self.assertEqual(
                settings.tools.database.password.get_secret_value(),
                "changed-password",
            )
            self.assertEqual(settings.persistence.host, "10.0.0.14")
            self.assertEqual(settings.persistence.port, 3307)
            self.assertEqual(settings.persistence.name, "changed_agent")
            self.assertEqual(settings.persistence.user, "changed-agent-user")
            self.assertEqual(
                settings.persistence.password.get_secret_value(),
                "changed-agent-password",
            )

    def test_llm_api_key_is_optional(self):
        with patch.dict(
            os.environ,
            {
                "APP_PROFILE": "dev",
                "NEW_DH_LLM__API_KEY": "",
            },
            clear=False,
        ):
            clear_settings_cache()
            settings = get_settings()
            self.assertEqual(settings.llm.api_key.get_secret_value(), "")

    def test_yaml_reads_sensitive_tool_values_from_environment(self):
        with patch.dict(
            os.environ,
            {
                "APP_PROFILE": "dev",
                "RAG_API_TOKEN": "rag-secret",
                "BUSINESS_DB_PASSWORD": "database-secret",
                "DEEPSEEK_API_KEY": "sql-llm-secret",
                "SPATIAL_API_KEY": "spatial-secret",
            },
            clear=False,
        ):
            clear_settings_cache()
            settings = get_settings()

            self.assertEqual(
                settings.tools.rag.api_token.get_secret_value(),
                "rag-secret",
            )
            self.assertEqual(
                settings.tools.database.password.get_secret_value(),
                "database-secret",
            )
            self.assertEqual(
                settings.tools.database.sql_llm.api_key.get_secret_value(),
                "sql-llm-secret",
            )
            self.assertEqual(
                settings.tools.spatial.api_key.get_secret_value(),
                "spatial-secret",
            )
            self.assertEqual(
                settings.tools.spatial.base_url,
                "http://71.10.60.143:6022",
            )
            self.assertEqual(settings.tools.rag.timeout, 30)
            self.assertTrue(settings.tools.database.host)
            self.assertEqual(settings.tools.database.name, "donghai_dwd")

    def test_tool_backend_variables_are_not_app_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "APP_PROFILE=dev\n"
                "NEW_DH_LLM__API_KEY=test-key\n"
                "RAG_API_TOKEN=rag-token\n"
                "BUSINESS_DB_PASSWORD=db-password\n"
                "DH_AGENT_DB_HOST=mysql\n"
                "DH_AGENT_DB_PASSWORD=agent-password\n"
                "SPATIAL_API_KEY=spatial-token\n",
                encoding="utf-8",
            )
            source = ApplicationDotEnvSettingsSource(
                Settings,
                env_file=env_file,
                env_file_encoding="utf-8",
            )

            values = source()

            self.assertEqual(values["APP_PROFILE"], "dev")
            self.assertEqual(values["llm"]["api_key"], "test-key")
            self.assertNotIn("rag_api_token", values)
            self.assertNotIn("business_db_password", values)
            self.assertNotIn("dh_agent_db_host", values)
            self.assertNotIn("dh_agent_db_password", values)
            self.assertNotIn("spatial_api_key", values)
