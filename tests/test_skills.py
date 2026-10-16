import ast
import json
import re
import tempfile
import unittest
from unittest.mock import patch
from copy import deepcopy
from pathlib import Path

from services.synonym_service import build_synonym_matcher
from agents.agent_loop import AgentLoop
from agents.skills import SkillCatalog
from agents.tools.registry import build_tool_executor
from agents.tools.duanmian_analysis import (
    CHEMISTRY_ELEMENTS, HYDROMET_ELEMENTS, execute_duanmian_analysis,
)


class FakeLlm:
    model = "fake-model"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def stream(self, messages, tools=None, temperature=0):
        del temperature
        self.calls.append((deepcopy(messages), tools))
        content, calls = next(self.responses)
        yield content, calls


class SkillCatalogTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        synonym_patch = patch(
            "services.synonym_service._active_synonym_matcher",
            build_synonym_matcher([
                {"standardTerm": "溶解氧", "terms": [{"term": "DO"}]},
            ]),
        )
        synonym_patch.start()
        self.addCleanup(synonym_patch.stop)

    def _create_skill(self, root: Path) -> SkillCatalog:
        directory = root / "观测站分析"
        directory.mkdir()
        (directory / "SKILL.md").write_text(
            "---\n"
            "name: 观测站分析\n"
            "description: Analyze operational observation stations.\n"
            "---\n\n"
            "Always query the spatial query service before drawing the map.\n",
            encoding="utf-8",
        )
        return SkillCatalog(root)

    def test_lists_metadata_and_loads_instructions_on_demand(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = self._create_skill(Path(directory))

            summaries = catalog.summaries()
            skill = catalog.load("观测站分析")

            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0].name, "观测站分析")
            self.assertEqual(
                skill.instructions,
                "Always query the spatial query service before drawing the map.",
            )

    def test_project_transect_business_skill_contains_domain_routing(self):
        project_root = Path(__file__).resolve().parents[1]
        catalog = SkillCatalog(project_root / "skills")

        skill = catalog.load("断面业务")

        self.assertIn("E1", skill.description)
        self.assertIn("仅按年月询问各类要素对比", skill.description)
        self.assertIn("database_query", skill.instructions)
        self.assertIn("spatial_query_service", skill.instructions)
        for operation in ("dm_duanmian_latest", "dm_duanmian_observations", "dm_duanmian_voyages"):
            self.assertIn(operation, skill.instructions)
        self.assertIn("scope=all、code=null", skill.instructions)
        self.assertIn("每次只返回一页", skill.instructions)
        self.assertIn("dm_duanmian_analysis", skill.instructions)
        self.assertIn("## 路由边界", skill.instructions)
        self.assertIn(
            "调查断面及其站位、辅助点的位置、基础属性和分布，使用 `spatial_query_service` 的通用查询",
            skill.instructions,
        )
        self.assertIn("历史观测、跨期统计和描述性评价使用 `database_query`", skill.instructions)
        self.assertNotIn("不调用 `spatial_query_service` 的 `dm_duanmian` 详情或查询操作", skill.instructions)
        self.assertIn("地图选区中的业务观测站，以及明确站名的状态、详情、最新观测、趋势或时间区间数据，使用 `spatial_query_service`。", skill.instructions)
        self.assertIn("不猜字段名或放宽为全库查询", skill.instructions)
        self.assertNotIn("所有断面数据查询和分析都必须使用数据库工具", skill.instructions)
        self.assertNotIn("observation_service", skill.instructions)
        self.assertIn("仅要求专题出图或打开分析浮层时使用 `dm_duanmian_analysis`", skill.instructions)
        self.assertIn("向用户综合展示全面的信息", skill.instructions)
        self.assertIn("## 断面地图展示", skill.instructions)
        self.assertIn("默认同步在地图展示 E1、E2、E3 三条断面", skill.instructions)
        self.assertIn("统计与专题图仍仅针对 E1", skill.instructions)
        self.assertIn("浮层不能代替地图展示", skill.instructions)
        self.assertIn("同图层首次 `replace=true`，后续 `replace=false`", skill.instructions)
        self.assertNotIn("无需额外查询数据库或调用 `map_action`", skill.instructions)
        spatial = catalog.load("空间联动")
        self.assertIn("按返回的 `dataKey` 逐类调用", spatial.instructions)
        self.assertIn("再显式调用 `map_action`", spatial.instructions)
        self.assertIn("区域总览由本 Skill 独立完成", spatial.instructions)
        self.assertNotIn("断面业务", spatial.instructions)
        for obsolete in ("search_stations", "list_station_types", "station_detail", "station_types"):
            self.assertNotIn(obsolete, spatial.instructions)
        self.assertNotIn("空间联动", skill.instructions)
        self.assertNotIn("不查询 `dm_duanmian`", spatial.instructions)
        self.assertIn("E1、E2、E3", skill.instructions)
        self.assertIn("共布设3条断面（E1、E2、E3）", skill.instructions)
        self.assertIn("EK、EL断面不纳入此范围", skill.instructions)
        self.assertIn("按三条断面的全部站位处理，不追问断面编号", skill.instructions)
        self.assertIn("东海 E1、E2、E3 三条断面", skill.description)
        self.assertNotIn("5条", skill.instructions)
        sql_guide = (project_root / "database_information" / "数据库_AI_SQL查询指南.md").read_text(encoding="utf-8")
        self.assertIn("东海断面为3条（E1、E2、E3）", sql_guide)
        self.assertIn("EL、EK 记录仅在用户明确指定时查询", sql_guide)
        self.assertIn("默认只查询三条断面", sql_guide)
        self.assertNotIn("五条断面", sql_guide)
        self.assertIn("E1001", skill.instructions)
        self.assertIn("E1-001", skill.instructions)
        self.assertIn("91 个纯数字历史编码", skill.instructions)
        self.assertIn("海水化学观测指标", skill.instructions)
        self.assertIn("水文气象观测内容", skill.instructions)
        self.assertIn("有效波周期", skill.instructions)
        self.assertNotIn("总碱度等", skill.instructions)
        self.assertNotIn("透明度等", skill.instructions)
        self.assertIn("完整、明确的自然语言问题", skill.instructions)
        self.assertIn("truncated=true", skill.instructions)
        self.assertIn("当前数据只有观测日期", skill.instructions)
        self.assertNotIn("station_name", skill.instructions)
        self.assertNotIn("dwd_as_", skill.instructions)

    def test_transect_chart_rules_and_examples_match_tool_contract(self):
        skill = SkillCatalog(Path(__file__).resolve().parents[1] / "skills").load("断面业务")
        self.assertIn("必须调用 `dm_duanmian_analysis`，无需用户额外说“画图”", skill.instructions)
        self.assertIn("用户明确只要文字、只要表格或不要图时不出图", skill.instructions)
        self.assertIn("数据库失败、空结果、缺少坐标均不阻止", skill.instructions)
        self.assertIn("最新且该要素有有效观测的实际航次", skill.instructions)
        self.assertNotIn("随机选一个", skill.instructions)
        examples = re.findall(r"`(dm_duanmian_analysis\([^`\n]+\))`", skill.instructions)
        self.assertTrue(examples)
        element_options = {"chemistry": CHEMISTRY_ELEMENTS, "hydromet": HYDROMET_ELEMENTS}
        element_types = set()
        for example in examples:
            with self.subTest(example=example):
                call = ast.parse(example, mode="eval").body
                arguments = {item.arg: ast.literal_eval(item.value) for item in call.keywords}
                payload = execute_duanmian_analysis(**arguments).client_events[0]["payload"]
                for name in ("analysis", "section", "voyage", "element"):
                    self.assertEqual(payload[name], arguments[name])
                self.assertIn(payload["element"], element_options[payload["elementType"]])
                element_types.add(payload["elementType"])
        self.assertEqual(element_types, {"chemistry", "hydromet"})

    def test_load_rejects_names_outside_the_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = self._create_skill(Path(directory))

            with self.assertRaises(LookupError):
                catalog.load("../观测站分析")

    async def test_registry_exposes_and_executes_load_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = self._create_skill(Path(directory))
            executor = build_tool_executor(catalog)

            result = json.loads(
                await executor.execute("load_skill", {"name": "观测站分析"})
            )

            self.assertEqual(result["name"], "观测站分析")
            self.assertIn("spatial query service", result["instructions"])
            await executor.close()

    async def test_project_business_skill_loads_through_registered_tool(self):
        catalog = SkillCatalog(Path(__file__).resolve().parents[1] / "skills")
        executor = build_tool_executor(catalog)
        self.addAsyncCleanup(executor.close)
        result = json.loads(await executor.execute("load_skill", {"name": "断面业务"}))
        self.assertEqual(result["name"], "断面业务")
        self.assertEqual(result["instructions"], catalog.load("断面业务").instructions)
        self.assertIn("断面业务", {skill.name for skill in catalog.summaries()})

    async def test_agent_context_contains_skill_index_without_tool_duplication(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = self._create_skill(Path(directory))
            executor = build_tool_executor(catalog)
            llm = FakeLlm([("done", [])])
            agent = AgentLoop(llm, executor, catalog)

            _ = [event async for event in agent.run_stream("analyze", [])]

            system_prompt = llm.calls[0][0][0]["content"]
            tool_names = {
                item["function"]["name"] for item in llm.calls[0][1]
            }
            self.assertIn("load_skill", tool_names)
            self.assertNotIn("当前可用工具：", system_prompt)
            self.assertNotIn("按名称加载一个可用能力", system_prompt)
            self.assertIn(
                "- 观测站分析: Analyze operational observation stations.",
                system_prompt,
            )
            self.assertNotIn("Always query the spatial query service", system_prompt)
            await agent.close()

    async def test_loaded_skill_is_added_to_the_next_model_round(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = self._create_skill(Path(directory))
            executor = build_tool_executor(catalog)
            llm = FakeLlm(
                [
                    (
                        "",
                        [
                            {
                                "id": "call-skill",
                                "name": "load_skill",
                                "args": {"name": "观测站分析"},
                            }
                        ],
                    ),
                    ("done", []),
                ]
            )
            agent = AgentLoop(llm, executor, catalog)

            _ = [event async for event in agent.run_stream("analyze", [])]

            loaded = json.loads(llm.calls[1][0][-1]["content"])
            self.assertEqual(llm.calls[1][0][-1]["role"], "tool")
            self.assertEqual(llm.calls[1][0][-1]["name"], "load_skill")
            self.assertIn("spatial query service", loaded["instructions"])
            await agent.close()
