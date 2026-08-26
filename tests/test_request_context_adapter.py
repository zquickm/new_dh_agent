import unittest

from pydantic import ValidationError

from schemas.chat import AgentRequest
from services.request_context_adapter import enrich_question_with_frontend_context


FRONTEND_REQUEST = {
    "sessionId": "1787734568114",
    "message": "这个区域有哪些站点",
    "context": {
        "drawn_geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [121.15697263116806, 29.863214899546577],
                    [122.56658116102153, 29.863214899546577],
                    [122.56658116102153, 30.76776565549946],
                    [121.15697263116806, 30.76776565549946],
                    [121.15697263116806, 29.863214899546577],
                ]
            ],
            "bbox": [
                121.15697263116806,
                29.863214899546577,
                122.56658116102153,
                30.76776565549946,
            ],
        }
    },
    "conversationId": "82edc36b5dd6483483516be532a35021",
}


class RequestContextAdapterTest(unittest.TestCase):
    def test_accepts_real_esic_map_context_request(self):
        request = AgentRequest.model_validate(
            {
                "question": (
                    "[系统上下文] 用户在地图上绘制了区域："
                    "外接矩形 [121.64807, 31.14529, 122.51080, 31.92631]\n"
                    "这个区域有哪些观测站点"
                ),
                "conversation_id": "e5aa026e17c74054a99128b594c6c6fa",
                "map_context": {
                    "drawn_geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [121.64807177848331, 31.145291852528445],
                                [122.51079620689507, 31.145291852528445],
                                [122.51079620689507, 31.926311797831147],
                                [121.64807177848331, 31.926311797831147],
                                [121.64807177848331, 31.145291852528445],
                            ]
                        ],
                        "bbox": [
                            121.64807177848331,
                            31.145291852528445,
                            122.51079620689507,
                            31.926311797831147,
                        ],
                    }
                },
            }
        )

        self.assertIsNotNone(request.context)
        enriched = enrich_question_with_frontend_context(
            request.question,
            request.context,
        )
        self.assertIn('"query_type":"polygon"', enriched)
        self.assertIn('"polygon":[[121.64807177848331', enriched)
        self.assertIn(
            '"bbox":[121.64807177848331,31.145291852528445,',
            enriched,
        )

    def test_accepts_frontend_aliases_and_normalizes_geojson_polygon(self):
        request = AgentRequest.model_validate(FRONTEND_REQUEST)

        self.assertEqual(request.question, "这个区域有哪些站点")
        self.assertEqual(request.session_id, "1787734568114")
        self.assertEqual(
            request.conversation_id,
            "82edc36b5dd6483483516be532a35021",
        )
        enriched = enrich_question_with_frontend_context(
            request.question,
            request.context,
        )
        self.assertIn('"query_type":"polygon"', enriched)
        self.assertIn('"polygon":[[121.15697263116806', enriched)
        self.assertIn('"crs":"EPSG:4490"', enriched)

    def test_old_request_contract_remains_supported(self):
        request = AgentRequest(
            question="普通问题",
            conversation_id="conversation-1",
        )

        self.assertEqual(
            enrich_question_with_frontend_context(
                request.question,
                request.context,
            ),
            "普通问题",
        )

    def test_rejects_unclosed_polygon(self):
        payload = {
            **FRONTEND_REQUEST,
            "context": {
                "drawn_geometry": {
                    **FRONTEND_REQUEST["context"]["drawn_geometry"],
                    "coordinates": [
                        FRONTEND_REQUEST["context"]["drawn_geometry"][
                            "coordinates"
                        ][0][:-1]
                    ],
                }
            },
        }

        with self.assertRaises(ValidationError):
            AgentRequest.model_validate(payload)

    def test_rejects_non_finite_bbox(self):
        payload = {
            **FRONTEND_REQUEST,
            "context": {
                "drawn_geometry": {
                    **FRONTEND_REQUEST["context"]["drawn_geometry"],
                    "bbox": [float("nan"), 29.8, 122.5, 30.7],
                }
            },
        }

        with self.assertRaises(ValidationError):
            AgentRequest.model_validate(payload)
