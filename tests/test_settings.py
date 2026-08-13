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
            },
            clear=False,
        ):
            clear_settings_cache()
            settings = get_settings()
            self.assertEqual(settings.app_profile, "dev")
            self.assertEqual(settings.server.host, "127.0.0.1")
            self.assertTrue(settings.server.reload)
            self.assertEqual(settings.react.max_rounds, 7)
            self.assertEqual(settings.llm.model, "dsv4")

    def test_system_environment_profile_has_priority(self):
        with patch.dict(os.environ, {"APP_PROFILE": "prod"}, clear=False):
            self.assertEqual(get_active_profile(), "prod")

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

    def test_tool_backend_variables_are_not_app_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "APP_PROFILE=dev\n"
                "NEW_DH_LLM__API_KEY=test-key\n"
                "RAG_API_TOKEN=rag-token\n"
                "BUSINESS_DB_PASSWORD=db-password\n",
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
