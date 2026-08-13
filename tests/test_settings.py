import os
import unittest
from unittest.mock import patch

from config.settings import clear_settings_cache, get_active_profile, get_settings


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
