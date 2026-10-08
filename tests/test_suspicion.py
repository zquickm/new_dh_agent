import asyncio
import json
import unittest
from unittest.mock import patch

from agents.agent_loop import AgentLoop
from agents.tools.context import ToolContext
from agents.tools.executor import ToolExecutor
from agents.tools.suspicion import RULES, SuspicionTools
from schemas.chat import AgentRequest
from services.chat_service import ChatService
from tests.fakes.memory_conversation_repository import MemoryConversationRepository
from tests.test_agent_loop import FakeLlm
from tool_backends.spatial_backend import SpatialServiceError


class Backend:
    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []
        self.before = None
        self.saved = {}
        self.reads = []
        self.missing_coastline = False

    async def inventory(self, batch):
        return batch or "XM-2026-06", {f"P{i:03}" for i in range(100)}

    def rows(self, path, keys=None):
        rule = next(r for r, spec in RULES.items() if spec[0] == path)
        keys = sorted(keys) if keys is not None else [f"P{i:03}" for i in range(100)]
        return [{"patchNo": k, "resultType": RULES[rule][2][0] if int(k[1:]) < 35 else "CLEAR",
                 "evidence": "来自后端的实际依据", "overlapRatio": 0.1} for k in keys]

    async def validate(self, path, payload):
        if self.before:
            self.before(path)
        self.calls.append((path, payload))
        await asyncio.sleep(0)
        if path == self.fail:
            raise SpatialServiceError("规则失败")
        rows = self.rows(path, payload.get("patchNos"))
        if payload["save"]:
            stored = self.saved.setdefault(path, {})
            stored.update({row["patchNo"]: row for row in rows})
        return {"batchNo": payload["batchNo"], "saved": payload["save"], "verdicts": rows}

    async def results(self, path, batch):
        if path == self.fail:
            raise SpatialServiceError("读取失败")
        self.reads.append((path, batch))
        rows = list(self.saved.get(path, {}).values())
        return rows[1:] if path == "coastline" and self.missing_coastline else rows

    async def close(self):
        pass


class SuspicionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = Backend()
        self.tools = SuspicionTools(self.backend)
        self.context = ToolContext()
        self.addAsyncCleanup(self.tools.close)

    async def test_serial_progress_and_summary_only(self):
        events = []
        async def emit(data):
            events.append(data)
        context = ToolContext(on_progress=emit)
        def before(path):
            if path == "sea-rights":
                self.assertEqual(events, [])
                return
            previous = events[-1]
            paths = [spec[0] for spec in RULES.values()]
            self.assertEqual(previous["step_id"], list(RULES)[paths.index(path) - 1])
            self.assertEqual(previous["status"], "success")
        self.backend.before = before
        output = await self.tools.analyze(context=context)
        self.assertEqual(output["status"], "SUCCESS")
        self.assertEqual(len(events), 5)
        self.assertEqual([e["step_id"] for e in events], list(RULES))
        self.assertEqual([p for p, _ in self.backend.calls], ["sea-rights", "eco-restore", "history", "coastline", "redline"])
        self.assertTrue(all(not args["save"] for _, args in self.backend.calls))
        self.assertTrue(all(e["details"]["status"] == "SUCCESS" for e in events))
        self.assertEqual(events[0]["summary"], "海域权属校验完成：共100个图斑；未批先建35个，超范围用海0个，改变用海方式0个，本规则无疑点65个。")
        self.assertEqual(events[2]["summary"], "历史疑点校验完成：共100个图斑；命中历史疑点35个，本规则无疑点65个。")
        self.assertNotIn("模拟", json.dumps(events, ensure_ascii=False))
        self.assertNotIn("patches", output["data"])
        self.assertLess(len(json.dumps(output)), 2200)
        self.assertEqual(output["data"]["summary"], {"suspicionPatchCount": 35, "allClearPatchCount": 65, "incompletePatchCount": 0})
        self.assertEqual(output["data"]["assessedRules"], list(RULES))
        self.assertTrue(all(r["counts"] is not None and not r["saved"] for r in output["data"]["ruleResults"]))
        self.assertTrue(all("implemented" not in r for r in output["data"]["ruleResults"]))

    async def test_filter_before_pagination_reads_saved_results(self):
        await self.tools.analyze(save=True)
        args = {"batchNo": "XM-2026-06", "suspicionTypes": ["REDLINE"], "pageSize": 20}
        first = await self.tools.query(**args)
        self.assertEqual(first["data"]["pagination"], {"page": 1, "pageSize": 20, "total": 35, "returned": 20, "hasMore": True})
        second = await self.tools.query(page=2, **args)
        self.assertEqual(second["data"]["patches"][0]["patchNo"], "P020")
        self.assertEqual(second["data"]["pagination"]["returned"], 15)
        self.assertFalse(second["data"]["pagination"]["hasMore"])
        end = await self.tools.query(page=3, **args)
        self.assertEqual(end["data"]["patches"], [])
        self.assertNotIn("evidence", first["data"]["patches"][0]["conclusions"][0])
        detail = await self.tools.query(batchNo="XM-2026-06", patchNos=["P000"], includeEvidence=True)
        self.assertEqual(detail["data"]["patches"][0]["conclusions"][0]["evidence"], "来自后端的实际依据")
        self.assertEqual(len(self.backend.reads), 20)
        self.assertTrue(all(batch == "XM-2026-06" for _, batch in self.backend.reads))

    async def test_trial_is_not_retained_and_does_not_replace_saved_results(self):
        output = await self.tools.analyze()
        self.assertNotIn("analysisId", output["data"])
        self.assertNotIn("expiresAt", output["data"])
        summary = await self.tools.summarize()
        self.assertEqual(summary["data"]["summary"]["incompletePatchCount"], 100)
        query = await self.tools.query(suspicionTypes=["REDLINE"])
        self.assertEqual(query["data"]["pagination"]["total"], 0)
        await self.tools.analyze(save=True)
        await self.tools.analyze(patchNos=["P099"])
        query = await self.tools.query(suspicionTypes=["REDLINE"])
        self.assertEqual(query["data"]["pagination"]["total"], 35)
        self.assertEqual((await self.tools.query(analysisId="removed"))["status"], "FAILED")

    async def test_partial_failure_missing_results_and_input_validation(self):
        self.backend.fail = "coastline"
        output = await self.tools.analyze(save=True)
        self.assertEqual(output["status"], "PARTIAL_SUCCESS")
        summary = await self.tools.summarize()
        coast = next(r for r in summary["data"]["ruleResults"] if r["ruleCode"] == "COASTLINE")
        self.assertIsNone(coast["counts"])
        self.assertEqual(summary["data"]["summary"]["incompletePatchCount"], 100)
        self.backend.fail = None
        await self.tools.analyze(save=True)
        self.backend.missing_coastline = True
        saved = await self.tools.summarize()
        self.assertEqual(saved["status"], "SUCCESS")
        self.assertEqual(saved["data"]["summary"]["incompletePatchCount"], 1)
        for args in ({"rules": []}, {"patchNos": []}, {"patchNos": [" "]}, {"save": "false"}):
            self.assertEqual((await self.tools.analyze(context=self.context, **args))["status"], "FAILED")
        self.assertEqual((await self.tools.query(pageSize=21))["status"], "FAILED")
        selected = await self.tools.analyze(context=self.context, patchNos=["P000"], save=True)
        self.assertEqual(selected["data"]["total"], 1)
        self.assertTrue(all(args["save"] for _, args in self.backend.calls[-5:]))

    async def test_eco_and_history_results_filtering_and_coverage(self):
        await self.tools.analyze(save=True)
        self.backend.saved["eco-restore"]["P099"].update(
            resultType="EXEMPT", inProject=True, projectName="修复项目", approvalState="免审批合规")
        self.backend.saved["eco-restore"]["P098"]["resultType"] = "ECO_CHANGE_MODE"
        self.backend.saved["history"]["P000"].update(
            matchCount=2, relatedId="H001", checkConclusion="历史核查结论", checkDate="2026-09-01")
        exempt = await self.tools.query(suspicionTypes=["EXEMPT"])
        patch = exempt["data"]["patches"][0]
        self.assertFalse(patch["hasSuspicion"])
        self.assertTrue(patch["complete"])
        self.assertEqual(patch["missingRules"], [])
        eco = next(c for c in patch["conclusions"] if c["ruleCode"] == "ECO_RESTORE")
        self.assertEqual(eco["projectName"], "修复项目")
        both = await self.tools.query(suspicionTypes=["ECO_OUT_OF_SCOPE", "HISTORY"], match="all")
        self.assertEqual(both["data"]["pagination"]["total"], 35)
        history = next(c for c in both["data"]["patches"][0]["conclusions"] if c["ruleCode"] == "HISTORY")
        self.assertEqual(history["relatedId"], "H001")
        self.assertEqual(history["checkConclusion"], "历史核查结论")
        self.assertEqual(history["checkDate"], "2026-09-01")
        detail = await self.tools.query(patchNos=["P000"], includeEvidence=True)
        history = next(c for c in detail["data"]["patches"][0]["conclusions"] if c["ruleCode"] == "HISTORY")
        self.assertEqual(history["matchCount"], 2)
        self.assertEqual(history["evidence"], "来自后端的实际依据")
        changed = await self.tools.query(suspicionTypes=["ECO_CHANGE_MODE"])
        self.assertEqual(changed["data"]["pagination"]["total"], 1)
        summary = await self.tools.summarize()
        self.assertEqual(summary["data"]["summary"], {"suspicionPatchCount": 36, "allClearPatchCount": 64, "incompletePatchCount": 0})
        self.assertTrue(all(r["assessedPatchCount"] == 100 for r in summary["data"]["ruleResults"]))
        eco = next(r for r in summary["data"]["ruleResults"] if r["ruleCode"] == "ECO_RESTORE")
        self.assertEqual(eco["counts"]["EXEMPT"], 1)
        del self.backend.saved["history"]["P099"]
        incomplete = await self.tools.query(suspicionTypes=["EXEMPT"])
        self.assertIsNone(incomplete["data"]["patches"][0]["hasSuspicion"])
        self.assertEqual(incomplete["data"]["patches"][0]["missingRules"], ["HISTORY"])

    async def test_auth_failure_and_cancellation_stop_execution(self):
        async def unauthorized(path, payload):
            self.backend.calls.append((path, payload))
            raise SpatialServiceError("凭证失效", http_status=401)
        self.backend.validate = unauthorized
        output = await self.tools.analyze(context=self.context)
        self.assertEqual(output["status"], "FAILED")
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual([r["status"] for r in output["data"]["ruleResults"]], ["FAILED"] + ["SKIPPED"] * 4)
        started, stopped = asyncio.Event(), asyncio.Event()
        async def blocking(path, payload):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.backend.validate = blocking
        executor = ToolExecutor()
        self.tools.register(executor)
        task = asyncio.create_task(executor.execute("analyze_suspicion", {}))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())
        with self.assertRaisesRegex(ValueError, "上下文"):
            await executor.execute_with_events("analyze_suspicion", {"context": {"on_progress": "invalid"}})

    @patch("services.chat_service.generate_recommendations", return_value=[])
    async def test_sse_progress_and_generic_transport(self, _recommendations):
        executor = ToolExecutor()
        self.tools.register(executor)
        async def unrelated(*, context):
            await context.report(step_id="stage-a", kind="stage", status="success", title="任意步骤", summary="完成", details={"value": 42})
            return {"success": True}
        executor.register("unrelated", "test", unrelated, contextual=True)
        llm = FakeLlm([
            ([], [{"id": "call-analysis", "name": "analyze_suspicion", "args": {}}]),
            ([], [{"id": "other", "name": "unrelated", "args": {}}]),
            (["完成。"], []),
        ])
        service = ChatService()
        service._agent = AgentLoop(llm, executor)
        self.addAsyncCleanup(service.close)
        repo = MemoryConversationRepository()
        with patch("services.chat_service.conversation_repository", repo), patch("services.context_service.expand_query_synonyms", side_effect=lambda v: v):
            events = [e async for e in service.stream_chat(AgentRequest(question="研判图斑"), user_id="test-user")]
        process = [e for e in events if e["event"] == "process"]
        self.assertEqual(len(process), 10)
        self.assertTrue(all("id" in e for e in process))
        self.assertEqual([e["data"]["status"] for e in process[:7]], ["running"] + ["success"] * 6)
        self.assertEqual(process[0]["data"]["step_id"], "call-analysis")
        self.assertEqual(process[6]["data"]["step_id"], "call-analysis")
        self.assertEqual(process[1]["data"]["step_id"], "call-analysis:SEA_RIGHTS")
        self.assertEqual(process[1]["data"]["parent_id"], "call-analysis")
        self.assertEqual(process[-2]["data"]["kind"], "stage")
        self.assertEqual(process[-2]["data"]["details"], {"value": 42})


