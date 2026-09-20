import math
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

    def test_accepts_circle_and_builds_circle_query_context(self):
        request = AgentRequest.model_validate(
            {
                **FRONTEND_REQUEST,
                "context": {
                    "drawn_geometry": {
                        "type": "Circle",
                        "center": [121.8, 30.4],
                        "radius": 25000,
                    }
                },
            }
        )

        enriched = enrich_question_with_frontend_context(
            request.question,
            request.context,
        )

        self.assertIn('"query_type":"circle"', enriched)
        self.assertIn('"center":[121.8,30.4]', enriched)
        self.assertIn('"radius":25000.0', enriched)
        self.assertIn('"crs":"EPSG:4490"', enriched)

    def test_accepts_documented_circle_contract_variants(self):
        geometries = [
            {
                "action_type": "CIRCLE_DRAW",
                "payload": {"center": [121.8, 30.4], "radius": 25000},
            },
            {
                "actionType": "CIRCLE_DRAW",
                "payload": {"center": [121.8, 30.4], "radius": 25000},
            },
            {
                "query_type": "circle",
                "center": [121.8, 30.4],
                "radius": 25000,
            },
            {
                "type": "circle",
                "center": [121.8, 30.4],
                "radius": 25000,
            },
            {
                "type": "CIRCLE_DRAW",
                "center": [121.8, 30.4],
                "radius": 25000,
            },
            {
                "center": [121.8, 30.4],
                "radius": 25000,
            },
        ]

        for geometry in geometries:
            with self.subTest(geometry=geometry):
                request = AgentRequest.model_validate(
                    {
                        **FRONTEND_REQUEST,
                        "context": {"drawn_geometry": geometry},
                    }
                )
                enriched = enrich_question_with_frontend_context(
                    request.question,
                    request.context,
                )
                self.assertIn('"query_type":"circle"', enriched)
                self.assertIn('"center":[121.8,30.4]', enriched)
                self.assertIn('"radius":25000.0', enriched)

    def test_accepts_camel_case_map_context_and_drawn_geometry(self):
        request = AgentRequest.model_validate(
            {
                "message": "这个圆形区域有哪些站点",
                "mapContext": {
                    "drawnGeometry": {
                        "center": [121.8, 30.4],
                        "radius": 25000,
                    }
                },
            }
        )

        enriched = enrich_question_with_frontend_context(
            request.question,
            request.context,
        )

        self.assertIn('"query_type":"circle"', enriched)
        self.assertIn('"center":[121.8,30.4]', enriched)
        self.assertIn('"radius":25000.0', enriched)

    def test_rejects_invalid_circle_geometry(self):
        invalid_geometries = [
            {"type": "Circle", "center": [121.8], "radius": 25000},
            {"type": "Circle", "center": [181, 30.4], "radius": 25000},
            {"type": "Circle", "center": [121.8, 30.4], "radius": 0},
            {"type": "Circle", "center": [121.8, 30.4], "radius": float("inf")},
        ]

        for geometry in invalid_geometries:
            with self.subTest(geometry=geometry), self.assertRaises(ValidationError):
                AgentRequest.model_validate(
                    {
                        **FRONTEND_REQUEST,
                        "context": {"drawn_geometry": geometry},
                    }
                )

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

    def test_selected_area_is_default_scope_for_implicit_station_request(self):
        payload = {
            **FRONTEND_REQUEST,
            "message": "输出观测站点",
        }
        request = AgentRequest.model_validate(payload)

        enriched = enrich_question_with_frontend_context(
            request.question,
            request.context,
        )

        self.assertTrue(enriched.startswith("输出观测站点\n\n"))
        self.assertIn("默认限定在 selected_area", enriched)
        self.assertIn("selected_area 是本轮唯一有效选区", enriched)
        self.assertIn("会话当前有效选区", enriched)
        self.assertIn('"query_type":"polygon"', enriched)

    def test_named_station_takes_priority_over_selected_area_display(self):
        payload = {
            **FRONTEND_REQUEST,
            "message": "小洋山站水温多少",
        }
        request = AgentRequest.model_validate(payload)

        enriched = enrich_question_with_frontend_context(
            request.question,
            request.context,
        )

        self.assertIn("明确单个对象时以该对象为准", enriched)
        self.assertIn("无关问题不套用范围", enriched)
        self.assertNotIn("search_stations", enriched)
        self.assertNotIn("database_query", enriched)

    def test_single_station_follow_up_does_not_reuse_selected_area(self):
        request = AgentRequest.model_validate(
            {
                **FRONTEND_REQUEST,
                "message": "该站点的经纬度是多少",
            }
        )

        enriched = enrich_question_with_frontend_context(
            request.question,
            request.context,
        )

        self.assertNotIn("selected_area", enriched)
        self.assertIn("最近一轮明确讨论的单个站点", enriched)

    def test_group_follow_up_keeps_area_even_when_it_contains_qi(self):
        request = AgentRequest.model_validate(FRONTEND_REQUEST)
        for question in ("具体有哪些雷达站", "其中雷达站的位置有哪些", "其中有哪些站点在线，状态如何"):
            with self.subTest(question=question):
                enriched = enrich_question_with_frontend_context(question, request.context)
                self.assertIn('"query_type":"polygon"', enriched)
                self.assertNotIn("仅指最近一轮明确讨论的单个站点", enriched)

    def test_normalizes_unclosed_polygon_and_recomputes_bbox(self):
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
                    "bbox": [0, 0, 0, 0],
                }
            },
        }

        request = AgentRequest.model_validate(payload)
        geometry = request.context.drawn_geometry

        self.assertEqual(geometry.coordinates[0][0], geometry.coordinates[0][-1])
        self.assertEqual(
            geometry.bbox,
            FRONTEND_REQUEST["context"]["drawn_geometry"]["bbox"],
        )

    def test_recomputes_non_finite_frontend_bbox_from_valid_polygon(self):
        payload = {
            **FRONTEND_REQUEST,
            "context": {
                "drawn_geometry": {
                    **FRONTEND_REQUEST["context"]["drawn_geometry"],
                    "bbox": [float("nan"), 29.8, 122.5, 30.7],
                }
            },
        }

        request = AgentRequest.model_validate(payload)

        self.assertEqual(
            request.context.drawn_geometry.bbox,
            FRONTEND_REQUEST["context"]["drawn_geometry"]["bbox"],
        )

    def test_restores_polygonized_circle_from_radius_metadata(self):
        center_lon = 121.45220037635829
        center_lat = 31.184881234368106
        ring = [
            [
                center_lon + 0.85 * math.cos(2 * math.pi * index / 64),
                center_lat + 0.75 * math.sin(2 * math.pi * index / 64),
            ]
            for index in range(65)
        ]
        request = AgentRequest.model_validate(
            {
                "sessionId": "1787889840431",
                "message": "这个区域有哪些站点",
                "context": {
                    "drawn_geometry": {
                        "type": "Polygon",
                        "coordinates": [ring],
                        "bbox": [
                            center_lon,
                            center_lat - 0.35,
                            center_lon + 0.77,
                            center_lat,
                        ],
                        "radius": 82991.07555434913,
                    }
                },
            }
        )

        geometry = request.context.drawn_geometry
        self.assertEqual(geometry.type, "Circle")
        self.assertAlmostEqual(geometry.center[0], center_lon)
        self.assertAlmostEqual(geometry.center[1], center_lat)
        self.assertEqual(geometry.radius, 82991.07555434913)
        enriched = enrich_question_with_frontend_context(
            request.question,
            request.context,
        )
        self.assertIn('"query_type":"circle"', enriched)
        self.assertIn('"center":[121.45220037635829,31.184881234368106]', enriched)
        self.assertIn('"radius":82991.07555434913', enriched)
        self.assertNotIn('"polygon"', enriched)

    def test_rejects_non_finite_polygon_coordinate(self):
        payload = {
            **FRONTEND_REQUEST,
            "context": {
                "drawn_geometry": {
                    **FRONTEND_REQUEST["context"]["drawn_geometry"],
                    "coordinates": [[
                        [float("nan"), 29.8],
                        [122.5, 29.8],
                        [122.5, 30.7],
                        [float("nan"), 29.8],
                    ]],
                }
            },
        }

        with self.assertRaises(ValidationError):
            AgentRequest.model_validate(payload)

    def test_discards_transient_history_from_previous_map_actions(self):
        payload = {
            **FRONTEND_REQUEST,
            "chat_history": [
                {"role": "user", "content": "小洋山站水温多少"},
                {"role": "assistant", "content": "小洋山站当前水温为 25℃"},
                {"role": "assistant", "content": ""},
                {"role": "tool", "content": "查询完成"},
                {"event": "map_action", "content": "FLY_TO"},
                {
                    "event": "CIRCLE_DRAW",
                    "payload": {"center": [121.8, 30.4], "radius": 25000},
                },
                {
                    "type": "circle",
                    "center": [121.8, 30.4],
                    "radius": 25000,
                },
            ],
        }

        request = AgentRequest.model_validate(payload)

        self.assertEqual(len(request.chat_history), 2)
        self.assertEqual(
            [message.role for message in request.chat_history],
            ["user", "assistant"],
        )
