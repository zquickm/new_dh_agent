from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agents.tools.executor import ToolOutput
from config.settings import get_settings
from tool_backends.spatial_backend import SpatialServiceClient, SpatialServiceError


ANALYSIS_ACTION_TYPE = "DUANMIAN_ANALYSIS"
SECTIONS = ("E1", "E2", "E3")
CHEMISTRY_ANALYSES = {"temp-section", "ts-scatter", "nutrient", "do-profile"}
ELEMENT_ANALYSES = {"depth-curve", "temp-section", "surface"}
ELEMENT_ALIASES = {
    "温度": "temperature_c",
    "水温": "temperature_c",
    "海水温度": "temperature_c",
    "盐度": "salinity_psu",
    "溶解氧": "dissolved_oxygen_umol_dm3",
    "pH": "ph_value",
    "活性磷酸盐": "po4_p_umol_dm3",
    "活性硅酸盐": "sio3_si_umol_dm3",
    "亚硝酸盐": "no2_n_umol_dm3",
    "硝酸盐": "no3_n_umol_dm3",
    "氨氮": "nh4_n_umol_dm3",
    "碱度": "alkalinity_mmol_dm3",
    "总碱度": "alkalinity_mmol_dm3",
    "气温": "air_temperature_c",
    "风速": "wind_speed_ms",
    "气压": "air_pressure_hpa",
}


def _date(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期格式必须为 yyyy-MM-dd")
    return date.fromisoformat(value)


class _CodeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    code: str = Field(
        min_length=1,
        description="断面前缀如 E1/EK/EL 或完整测点编码如 E1001；不区分大小写，保留连字符。",
    )

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.upper()


class _ObservationArguments(_CodeArguments):
    start_date: str = Field(alias="from", description="起始日期 yyyy-MM-dd，包含当日。")
    end_date: str = Field(
        alias="to", description="结束日期 yyyy-MM-dd，包含当日；跨度不超过三个自然月。"
    )
    page: int = Field(default=1, ge=1, description="页码，从 1 开始。")
    size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="每页最多 1000 条，默认 100；返回部分不能作为全部数据统计。",
    )

    @model_validator(mode="after")
    def validate_dates(self) -> _ObservationArguments:
        start, end = _date(self.start_date), _date(self.end_date)
        year, month = divmod(start.year * 12 + start.month - 1 + 3, 12)
        maximum = date(
            year, month + 1, min(start.day, calendar.monthrange(year, month + 1)[1])
        )
        if end < start or end > maximum:
            raise ValueError(
                "from/to 必须正序且跨度不超过三个自然月；更长跨度使用数据库统计"
            )
        return self


class _AnalysisArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    analysis: Literal[
        "depth-curve", "temp-section", "ts-scatter", "nutrient", "do-profile", "surface"
    ] = Field(
        description="沿深度曲线、化学剖面、全部断面温盐散点、营养盐、溶解氧垂向、表层沿程。"
    )
    section: Optional[str] = Field(
        default=None,
        description="E1/E2/E3；自动出图时除 ts-scatter 外必须明确指定，ts-scatter 不传。",
    )
    voyage: Optional[str] = Field(
        default=None,
        description="可用航次 yyyy-MM 或 yyyy-MM-dd；省略只打开面板，不自动查询。最新航次先调用 dm_duanmian_voyages 确认。",
    )
    element_type: Optional[Literal["chemistry", "hydromet"]] = Field(
        default=None,
        alias="elementType",
        description="要素类型；省略默认 chemistry。化学剖面、温盐、营养盐、溶解氧图使用 chemistry。",
    )
    element: Optional[str] = Field(
        default=None,
        min_length=1,
        description="要素物理列名或返回字段标签/中文短名。自动出图的 depth-curve、temp-section、surface 必填。",
    )

    @model_validator(mode="after")
    def validate_analysis(self) -> _AnalysisArguments:
        if self.section is not None:
            self.section = self.section.upper()
            if self.section not in SECTIONS:
                raise ValueError(
                    "分析面板只支持 E1/E2/E3；EK/EL 可以查询观测但不能在该面板出图"
                )
        if self.analysis == "ts-scatter" and self.section is not None:
            raise ValueError(
                "ts-scatter 固定合并 E1/E2/E3，不能指定单断面或静默扩大范围"
            )
        if self.analysis in CHEMISTRY_ANALYSES and self.element_type == "hydromet":
            raise ValueError(f"{self.analysis} 只支持 chemistry")
        if self.analysis in {"ts-scatter", "nutrient"} and self.element is not None:
            raise ValueError(f"{self.analysis} 为多要素分析，不接受单个 element")
        if self.analysis == "do-profile" and self.element not in (
            None,
            "溶解氧",
            "dissolved_oxygen_umol_dm3",
        ):
            raise ValueError("do-profile 只展示溶解氧")
        if self.voyage is not None:
            _date(
                self.voyage + "-01"
                if re.fullmatch(r"\d{4}-\d{2}", self.voyage)
                else self.voyage
            )
            if self.analysis != "ts-scatter" and self.section is None:
                raise ValueError("自动出图需要 section；仅打开面板时省略 voyage")
            if self.analysis in ELEMENT_ANALYSES and self.element is None:
                raise ValueError("自动出图需要 element，不能复用面板残留选择")
        return self


