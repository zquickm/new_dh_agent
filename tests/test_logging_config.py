import logging
import tempfile
import unittest

from common.logging_config import configure_logging
from config.settings import LoggingSettings, get_settings


class LoggingConfigTest(unittest.TestCase):
    def test_writes_utf8_log_file(self):
        with tempfile.TemporaryDirectory() as directory:
            try:
                log_path = configure_logging(
                    LoggingSettings(directory=directory, retention_days=2)
                )
                logging.getLogger("tests.logging").info(
                    'chat_started question="查询小洋山站水温"'
                )
                for handler in logging.getLogger().handlers:
                    handler.flush()

                content = log_path.read_text(encoding="utf-8")
                self.assertIn("查询小洋山站水温", content)
            finally:
                configure_logging(get_settings().logging)
