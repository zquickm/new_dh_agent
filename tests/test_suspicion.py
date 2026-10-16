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


# OpenAPI 1.6.0 wire responses contain Chinese conclusions, without resultType.
CONCLUSIONS = {
    "sea-rights": ("疑似未批先建", "有权-合规"),
    "eco-restore": ("疑似修复项目内超范围", "未占用修复区域"),
    "history": ("疑似命中历史疑点", "未命中历史疑点"),
    "coastline": ("疑似占用自然岸线", "未占用自然岸线"),
    "redline": ("疑似占用生态保护红线", "未占用生态保护红线"),
}


class Backend:
    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []
        self.before = None
        self.saved = {}
        self.reads = []
        self.missing_coastline = False

    def rows(self, path, keys=None):
        keys = sorted(keys) if keys is not None else [f"P{i:03}" for i in range(100)]
        return [{"patchNo": k, "conclusion": CONCLUSIONS[path][0 if int(k[1:]) < 35 else 1],
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
        return {"batchNo": payload.get("batchNo", "XM-2026-06"), "total": len(rows), "saved": payload["save"], "verdicts": rows}

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
        self.assertNotIn("batchNo", self.backend.calls[0][1])
        self.assertTrue(all(args["batchNo"] == "XM-2026-06" for _, args in self.backend.calls[1:]))
        self.assertTrue(all(e["details"]["status"] == "SUCCESS" for e in events))
        self.assertEqual(events[0]["summary"], "海域权属校验完成：共100个图斑；疑似未批先建35个，疑似超范围用海0个，疑似改变用海方式0个，有权-合规65个。")
        self.assertEqual(events[2]["summary"], "历史疑点校验完成：共100个图斑；疑似命中历史疑点35个，未命中历史疑点65个。")
        self.assertNotIn("模拟", json.dumps(events, ensure_ascii=False))
        self.assertNotIn("patches", output["data"])
        self.assertLess(len(json.dumps(output, ensure_ascii=False)), 2200)
        self.assertEqual(output["data"]["ruleResults"][0]["counts"], {
            "疑似未批先建": 35, "疑似超范围用海": 0, "疑似改变用海方式": 0, "有权-合规": 65,
        })
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
        self.assertEqual(first["data"]["patches"][0]["conclusions"][0]["conclusion"], "疑似未批先建")
        self.assertNotIn("resultType", json.dumps(first))
        detail = await self.tools.query(batchNo="XM-2026-06", patchNos=["P000"], includeEvidence=True)
        self.assertEqual(detail["data"]["patches"][0]["conclusions"][0]["evidence"], "来自后端的实际依据")
        self.assertEqual(len(self.backend.reads), 20)
        self.assertTrue(all(batch == "XM-2026-06" for _, batch in self.backend.reads))

    async def test_trial_is_not_retained_and_does_not_replace_saved_results(self):
        output = await self.tools.analyze()
        self.assertNotIn("analysisId", output["data"])
        self.assertNotIn("expiresAt", output["data"])
        summary = await self.tools.summarize()
        self.assertEqual(summary["data"]["totalPatchCount"], 0)
        self.assertEqual(summary["data"]["summary"]["incompletePatchCount"], 0)
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
            conclusion="合规免审批", projectCount=2, projects="修复项目甲;修复项目乙", exempt=True,
            approvalState="合规免审批", rightsMethods="其他方式", patchId=99, eventNo="E099")
        self.backend.saved["eco-restore"]["P098"]["conclusion"] = "疑似修复项目内改变用海方式"
        self.backend.saved["eco-restore"]["P097"].update(
            projectCount=0, projects=None, approvalState=None, outsideAreaHa=None, outsideRatioPct=None)
        self.backend.saved["history"]["P000"].update(
            historyCount=2, historyPatchNos="H001、H002", checkConclusions="历史结论甲、历史结论乙",
            lastCheckDate="2026-09-01", patchId=1, eventNo="E000", areaHa=0.5)
        exempt = await self.tools.query(suspicionTypes=["EXEMPT"])
        patch = exempt["data"]["patches"][0]
        self.assertFalse(patch["hasSuspicion"])
        self.assertTrue(patch["complete"])
        self.assertEqual(patch["missingRules"], [])
        eco = next(c for c in patch["conclusions"] if c["ruleCode"] == "ECO_RESTORE")
        self.assertEqual(eco["conclusion"], "合规免审批")
        self.assertEqual(eco["projectCount"], 2)
        self.assertEqual(eco["projects"], "修复项目甲;修复项目乙")
        self.assertTrue(eco["exempt"])
        self.assertEqual(eco["eventNo"], "E099")
        outside = await self.tools.query(patchNos=["P097"])
        eco_outside = next(c for c in outside["data"]["patches"][0]["conclusions"] if c["ruleCode"] == "ECO_RESTORE")
        for field in ("approvalState", "outsideAreaHa", "outsideRatioPct"):
            self.assertIn(field, eco_outside)
            self.assertIsNone(eco_outside[field])
        both = await self.tools.query(suspicionTypes=["ECO_OUT_OF_SCOPE", "HISTORY"], match="all")
        self.assertEqual(both["data"]["pagination"]["total"], 35)
        history = next(c for c in both["data"]["patches"][0]["conclusions"] if c["ruleCode"] == "HISTORY")
        self.assertEqual(history["historyPatchNos"], "H001、H002")
        self.assertEqual(history["checkConclusions"], "历史结论甲、历史结论乙")
        self.assertEqual(history["lastCheckDate"], "2026-09-01")
        self.assertEqual(history["historyCount"], 2)
        self.assertEqual(history["areaHa"], 0.5)
        detail = await self.tools.query(patchNos=["P000"], includeEvidence=True)
        history = next(c for c in detail["data"]["patches"][0]["conclusions"] if c["ruleCode"] == "HISTORY")
        self.assertEqual(history["historyCount"], 2)
        self.assertEqual(history["evidence"], "来自后端的实际依据")
        detail = await self.tools.query(patchNos=["P099"], includeEvidence=True)
        eco = next(c for c in detail["data"]["patches"][0]["conclusions"] if c["ruleCode"] == "ECO_RESTORE")
        self.assertEqual(eco["rightsMethods"], "其他方式")
        changed = await self.tools.query(suspicionTypes=["ECO_CHANGE_MODE"])
        self.assertEqual(changed["data"]["pagination"]["total"], 1)
        summary = await self.tools.summarize()
        self.assertEqual(summary["data"]["summary"], {"suspicionPatchCount": 36, "allClearPatchCount": 64, "incompletePatchCount": 0})
        self.assertTrue(all(r["assessedPatchCount"] == 100 for r in summary["data"]["ruleResults"]))
        eco = next(r for r in summary["data"]["ruleResults"] if r["ruleCode"] == "ECO_RESTORE")
        self.assertEqual(eco["counts"]["合规免审批"], 1)
        del self.backend.saved["history"]["P099"]
        incomplete = await self.tools.query(suspicionTypes=["EXEMPT"])
        self.assertIsNone(incomplete["data"]["patches"][0]["hasSuspicion"])
        self.assertEqual(incomplete["data"]["patches"][0]["missingRules"], ["HISTORY"])

    async def test_shared_no_approval_filter_and_unknown_conclusions_fail_closed(self):
        await self.tools.analyze(save=True)
        self.backend.saved["eco-restore"]["P099"]["conclusion"] = "疑似未批先建"
        selected = await self.tools.query(suspicionTypes=["NO_APPROVAL"], page=4)
        self.assertEqual(selected["data"]["pagination"]["total"], 36)
        self.assertEqual(selected["data"]["patches"][-1]["patchNo"], "P099")
        for invalid in (None, "未知结论", "未批先建", "有权-合规"):
            with self.subTest(conclusion=invalid):
                self.backend.saved["history"]["P000"]["conclusion"] = invalid
                query = await self.tools.query()
                summary = await self.tools.summarize()
                self.assertEqual(query["status"], "PARTIAL_SUCCESS")
                self.assertEqual(summary["data"]["summary"]["incompletePatchCount"], 100)
                history = next(r for r in summary["data"]["ruleResults"] if r["ruleCode"] == "HISTORY")
                self.assertIsNone(history["counts"])
                self.assertIn("未知结论", query["errors"][0]["message"])

        validate = self.backend.validate
        async def invalid_validate(path, payload):
            result = await validate(path, payload)
            if path == "history":
                result["verdicts"][0].pop("conclusion")
            return result
        self.backend.validate = invalid_validate
        output = await self.tools.analyze()
        self.assertEqual(output["status"], "PARTIAL_SUCCESS")
        self.assertEqual(output["data"]["ruleResults"][2]["status"], "FAILED")
        self.assertEqual(output["data"]["summary"]["incompletePatchCount"], 100)

    async def test_auth_failure_and_cancellation_stop_execution(self):
        async def unauthorized(path, payload):
            self.backend.calls.append((path, payload))
            raise SpatialServiceError("凭证失效", http_status=401)
        self.backend.validate = unauthorized
        output = await self.tools.analyze(context=self.context)
        self.assertEqual(output["status"], "FAILED")
        self.assertEqual(len(self.backend.calls), 1)
        self.assertIsNone(output["data"]["total"])
        self.assertIsNone(output["data"]["summary"])
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

    async def test_first_rule_failure_and_business_scope_validation(self):
        self.backend.fail = "sea-rights"
        output = await self.tools.analyze()
        self.assertEqual(output["status"], "PARTIAL_SUCCESS")
        self.assertEqual(output["data"]["batchNo"], "XM-2026-06")
        self.assertEqual(output["data"]["summary"]["incompletePatchCount"], 100)
        self.assertNotIn("batchNo", self.backend.calls[1][1])
        self.assertTrue(all(args["batchNo"] == "XM-2026-06" for _, args in self.backend.calls[2:]))
        self.backend.fail = None
        validate = self.backend.validate
        async def bad_scope(path, payload):
            result = await validate(path, payload)
            if path == "eco-restore":
                result["verdicts"][-1]["patchNo"] = "OTHER"
            return result
        self.backend.validate = bad_scope
        output = await self.tools.analyze(batchNo="XM-2026-06")
        self.assertEqual(output["data"]["ruleResults"][1]["status"], "FAILED")
        self.assertIn("范围或数量不一致", output["errors"][0]["message"])

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
            (["完成。"], []),
        ])
        service = ChatService()
        service._agent = AgentLoop(llm, executor)
        self.addAsyncCleanup(service.close)
        repo = MemoryConversationRepository()
        with patch("services.chat_service.conversation_repository", repo), patch("services.context_service.expand_query_synonyms", side_effect=lambda v: v):
            events = [e async for e in service.stream_chat(AgentRequest(question="研判图斑"), user_id="test-user")]
        process = [e for e in events if e["event"] == "process"]
        self.assertEqual(len(process), 7)
        self.assertTrue(all("id" in e for e in process))
        self.assertEqual([e["data"]["status"] for e in process[:7]], ["running"] + ["success"] * 6)
        self.assertEqual(process[0]["data"]["step_id"], "call-analysis")
        self.assertEqual(process[6]["data"]["step_id"], "call-analysis")
        self.assertEqual(process[1]["data"]["step_id"], "call-analysis:SEA_RIGHTS")
        self.assertEqual(process[1]["data"]["parent_id"], "call-analysis")
        self.assertEqual(process[1]["data"]["details"]["counts"]["疑似未批先建"], 35)
        self.assertIn("疑似未批先建", process[1]["data"]["summary"])

        # The suspicion request scope blocks unrelated tools; exercise generic
        # progress transport through an ordinary request where that tool is available.
        service._agent = AgentLoop(FakeLlm([
            ([], [{"id": "other", "name": "unrelated", "args": {}}]),
            (["完成。"], []),
        ]), executor)
        with patch("services.chat_service.conversation_repository", repo), patch("services.context_service.expand_query_synonyms", side_effect=lambda v: v):
            events = [e async for e in service.stream_chat(AgentRequest(question="运行通用工具"), user_id="test-user")]
        process = [e for e in events if e["event"] == "process"]
        self.assertEqual(len(process), 3)
        self.assertEqual(process[-2]["data"]["kind"], "stage")
        self.assertEqual(process[-2]["data"]["details"], {"value": 42})