TOOL_DEFINITIONS = {
    "dm_duanmian_overview": (
        "查询 E1/E2/E3 断面一览：断面/测点/指标数、记录总量、最新观测日期及分断面明细。无入参；不代表 EK/EL 等全部断面。",
        {"type": "object", "properties": {}, "additionalProperties": False},
        "overview",
    ),
    "dm_duanmian_latest": (
        "查询断面或测点各站最近一次化学/水文观测，保留各深度层、字段标签和单位。各站/数据类型日期可能不同，不是共同航次，不代替指定航次出图。",
        _CodeArguments.model_json_schema(),
        "latest",
    ),
    "dm_duanmian_observations": (
        "查询指定断面/测点、起止日期内的化学/水文逐条明细，正序分页，跨度不超过三个月。保留日期、深度和缺测；partial=true 时不能按本页做全量统计。复杂/跨期统计用 database_query。",
        _ObservationArguments.model_json_schema(),
        "observations",
    ),
    "dm_duanmian_voyages": (
        "查询断面或测点可用观测日，按日期倒序，附化学/水文覆盖站数。出图前确认航次；覆盖站数不保证目标指标有值，不把多日静默合并成一个月。",
        _CodeArguments.model_json_schema(),
        "voyages",
    ),
    "dm_duanmian_analysis": (
        "通过既有 map_action SSE 打开断面专题分析面板。六种图使用 analysis 枚举；不传 voyage 只打开面板，传入则校验航次和字段后请求自动出图。"
        "仅展示指令，不是取数/渲染完成回执，不提供分析结论；解释图表必须另查同口径实测数据。ts-scatter 固定全部三条断面，不支持只看某一断面。",
        _AnalysisArguments.model_json_schema(),
        "analysis",
    ),
}


