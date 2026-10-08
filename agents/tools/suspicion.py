"""Suspicion workflow, pagination and UI progress; all business decisions stay here."""
from collections import Counter
from copy import deepcopy
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agents.tools.context import ToolContext
from tool_backends.spatial_backend import SpatialServiceError
from tool_backends.suspicion_backend import SuspicionBackend

RULES = {
    "SEA_RIGHTS": ("sea-rights", "海域权属校验", ("NO_APPROVAL", "OUT_OF_SCOPE", "CHANGE_MODE", "CLEAR")),
    "ECO_RESTORE": ("eco-restore", "生态修复项目校验", ("ECO_OUT_OF_SCOPE", "ECO_CHANGE_MODE", "EXEMPT", "NO_APPROVAL", "CLEAR")),
    "HISTORY": ("history", "历史疑点校验", ("HISTORY", "CLEAR")),
    "COASTLINE": ("coastline", "自然岸线校验", ("OCCUPY_COASTLINE", "CLEAR")),
    "REDLINE": ("redline", "生态保护红线校验", ("REDLINE", "CLEAR")),
}
RESULT_LABELS = {
    "NO_APPROVAL": "未批先建", "OUT_OF_SCOPE": "超范围用海", "CHANGE_MODE": "改变用海方式",
    "ECO_OUT_OF_SCOPE": "修复项目内超范围", "ECO_CHANGE_MODE": "修复项目内改变用海方式",
    "EXEMPT": "免审批合规", "HISTORY": "命中历史疑点", "OCCUPY_COASTLINE": "占用自然岸线",
    "REDLINE": "占用生态保护红线", "CLEAR": "本规则无疑点",
}


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    batchNo: Optional[str] = Field(default=None, min_length=1, description="批次号，省略取最新可排序批次。")

    @field_validator("batchNo", check_fields=False)
    @classmethod
    def valid_string(cls, value):
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError("编号不得为空或含首尾空格")
        return value


class Analyze(Scope):
    patchNos: Optional[list[str]] = Field(default=None, min_length=1, description="图斑编号数组，省略处理整批。")
    save: bool = Field(default=False, description="默认试算；明确要求保存或更新正式结果时为true。")

    @field_validator("patchNos")
    @classmethod
    def valid_patches(cls, values):
        if values is not None:
            for value in values:
                cls.valid_string(value)
            return list(dict.fromkeys(values))
        return values


class Query(Scope):
    patchNos: Optional[list[str]] = Field(default=None, min_length=1, description="只查询指定图斑编号。")
    suspicionTypes: Optional[list[Literal["NO_APPROVAL", "OUT_OF_SCOPE", "CHANGE_MODE", "ECO_OUT_OF_SCOPE", "ECO_CHANGE_MODE", "EXEMPT", "HISTORY", "OCCUPY_COASTLINE", "REDLINE"]]] = Field(default=None, min_length=1, description="结论类型筛选，省略不筛选。EXEMPT表示免审批合规，不计入疑点。")
    match: Literal["any", "all"] = Field(default="any", description="多个结论条件满足任一或同时满足。")
    page: int = Field(default=1, ge=1, description="页码，默认只返回第一页。")
    pageSize: int = Field(default=10, ge=1, le=20, description="每页数量，默认10，最多20个图斑。")
    includeEvidence: bool = Field(default=False, description="需要解释具体图斑时返回依据及关联对象。")

    @field_validator("patchNos")
    @classmethod
    def valid_patches(cls, values):
        return Analyze.valid_patches(values)


def error(message, rule=None):
    return {"ruleCode": rule, "message": str(message)[:500]}


def response(data, errors):
    successes = any(r["status"] == "SUCCESS" for r in data.get("ruleResults", []))
    status = "SUCCESS" if not errors else ("PARTIAL_SUCCESS" if successes else "FAILED")
    return {"status": status, "success": status != "FAILED", "data": data, "errors": errors,
            **({"error": {"type": "SuspicionError", "message": errors[0]["message"], "retryable": False}} if status == "FAILED" else {})}