class SuspicionBackendTest(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_pagination_and_validate_request_policy(self):
        from unittest.mock import AsyncMock
        from tool_backends.suspicion_backend import SuspicionBackend
        client = AsyncMock()
        client.get_spatial_element_detail.return_value = {"maxQueryLimit": 2, "fields": [{"name": "batch_no"}, {"name": "patch_no"}]}
        client.query_spatial_element.side_effect = [
            {"total": 3, "features": [{"properties": {"batch_no": "XM-2026-06", "patch_no": "P0"}}]},
            {"total": 3, "features": [{"properties": {"patch_no": "P0"}}, {"properties": {"patch_no": "P1"}}]},
            {"total": 3, "features": [{"properties": {"patch_no": "P2"}}]},
        ]
        backend = SuspicionBackend(client)
        batch, keys = await backend.inventory(None)
        self.assertEqual(batch, "XM-2026-06")
        self.assertEqual(keys, {"P0", "P1", "P2"})
        self.assertFalse(client.query_spatial_element.call_args.kwargs)
        self.assertEqual(client.query_spatial_element.call_args.args[0]["offset"], 2)
        await backend.validate("sea-rights", {"batchNo": batch, "save": True})
        self.assertEqual(client._request.call_args.args, ("POST", "/api/spatial/suspicion/sea-rights/validate"))
        self.assertEqual(client._request.call_args.kwargs["retries"], 0)
        await backend.results("redline", batch)
        self.assertEqual(client._request.call_args.kwargs["params"], {"batchNo": batch})
        self.assertIs(client._request.call_args.kwargs["data_type"], list)