class DuanmianServiceTool:
    def __init__(self, client: Optional[SpatialServiceClient] = None) -> None:
        self._client = client

    @property
    def client(self) -> SpatialServiceClient:
        if self._client is None:
            settings = get_settings().tools.spatial
            self._client = SpatialServiceClient(
                base_url=settings.base_url,
                api_key=settings.api_key.get_secret_value(),
                timeout=settings.timeout,
                connect_timeout=settings.connect_timeout,
                verify_ssl=settings.verify_ssl,
                max_retries=settings.max_retries,
                retry_backoff=settings.retry_backoff,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def overview(self) -> dict[str, Any]:
        data = await self.client.get_duanmian_overview()
        if not isinstance(data.get("overview"), dict) or not isinstance(
            data.get("sections"), list
        ):
            raise SpatialServiceError("断面概况响应结构无效")
        return {**data, "scope_sections": list(SECTIONS)}

    async def latest(self, **arguments: Any) -> dict[str, Any]:
        args = _CodeArguments.model_validate(arguments)
        data = await self.client.get_duanmian_latest(args.code)
        self._validate_scope(data, args.code)
        self._validate_rows(data)
        return {
            **data,
            "snapshot_policy": "per_station_latest",
            "returned": len(data["rows"]),
        }

    async def observations(self, **arguments: Any) -> dict[str, Any]:
        args = _ObservationArguments.model_validate(arguments)
        data = await self.client.get_duanmian_observations(
            code=args.code,
            start_date=args.start_date,
            end_date=args.end_date,
            page=args.page,
            size=args.size,
        )
        self._validate_scope(data, args.code)
        self._validate_rows(data)
        total, rows = data.get("total"), data["rows"]
        offset = (args.page - 1) * args.size
        if (
            type(total) is not int
            or total < 0
            or len(rows) > args.size
            or (rows and offset + len(rows) > total)
            or data.get("page") != args.page
            or data.get("size") != args.size
            or data.get("from") != args.start_date
            or data.get("to") != args.end_date
            or any(
                not args.start_date <= row["observedAt"] <= args.end_date
                for row in rows
            )
        ):
            raise SpatialServiceError("断面观测分页或日期范围与请求不一致")
        return {
            **data,
            "returned": len(rows),
            "has_more": offset + len(rows) < total,
            "partial": offset > 0 or len(rows) < total,
        }

    async def voyages(self, **arguments: Any) -> dict[str, Any]:
        args = _CodeArguments.model_validate(arguments)
        data = await self.client.get_duanmian_voyages(args.code)
        self._validate_scope(data, args.code)
        voyages = data.get("voyages")
        if not isinstance(voyages, list):
            raise SpatialServiceError("断面航次响应缺少 voyages")
        for item in voyages:
            if not isinstance(item, dict) or not isinstance(item.get("date"), str):
                raise SpatialServiceError("断面航次日期无效")
            try:
                _date(item["date"])
            except ValueError as exc:
                raise SpatialServiceError("断面航次日期无效") from exc
            if any(
                type(item.get(key)) is not int or item[key] < 0
                for key in ("chemistryStations", "hydrometStations")
            ):
                raise SpatialServiceError("断面航次覆盖站数无效")
        return {
            **data,
            "voyages": sorted(voyages, key=lambda item: item["date"], reverse=True),
        }

    async def analysis(self, **arguments: Any) -> ToolOutput:
        args = _AnalysisArguments.model_validate(arguments)
        payload = args.model_dump(by_alias=True, exclude_none=True)
        payload["elementType"] = args.element_type or "chemistry"
        if args.analysis == "do-profile":
            payload["element"] = "dissolved_oxygen_umol_dm3"
        if args.voyage is not None:
            sections = SECTIONS if args.analysis == "ts-scatter" else (args.section,)
            coverage_key = f"{payload['elementType']}Stations"
            matched_dates = []
            for section in sections:
                data = await self.voyages(code=section)
                matching = [
                    item
                    for item in data["voyages"]
                    if item["date"] == args.voyage
                    or (
                        len(args.voyage) == 7
                        and item["date"].startswith(args.voyage + "-")
                    )
                ]
                dates = {item["date"] for item in matching}
                if len(dates) > 1:
                    raise ValueError(
                        f"{section} 该月有多个观测日，请从 dm_duanmian_voyages 选择明确日期"
                    )
                if not matching or not any(item[coverage_key] > 0 for item in matching):
                    raise ValueError(
                        f"{section} 在指定航次没有 {payload['elementType']} 观测，不能自动出图"
                    )
                matched_dates.append(matching[0]["date"])
            if len(set(matched_dates)) > 1:
                raise ValueError("各断面的观测日不同，不能将它们拼成共同航次")
            if args.element is not None or args.analysis == "do-profile":
                payload["element"] = await self._resolve_element(
                    sections[0], payload["elementType"], payload["element"]
                )
        return ToolOutput(
            content={
                "success": True,
                "message": "断面分析展示指令已发送，等待前端执行",
                "auto_query_requested": args.voyage is not None,
                "parameters": payload,
            },
            client_events=({"action_type": ANALYSIS_ACTION_TYPE, "payload": payload},),
        )

    async def _resolve_element(
        self, section: str, element_type: str, element: str
    ) -> str:
        # Latest is used only to discover field definitions, never as the plotted voyage.
        data = await self.latest(code=section)
        definitions = data.get(f"{element_type}Fields", [])
        if not isinstance(definitions, list):
            raise SpatialServiceError("断面要素字段清单格式无效")
        candidate = ELEMENT_ALIASES.get(element, element)
        matches = {
            item["column"]
            for item in definitions
            if isinstance(item, dict)
            and isinstance(item.get("column"), str)
            and (
                item["column"] == candidate
                or item.get("label") == element
                or re.split(r"[（(]", str(item.get("label") or ""))[0].strip()
                == element
            )
        }
        if len(matches) != 1:
            raise ValueError(
                f"无法从 {element_type}Fields 唯一确认要素 {element}，请使用返回的物理列名或标签"
            )
        return matches.pop()

    @staticmethod
    def _validate_scope(data: dict, code: str) -> None:
        if (
            not isinstance(data.get("code"), str)
            or data["code"].strip().upper() != code
            or data.get("scope") != ("section" if len(code) == 2 else "station")
        ):
            raise SpatialServiceError("断面响应对象与请求 code/scope 不一致")

    @staticmethod
    def _validate_rows(data: dict) -> None:
        if not isinstance(data.get("rows"), list):
            raise SpatialServiceError("断面响应缺少 rows")
        for row in data["rows"]:
            if (
                not isinstance(row, dict)
                or row.get("dataType") not in ("chemistry", "hydromet")
                or not isinstance(row.get("stationCode"), str)
                or not row["stationCode"].strip()
                or not isinstance(row.get("observedAt"), str)
                or not isinstance(row.get("elements"), dict)
            ):
                raise SpatialServiceError("断面观测行格式无效")
            try:
                _date(row["observedAt"])
            except ValueError as exc:
                raise SpatialServiceError("断面观测日期无效") from exc
