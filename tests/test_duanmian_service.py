import json
import unittest
from copy import deepcopy

from agents.agent_loop import AgentLoop
from agents.tools.duanmian_service import DuanmianServiceTool, TOOL_DEFINITIONS
from agents.tools.executor import ToolExecutor, ToolOutput
from controller.streaming.sse_encoder import encode_chat_stream
from schemas.chat import AgentRequest
from services.chat_service import ChatService
from tool_backends.spatial_backend import SpatialServiceError


ROW = {
    "dataType": "chemistry",
    "stationCode": "E1001",
    "observedAt": "2025-11-01",
    "depthM": "0",
    "elements": {"temperature_c": 20.5, "ph_value": None},
}
FIELDS = [
    {"column": "temperature_c", "label": "温度 T（℃）"},
    {"column": "salinity_psu", "label": "盐度 S（PSU）"},
    {"column": "dissolved_oxygen_umol_dm3", "label": "溶解氧（μmol/dm³）"},
]


class FakeClient:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.rows = [deepcopy(ROW)]
        self.total = 1
        self.chemistry_fields = deepcopy(FIELDS)
        self.voyage_rows = [
            {"date": "2025-08-01", "chemistryStations": 6, "hydrometStations": 0},
            {"date": "2025-11-01", "chemistryStations": 6, "hydrometStations": 6},
        ]

    def scoped(self, code, **data):
        return {
            "code": code,
            "scope": "section" if len(code) == 2 else "station",
            **data,
        }

    async def get_duanmian_overview(self):
        self.calls.append(("overview",))
        return {
            "overview": {"sectionCount": 3, "stationCount": 26},
            "sections": [{"section": "E1"}],
        }

    async def get_duanmian_latest(self, code):
        self.calls.append(("latest", code))
        return self.scoped(
            code,
            rows=deepcopy(self.rows),
            chemistryFields=self.chemistry_fields,
            hydrometFields=[{"column": "wind_speed_ms", "label": "风速（m/s）"}],
        )

    async def get_duanmian_voyages(self, code):
        self.calls.append(("voyages", code))
        return self.scoped(code, voyages=deepcopy(self.voyage_rows))

    async def get_duanmian_observations(self, **arguments):
        self.calls.append(("observations", arguments))
        return self.scoped(
            arguments["code"],
            rows=deepcopy(self.rows),
            total=self.total,
            page=arguments["page"],
            size=arguments["size"],
            **{
                "from": arguments["start_date"],
                "to": arguments["end_date"],
                "chemistryFields": self.chemistry_fields,
            },
        )

    async def close(self):
        self.closed = True


class DuanmianServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FakeClient()
        self.tool = DuanmianServiceTool(self.client)

    async def asyncTearDown(self):
        await self.tool.close()

    async def test_overview_scope_and_latest_dates_depths_nulls_are_preserved(self):
        overview = await self.tool.overview()
        self.assertEqual(overview["scope_sections"], ["E1", "E2", "E3"])
        self.client.rows.append(
            {
                **deepcopy(ROW),
                "dataType": "hydromet",
                "observedAt": "2025-08-01",
                "depthM": "＞1000",
                "elements": {"wind_speed_ms": None},
            }
        )
        latest = await self.tool.latest(code=" ek001 ")
        self.assertIn(("latest", "EK001"), self.client.calls)
        self.assertEqual(latest["rows"], self.client.rows)
        self.assertEqual(latest["chemistryFields"], FIELDS)
        self.assertEqual(latest["snapshot_policy"], "per_station_latest")

    async def test_code_case_is_normalized_but_historical_hyphen_is_not_discarded(self):
        await self.tool.latest(code=" e1-001 ")
        self.assertEqual(self.client.calls, [("latest", "E1-001")])

    async def test_observation_pagination_preserves_filters_without_automatic_fetches(
        self,
    ):
        self.client.total = 50
        result = await self.tool.observations(
            code="e1",
            **{"from": "2025-11-01", "to": "2025-11-30", "page": 2, "size": 20},
        )
        self.assertEqual(
            self.client.calls,
            [
                (
                    "observations",
                    {
                        "code": "E1",
                        "start_date": "2025-11-01",
                        "end_date": "2025-11-30",
                        "page": 2,
                        "size": 20,
                    },
                )
            ],
        )
        self.assertEqual((result["total"], result["returned"]), (50, 1))
        self.assertTrue(result["partial"])
        self.assertTrue(result["has_more"])
        self.assertEqual(result["chemistryFields"], FIELDS)

    async def test_three_calendar_month_boundary_and_invalid_arguments(self):
        self.client.rows = []
        self.client.total = 0
        await self.tool.observations(
            code="E1", **{"from": "2024-11-30", "to": "2025-02-28"}
        )
        count = len(self.client.calls)
        invalid = [
            {"from": "2024-11-30", "to": "2025-03-01"},
            {"from": "2025-02-29", "to": "2025-03-01"},
            {"from": "2025-11-02", "to": "2025-11-01"},
            {"from": "2025-1-01", "to": "2025-02-01"},
            {"from": "2025-11-01", "to": "2025-11-30", "size": 1001},
            {"from": "2025-11-01", "to": "2025-11-30", "page": True},
            {"from": "2025-11-01"},
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                await self.tool.observations(code="E1", **arguments)
        self.assertEqual(len(self.client.calls), count)

    async def test_empty_results_remain_empty_without_fake_map_coordinates(self):
        self.client.rows = []
        self.client.total = 0
        latest = await self.tool.latest(code="E1")
        observations = await self.tool.observations(
            code="E1", **{"from": "2025-11-01", "to": "2025-11-30"}
        )
        self.assertEqual(latest["rows"], [])
        self.assertFalse(observations["has_more"])
        self.assertFalse(observations["partial"])
        self.assertNotIn("map_actions", latest)

    async def test_voyages_are_sorted_by_actual_date_and_keep_coverage(self):
        result = await self.tool.voyages(code="el")
        self.assertEqual(
            [v["date"] for v in result["voyages"]], ["2025-11-01", "2025-08-01"]
        )
        self.assertEqual(result["voyages"][1]["hydrometStations"], 0)

    async def test_bad_upstream_results_are_errors_not_no_data(self):
        self.client.rows = [{**ROW, "observedAt": "yesterday"}]
        with self.assertRaises(SpatialServiceError):
            await self.tool.latest(code="E1")
        self.client.rows = [deepcopy(ROW)]
        self.client.total = -1
        with self.assertRaises(SpatialServiceError):
            await self.tool.observations(
                code="E1", **{"from": "2025-11-01", "to": "2025-11-30"}
            )
        self.client.voyage_rows[0]["chemistryStations"] = True
        with self.assertRaises(SpatialServiceError):
            await self.tool.voyages(code="E1")

    async def test_open_only_never_calls_virtual_http_or_selects_a_voyage(self):
        for analysis in (
            "depth-curve",
            "temp-section",
            "ts-scatter",
            "nutrient",
            "do-profile",
            "surface",
        ):
            output = await self.tool.analysis(analysis=analysis)
            self.assertFalse(output.content["auto_query_requested"])
            self.assertNotIn("voyage", output.client_events[0]["payload"])
            self.assertEqual(
                output.client_events[0]["action_type"], "DUANMIAN_ANALYSIS"
            )
        self.assertEqual(self.client.calls, [])

    async def test_all_six_charts_can_request_auto_query_using_real_voyages(self):
        for analysis in (
            "depth-curve",
            "temp-section",
            "ts-scatter",
            "nutrient",
            "do-profile",
            "surface",
        ):
            with self.subTest(analysis=analysis):
                arguments = {"analysis": analysis, "voyage": "2025-11"}
                if analysis != "ts-scatter":
                    arguments["section"] = "e1"
                if analysis in {"depth-curve", "temp-section", "surface"}:
                    arguments["element"] = "温度"
                output = await self.tool.analysis(**arguments)
                payload = output.client_events[0]["payload"]
                self.assertEqual(payload["voyage"], "2025-11")
                self.assertEqual(payload["elementType"], "chemistry")
                self.assertTrue(output.content["auto_query_requested"])
                if "element" in arguments:
                    self.assertEqual(payload["element"], "temperature_c")
        self.assertIn(("voyages", "E3"), self.client.calls)

    async def test_exact_date_hydromet_and_unit_label_are_preserved(self):
        output = await self.tool.analysis(
            analysis="surface",
            section="E1",
            voyage="2025-11-01",
            elementType="hydromet",
            element="风速（m/s）",
        )
        self.assertEqual(
            output.client_events[0]["payload"],
            {
                "analysis": "surface",
                "section": "E1",
                "voyage": "2025-11-01",
                "elementType": "hydromet",
                "element": "wind_speed_ms",
            },
        )

    async def test_invalid_chart_combinations_do_not_query_or_emit_an_action(self):
        arguments_list = [
            {"analysis": "unknown"},
            {"analysis": "temp-section", "section": "EK"},
            {"analysis": "ts-scatter", "section": "E1"},
            {"analysis": "temp-section", "elementType": "hydromet"},
            {"analysis": "temp-section", "voyage": "2025-11", "element": "温度"},
            {"analysis": "temp-section", "voyage": "2025-11", "section": "E1"},
            {
                "analysis": "temp-section",
                "voyage": "latest",
                "section": "E1",
                "element": "温度",
            },
            {"analysis": "do-profile", "element": "盐度"},
        ]
        for arguments in arguments_list:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                await self.tool.analysis(**arguments)
        self.assertEqual(self.client.calls, [])

    async def test_missing_or_ambiguous_voyage_and_unknown_field_cannot_trigger_chart(
        self,
    ):
        with self.assertRaisesRegex(ValueError, "没有"):
            await self.tool.analysis(
                analysis="surface", section="E1", voyage="2025-07", element="温度"
            )
        with self.assertRaisesRegex(ValueError, "没有"):
            await self.tool.analysis(
                analysis="surface",
                section="E1",
                voyage="2025-08",
                elementType="hydromet",
                element="风速",
            )
        with self.assertRaisesRegex(ValueError, "唯一确认"):
            await self.tool.analysis(
                analysis="surface", section="E1", voyage="2025-11", element="made_up"
            )
        self.client.voyage_rows.append(
            {"date": "2025-11-20", "chemistryStations": 6, "hydrometStations": 6}
        )
        with self.assertRaisesRegex(ValueError, "多个观测日"):
            await self.tool.analysis(
                analysis="surface", section="E1", voyage="2025-11", element="温度"
            )
        output = await self.tool.analysis(
            analysis="surface", section="E1", voyage="2025-11-01", element="温度"
        )
        self.assertEqual(output.content["parameters"]["voyage"], "2025-11-01")

    async def test_ts_scatter_does_not_silently_mix_dates_or_incomplete_sections(self):
        original = self.client.get_duanmian_voyages

        async def different_date(code):
            data = await original(code)
            if code == "E3":
                data["voyages"][1]["date"] = "2025-11-02"
            return data

        self.client.get_duanmian_voyages = different_date
        with self.assertRaisesRegex(ValueError, "观测日不同"):
            await self.tool.analysis(analysis="ts-scatter", voyage="2025-11")
        with self.assertRaisesRegex(ValueError, "没有"):
            await self.tool.analysis(analysis="ts-scatter", voyage="2025-11-01")

    async def test_voyage_then_analysis_flows_through_existing_sse_after_map_query(
        self,
    ):
        class FakeLlm:
            model = "fake-model"
            calls = 0

            async def stream(inner, messages, tools=None, temperature=0):
                inner.calls += 1
                if inner.calls == 1:
                    yield (
                        "",
                        [
                            {
                                "id": "voyages",
                                "name": "dm_duanmian_voyages",
                                "args": {"code": "E1"},
                            }
                        ],
                    )
                elif inner.calls == 2:
                    self.assertIn("2025-11-01", messages[-1]["content"])
                    yield (
                        "",
                        [
                            {"id": "map", "name": "spatial_query", "args": {}},
                            {
                                "id": "analysis",
                                "name": "dm_duanmian_analysis",
                                "args": {
                                    "analysis": "temp-section",
                                    "section": "E1",
                                    "voyage": "2025-11-01",
                                    "element": "温度",
                                },
                            },
                        ],
                    )
                else:
                    self.assertIn("auto_query_requested", messages[-1]["content"])
                    yield "已发送 E1 水温剖面的展示指令。", []

        executor = ToolExecutor()
        for name, (description, parameters, method) in TOOL_DEFINITIONS.items():
            executor.register(name, description, getattr(self.tool, method), parameters)
        executor.register(
            "spatial_query",
            "空间查询",
            lambda: ToolOutput(
                content={"total": 0},
                client_events=(
                    {
                        "action_type": "CLEAR",
                        "payload": {"layer_ids": ["spatial-dm_duanmian"]},
                    },
                ),
            ),
        )
        executor.register_closer(self.tool.close)
        service = ChatService()
        service._agent = AgentLoop(FakeLlm(), executor)
        service._agent.max_rounds = 5
        try:
            frames = [
                f
                async for f in encode_chat_stream(
                    service.stream_chat(
                        AgentRequest(question="打开 E1 最新航次水温剖面"),
                        user_id="duanmian-test",
                    )
                )
            ]
        finally:
            await service.close()
        actions = [
            json.loads(f.split("\ndata: ", 1)[1])["data"]
            for f in frames
            if f.startswith("event: map_action\n")
        ]
        self.assertEqual(
            [a["action_type"] for a in actions], ["CLEAR", "DUANMIAN_ANALYSIS"]
        )
        self.assertEqual(actions[1]["action_id"], "analysis-1")
        self.assertEqual(actions[1]["batch_id"], "analysis")
        self.assertEqual(actions[1]["seq"], 1)
        self.assertEqual(actions[0]["run_id"], actions[1]["run_id"])
        self.assertEqual(actions[1]["version"], "1.0")
        self.assertEqual(actions[1]["payload"]["element"], "temperature_c")
        self.assertNotIn("element_type", actions[1]["payload"])
        self.assertTrue(frames[-1].startswith("event: done\n"))
        self.assertTrue(self.client.closed)

    async def test_analysis_upstream_failure_is_reported_without_an_action(self):
        async def fail(code):
            raise SpatialServiceError("空间服务返回 HTTP 503")

        self.client.get_duanmian_voyages = fail

        class FakeLlm:
            model = "fake-model"
            calls = 0

            async def stream(inner, messages, tools=None, temperature=0):
                inner.calls += 1
                if inner.calls == 1:
                    yield (
                        "",
                        [
                            {
                                "id": "analysis",
                                "name": "dm_duanmian_analysis",
                                "args": {
                                    "analysis": "do-profile",
                                    "section": "E1",
                                    "voyage": "2025-11",
                                },
                            }
                        ],
                    )
                else:
                    self.assertIn("HTTP 503", messages[-1]["content"])
                    yield "服务暂不可用，未发送出图动作。", []

        executor = ToolExecutor()
        executor.register("dm_duanmian_analysis", "断面分析", self.tool.analysis)
        agent = AgentLoop(FakeLlm(), executor)
        events = [e async for e in agent.run_stream("打开剖面", [])]
        self.assertFalse(any(e.type == "map_action" for e in events))
        self.assertFalse(next(e for e in events if e.type == "tool_finished").success)