class SuspicionBackendTest(unittest.IsolatedAsyncioTestCase):
    async def test_tools_use_only_business_endpoints(self):
        from unittest.mock import AsyncMock
        from tool_backends.suspicion_backend import SuspicionBackend
        client = AsyncMock()
        async def respond(method, path, **kwargs):
            rule_path = path.split("/")[-2]
            rows = [{"patchNo": "P0", "conclusion": CONCLUSIONS[rule_path][1]}]
            if method == "GET":
                self.assertIs(kwargs["data_type"], list)
                return rows
            self.assertEqual(kwargs["retries"], 0)
            self.assertFalse(kwargs["json"]["save"])
            return {"batchNo": "XM-2026-06", "saved": False, "total": 1, "verdicts": rows}
        client._request.side_effect = respond
        backend = SuspicionBackend(client)
        self.addAsyncCleanup(backend.close)
        tools = SuspicionTools(backend)
        self.assertEqual((await tools.analyze())["status"], "SUCCESS")
        self.assertEqual((await tools.query())["data"]["pagination"]["total"], 1)
        self.assertEqual((await tools.summarize())["data"]["totalPatchCount"], 1)
        expected = [("POST", f"/api/spatial/suspicion/{path}/validate") for path, _, _ in RULES.values()]
        expected += [("GET", f"/api/spatial/suspicion/{path}/results") for path, _, _ in RULES.values()] * 2
        self.assertEqual([call.args for call in client._request.call_args_list], expected)
        self.assertTrue(all(call.kwargs["params"] == {} for call in client._request.call_args_list[5:]))
        client.get_spatial_element_detail.assert_not_awaited()
        client.query_spatial_element.assert_not_awaited()
        await backend.results("redline", "XM-2026-06")
        self.assertEqual(client._request.call_args.kwargs["params"], {"batchNo": "XM-2026-06"})
        self.assertIs(client._request.call_args.kwargs["data_type"], list)