def merge(patch_nos, results):
    patches = {key: {"patchNo": key, "conclusions": []} for key in sorted(patch_nos)}
    for rule, rows in results.items():
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("规则返回了无效图斑记录")
            key = row.get("patchNo")
            if key not in patches or key in seen or row.get("resultType") not in RULES[rule][2]:
                raise ValueError("规则结果包含无效编号、重复图斑或未知结论")
            seen.add(key)
            # Preserve source attribute names/units. Do not translate the ambiguous overlapRatio to percent.
            patches[key]["conclusions"].append({"ruleCode": rule, **{k: v for k, v in row.items() if k != "patchNo"}})
    for patch in patches.values():
        present = {c["ruleCode"] for c in patch["conclusions"]}
        patch["missingRules"] = [r for r in RULES if r not in present]
        patch["complete"] = not patch["missingRules"]
        suspicious = any(c["resultType"] not in ("CLEAR", "EXEMPT") for c in patch["conclusions"])
        patch["hasSuspicion"] = True if suspicious else (False if patch["complete"] else None)
    return list(patches.values())


def totals(patches):
    return {"suspicionPatchCount": sum(p["hasSuspicion"] is True for p in patches),
            "allClearPatchCount": sum(p["hasSuspicion"] is False for p in patches),
            "incompletePatchCount": sum(not p["complete"] for p in patches)}


