import asyncio
import unittest
from unittest.mock import patch

from agents.agent_loop import AgentLoop
from agents.temporary_suspicion_scope import SKILLS, TOOLS, restrict_request
from agents.tools.registry import build_skill_catalog, build_tool_executor
from schemas.chat import ChatMessage
from tests.test_agent_loop import FakeLlm


class TemporarySuspicionScopeTest(unittest.IsolatedAsyncioTestCase):
    async def test_scope_is_request_local_and_blocks_hidden_capabilities(self):
        catalog = build_skill_catalog()
        tools = build_tool_executor(catalog)
        self.addAsyncCleanup(tools.close)
        original_names = set(tools.tools)
        ordinary_names = original_names - (TOOLS - {"load_skill", "rag_search"})
        ordinary_skills = frozenset(skill.name for skill in catalog.summaries()) - SKILLS
        history = [
            ChatMessage(role="user", content="查询资料"),
            ChatMessage(role="assistant", content="", tool_calls=[
                {"id": "db", "function": {"name": "database_query", "arguments": "{}"}},
                {"id": "skill", "name": "load_skill", "args": {"name": "空间联动"}},
                {"id": "xiamen", "name": "query_suspicion_results", "args": {}},
                {"id": "xiamen-skill", "function": {"name": "load_skill", "arguments": '{"name":"疑点疑区"}'}},
            ]),
            ChatMessage(role="tool", content="隐藏的数据库结果", tool_call_id="db"),
            ChatMessage(role="tool", content="隐藏的空间能力说明", tool_call_id="skill"),
            ChatMessage(role="tool", content="厦门研判结果", tool_call_id="xiamen"),
            ChatMessage(role="tool", content="厦门能力说明", tool_call_id="xiamen-skill"),
            ChatMessage(role="assistant", content="已查询"),
        ]
        for question in ("厦门业务", "分析疑点疑区", "查询图斑"):
            with self.subTest(question=question):
                scoped, filtered, skills, _ = restrict_request(question, tools, history, None, "提示")
                self.assertEqual(set(scoped.tools), TOOLS)
                self.assertEqual(skills, SKILLS)
                with self.assertRaises(LookupError):
                    await scoped.execute("database_query", {})
                with self.assertRaises(PermissionError):
                    await scoped.execute("load_skill", {"name": "空间联动"}, enabled_skill_names=skills)
                self.assertEqual(filtered[1].tool_calls[0]["id"], "skill")
        self.assertEqual(restrict_request("厦门", tools, [], frozenset(), "提示")[2], frozenset())
        ordinary = restrict_request(
            "海洋业务", tools, history, None, "提示",
            available_skill_names=ordinary_skills | SKILLS,
        )
        self.assertEqual(set(ordinary[0].tools), ordinary_names)
        self.assertEqual(ordinary[2], ordinary_skills)
        for name in TOOLS - {"load_skill", "rag_search"}:
            with self.assertRaises(LookupError):
                await ordinary[0].execute(name, {})
        with self.assertRaises(PermissionError):
            await ordinary[0].execute("load_skill", {"name": "疑点疑区"}, enabled_skill_names=ordinary[2])
        self.assertEqual(restrict_request("海洋业务", tools, [], SKILLS, "提示")[2], frozenset())

        restricted_llm = FakeLlm([
            ([], [{"id": "load", "name": "load_skill", "args": {"name": "疑点疑区"}}]),
            (["完成"], []),
        ])
        ordinary_llm = FakeLlm([
            ([], [{"id": "load", "name": "load_skill", "args": {"name": "空间联动"}}]),
            (["完成"], []),
        ])
        agents = (AgentLoop(restricted_llm, tools, catalog), AgentLoop(ordinary_llm, tools, catalog))

        async def run(agent, question):
            return [event async for event in agent.run_stream(question, history)]

        with patch("services.context_service.expand_query_synonyms", side_effect=lambda text: text):
            await asyncio.gather(run(agents[0], "厦门业务"), run(agents[1], "海洋业务"))
        for messages, definitions in restricted_llm.calls:
            self.assertEqual({d["function"]["name"] for d in definitions}, TOOLS)
            index = messages[0]["content"].split("当前可用能力：", 1)[1].split("Skill 加载权限", 1)[0]
            self.assertIn("- 疑点疑区:", index)
            self.assertNotIn("空间联动", index)
            self.assertNotIn("断面业务", index)
            self.assertFalse(any(m.get("tool_call_id") in {"db", "skill"} for m in messages))
            loader = next(d for d in definitions if d["function"]["name"] == "load_skill")
            self.assertEqual(loader["function"]["parameters"]["properties"]["name"]["enum"], ["疑点疑区"])
        for messages, definitions in ordinary_llm.calls:
            self.assertEqual({d["function"]["name"] for d in definitions}, ordinary_names)
            self.assertNotIn("疑点疑区", messages[0]["content"])
            self.assertIn("- 空间联动:", messages[0]["content"])
            self.assertIn("- 断面业务:", messages[0]["content"])
            self.assertFalse(any(m.get("tool_call_id") in {"xiamen", "xiamen-skill"} for m in messages))
            self.assertFalse(any(
                call["name"] in TOOLS - {"load_skill", "rag_search"}
                or (call["name"] == "load_skill" and call["args"].get("name") in SKILLS)
                for m in messages for call in m.get("tool_calls") or []
            ))
            loader = next(d for d in definitions if d["function"]["name"] == "load_skill")
            self.assertNotIn("enum", loader["function"]["parameters"]["properties"]["name"])
        self.assertEqual(set(tools.tools), original_names)
        self.assertNotIn("enum", tools.get("load_skill").parameters["properties"]["name"])
        self.assertEqual(len(history[1].tool_calls), 4)
