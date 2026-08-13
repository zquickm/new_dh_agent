import json
import unittest

from controller.streaming.sse_encoder import encode_sse


class SseEncoderTest(unittest.TestCase):
    def test_encodes_frontend_compatible_unicode_event(self):
        block = encode_sse({"event": "token", "data": {"content": "你好"}})
        self.assertTrue(block.startswith("event: token\n"))
        self.assertTrue(block.endswith("\n\n"))
        payload = json.loads(block.split("data: ", 1)[1])
        self.assertEqual(payload["data"]["content"], "你好")