class SuspicionTools:
    def __init__(self, backend=None):
        self.backend = backend or SuspicionBackend()

    async def analyze(self, *, context=ToolContext(), **arguments):
        results, rule_results, errors = {}, [], []
        try:
            args = Analyze.model_validate(arguments)
            batch, patch_nos = await self.backend.inventory(args.batchNo)
            if args.patchNos is not None:
                if set(args.patchNos) - patch_nos:
                    raise ValueError("部分图斑不属于指定批次")
                patch_nos = set(args.patchNos)
            for index, (rule, (path, title, codes)) in enumerate(RULES.items(), 1):
                details = {"ruleCode": rule, "ruleIndex": index, "batchNo": batch,
                           "finishedRules": index, "totalRules": len(RULES)}
                stop = False
                try:
                    payload = {"batchNo": batch, "save": args.save}
                    if args.patchNos is not None:
                        payload["patchNos"] = args.patchNos
                    result = await self.backend.validate(path, payload)
                    rows = result.get("verdicts")
                    if result.get("batchNo") != batch or not isinstance(rows, list) or len(rows) != len(patch_nos):
                        raise ValueError("返回批次或图斑数量不一致")
                    merge(patch_nos, {rule: rows})
                    if result.get("saved") is not args.save:
                        raise ValueError("保存状态与请求不一致，请核查正式结果")
                    results[rule] = rows
                    item = {"ruleCode": rule, "status": "SUCCESS",
                            "saved": result["saved"], "counts": {code: sum(row["resultType"] == code for row in rows) for code in codes}}
                    if "thresholds" in result:
                        item["thresholds"] = result["thresholds"]
                    counts_text = "，".join(f"{RESULT_LABELS[code]}{count}个" for code, count in item["counts"].items())
                    summary = f"{title}完成：共{len(rows)}个图斑；{counts_text}。"
                except (SpatialServiceError, ValueError) as exc:
                    errors.append(error(exc, rule))
                    item = {"ruleCode": rule, "status": "FAILED", "saved": None, "counts": None}
                    summary = f"{title}失败"
                    stop = getattr(exc, "http_status", None) in (401, 403)
                rule_results.append(item)
                await context.report(
                    step_id=rule, kind="rule", status=item["status"].lower(), title=title,
                    summary=summary,
                    details={**details, **item},
                )
                if stop:
                    for remaining in list(RULES)[index:]:
                        rule_results.append({"ruleCode": remaining, "status": "SKIPPED", "counts": None, "saved": False})
                    break
            patches = merge(patch_nos, results)
            data = {"batchNo": batch, "total": len(patches), "saveRequested": args.save,
                    "assessedRules": list(RULES), "totalRules": len(RULES),
                    "finishedRules": sum(r["status"] != "SKIPPED" for r in rule_results),
                    "ruleResults": rule_results, "summary": totals(patches)}
            output = response(data, errors)
        except (ValidationError, ValueError, SpatialServiceError) as exc:
            output = response({"ruleResults": rule_results}, [error(exc)])
        return output

    async def load(self, args):
        batch, keys = await self.backend.inventory(args.batchNo)
        results, states, errors = {}, [], []
        for rule, (path, _, _) in RULES.items():
            try:
                rows = await self.backend.results(path, batch)
                merge(keys, {rule: rows})
                results[rule] = rows
                states.append({"ruleCode": rule, "status": "SUCCESS"})
            except (SpatialServiceError, ValueError) as exc:
                errors.append(error(exc, rule))
                states.append({"ruleCode": rule, "status": "FAILED"})
        return {"batchNo": batch, "ruleResults": states}, merge(keys, results), errors

    async def query(self, **arguments):
        try:
            args = Query.model_validate(arguments)
            data, patches, errors = await self.load(args)
            selected = []
            for patch in patches:
                if args.patchNos is not None and patch["patchNo"] not in args.patchNos:
                    continue
                if args.suspicionTypes:
                    codes = {c["resultType"] for c in patch["conclusions"]}
                    matches = [code in codes for code in args.suspicionTypes]
                    if not (all(matches) if args.match == "all" else any(matches)):
                        continue
                selected.append(patch)
            start = (args.page - 1) * args.pageSize
            page = deepcopy(selected[start:start + args.pageSize])
            if not args.includeEvidence:
                compact_fields = {"ruleCode", "resultType", "useType", "useMode", "areaHa", "inProject", "projectName", "approvalState", "relatedId", "checkConclusion", "checkDate", "outsideAreaHa", "outsideRatioPct", "overlapAreaHa", "overlapRatio", "overlapRatioPct", "overlapLengthM", "naturalLengthM", "anyLengthM", "createdAt"}
                for patch in page:
                    patch["conclusions"] = [{k: v for k, v in c.items() if k in compact_fields} for c in patch["conclusions"]]
            return response({"batchNo": data["batchNo"],
                             "ruleResults": data["ruleResults"], "assessedRules": list(RULES),
                             "pagination": {"page": args.page, "pageSize": args.pageSize, "total": len(selected),
                                            "returned": len(page), "hasMore": start + len(page) < len(selected)}, "patches": page}, errors)
        except (ValidationError, ValueError, SpatialServiceError) as exc:
            return response({}, [error(exc)])

    async def summarize(self, **arguments):
        try:
            args = Scope.model_validate(arguments)
            data, patches, errors = await self.load(args)
            rules = []
            for item in data["ruleResults"]:
                rule = item["ruleCode"]
                conclusions = [c for p in patches for c in p["conclusions"] if c["ruleCode"] == rule]
                counts = Counter(c["resultType"] for c in conclusions)
                rules.append({**item, "assessedPatchCount": len(conclusions),
                              "unassessedPatchCount": len(patches) - len(conclusions),
                              "counts": {code: counts[code] for code in RULES[rule][2]} if item["status"] == "SUCCESS" else None})
            return response({"batchNo": data["batchNo"], "totalPatchCount": len(patches),
                             "assessedRules": list(RULES), "ruleResults": rules, "summary": totals(patches)}, errors)
        except (ValidationError, ValueError, SpatialServiceError) as exc:
            return response({}, [error(exc)])

    def register(self, executor):
        for name, function, schema, description in (
            ("analyze_suspicion", self.analyze, Analyze, "串行调用五项规则服务执行真实研判，每条规则完成后上报进度。默认试算，save=true保存正式结果。仅返回本次摘要，不暂存。save=false的试算明细不能后续查询。"),
            ("query_suspicion_results", self.query, Query, "先筛选再分页查询；默认只返回第一页10个，最多20个。includeEvidence=true返回依据。只查询空间服务已保存的正式批次结果。"),
            ("summarize_suspicion_results", self.summarize, Scope, "查询规则计数、覆盖情况和去重图斑数量，不返回明细。只统计空间服务已保存的正式批次结果。"),
        ):
            executor.register(name, "调用前先加载“疑点疑区” Skill。" + description, function, schema.model_json_schema(),
                              contextual=name == "analyze_suspicion")
        executor.register_closer(self.close)

    async def close(self):
        await self.backend.close()
