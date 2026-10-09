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
        history = [
            ChatMessage(role="user", content="查询资料"),
            ChatMessage(role="assistant", content="", tool_calls=[
                {"id": "db", "function": {"name": "database_query", "arguments": "{}"}},
                {"id": "skill", "name": "load_skill", "args": {"name": "空间联动"}},
            ]),
            ChatMessage(role="tool", content="隐藏的数据库结果", tool_call_id="db"),
            ChatMessage(role="tool", content="隐藏的空间能力说明", tool_call_id="skill"),
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
        ordinary = restrict_request("海洋业务", tools, history, None, "提示")
        self.assertIs(ordinary[0], tools)
        self.assertIs(ordinary[1], history)

        restricted_llm = FakeLlm([
            ([], [{"id": "load", "name": "load_skill", "args": {"name": "疑点疑区"}}]),
            (["完成"], []),
        ])
        ordinary_llm = FakeLlm([(["完成"], [])])
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
        self.assertEqual({d["function"]["name"] for d in ordinary_llm.calls[0][1]}, original_names)
        self.assertEqual(set(tools.tools), original_names)
        self.assertNotIn("enum", tools.get("load_skill").parameters["properties"]["name"])
        self.assertEqual(len(history[1].tool_calls), 2)
