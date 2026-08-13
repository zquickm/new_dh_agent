import os
import unittest
from unittest.mock import patch

from config.settings import clear_settings_cache


class LlmClientTest(unittest.TestCase):
    def tearDown(self):
        clear_settings_cache()

    def test_uses_placeholder_when_upstream_does_not_require_api_key(self):
        with patch.dict(
            os.environ,
            {"NEW_DH_LLM__API_KEY": ""},
            clear=False,
        ):
            clear_settings_cache()
            with patch("agents.llm_client.ChatOpenAI") as chat_openai:
                from agents.llm_client import HelloAgentsLLM

                HelloAgentsLLM()

            self.assertEqual(chat_openai.call_args.kwargs["api_key"], "not-required")

    def test_passes_configured_api_key(self):
        with patch.dict(
            os.environ,
            {"NEW_DH_LLM__API_KEY": "configured-key"},
            clear=False,
        ):
            clear_settings_cache()
            with patch("agents.llm_client.ChatOpenAI") as chat_openai:
                from agents.llm_client import HelloAgentsLLM

                HelloAgentsLLM()

            self.assertEqual(chat_openai.call_args.kwargs["api_key"], "configured-key")
