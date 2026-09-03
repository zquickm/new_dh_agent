import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from agents.agent_loop import AgentLoop
from agents.skills import SkillCatalog
from agents.tools.registry import build_tool_executor


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
    def _create_skill(self, root: Path) -> SkillCatalog:
        directory = root / "station-analysis"
        directory.mkdir()
        (directory / "SKILL.md").write_text(
            "---\n"
            "name: station-analysis\n"
            "description: Analyze operational observation stations.\n"
            "---\n\n"
            "Always query the observation service before drawing the map.\n",
            encoding="utf-8",
        )
        return SkillCatalog(root)

    def test_lists_metadata_and_loads_instructions_on_demand(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = self._create_skill(Path(directory))

            summaries = catalog.summaries()
            skill = catalog.load("station-analysis")

            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0].name, "station-analysis")
            self.assertEqual(
                skill.instructions,
                "Always query the observation service before drawing the map.",
            )

    def test_project_transect_database_skill_contains_domain_routing(self):
        project_root = Path(__file__).resolve().parents[1]
        catalog = SkillCatalog(project_root / "skills")

        skill = catalog.load("transect-database-query")

        self.assertIn("E1", skill.description)
        self.assertIn("仅按年月询问各类要素对比", skill.description)
        self.assertIn("database_query", skill.instructions)
        self.assertIn("observation_service", skill.instructions)
        self.assertIn("rag_search", skill.instructions)
        self.assertIn("只统计有效数值", skill.instructions)
        self.assertIn("站位代码以 E2 开头", skill.instructions)
        self.assertIn("按本系统的断面业务默认口径处理", skill.instructions)
        self.assertIn("跨期各类要素对比", skill.instructions)
        self.assertIn("温度数据概况", skill.instructions)
        self.assertIn("水质描述性评价", skill.instructions)
        self.assertIn("不得把有限站位结果外推", skill.instructions)

    def test_load_rejects_names_outside_the_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = self._create_skill(Path(directory))

            with self.assertRaises(LookupError):
                catalog.load("../station-analysis")

    async def test_registry_exposes_and_executes_load_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = self._create_skill(Path(directory))
            executor = build_tool_executor(catalog)

            result = json.loads(
                await executor.execute("load_skill", {"name": "station-analysis"})
            )

            self.assertEqual(result["name"], "station-analysis")
            self.assertIn("observation service", result["instructions"])
            await executor.close()

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
            self.assertNotIn("按名称加载一个可用 Skill", system_prompt)
            self.assertIn(
                "- station-analysis: Analyze operational observation stations.",
                system_prompt,
            )
            self.assertNotIn("Always query the observation service", system_prompt)
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
                                "args": {"name": "station-analysis"},
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
            self.assertIn("observation service", loaded["instructions"])
            await agent.close()
