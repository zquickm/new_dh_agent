from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.tools.executor import ToolOutput
from agents.tools.map_action import spatial_map_results
from agents.tools.spatial_query_schema import QUERY_PARAMETERS, SpatialElementQuery
from config.settings import get_settings
from tool_backends.spatial_backend import SpatialServiceClient


class IslandOverviewQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, allow_inf_nan=False)

    page: int = Field(default=1, ge=1, description="海岛列表起始页，从 1 开始；获取全部信息保持 1。")
    size: int = Field(default=20, ge=1, le=100, description="海岛每次接口请求的条数，上限 100；默认自动翻页，不限制合并总数。")
    keyword: Optional[str] = Field(default=None, min_length=1, description="标准名称或现用名称关键字，模糊匹配。")
    province: Optional[str] = Field(default=None, min_length=1, description="省全称，精确匹配，如浙江省。")
    city: Optional[str] = Field(default=None, min_length=1, description="市全称，精确匹配，如舟山市。")
    islandClass: Optional[str] = Field(default=None, min_length=1, description="海岛分类，模糊匹配，如无居民海岛。")
    minLng: Optional[float] = Field(default=None, ge=-180, le=180, description="矩形最小经度，须同时传四个坐标。")
    minLat: Optional[float] = Field(default=None, ge=-90, le=90, description="矩形最小纬度，须同时传四个坐标。")
    maxLng: Optional[float] = Field(default=None, ge=-180, le=180, description="矩形最大经度，须同时传四个坐标。")
    maxLat: Optional[float] = Field(default=None, ge=-90, le=90, description="矩形最大纬度，须同时传四个坐标。")

    @model_validator(mode="after")
    def validate_bounds(self) -> "IslandOverviewQuery":
        bounds = (self.minLng, self.minLat, self.maxLng, self.maxLat)
        if any(value is not None for value in bounds):
            if any(value is None for value in bounds):
                raise ValueError("海岛矩形范围须同时传 minLng/minLat/maxLng/maxLat")
            if self.minLng > self.maxLng or self.minLat > self.maxLat:
                raise ValueError("海岛矩形最小坐标不能大于最大坐标")
        return self


class DuanmianObservationsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    code: Optional[str] = Field(default=None, min_length=1, description="断面编号（如 E1/EK）或测点编码（如 E1001）；省略时查询 E1/E2/E3 全部断面测点。")
    from_: str = Field(alias="from", pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", description="dm_duanmian_observations 必填的起始日期 yyyy-MM-dd（含）。")
    to: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", description="dm_duanmian_observations 必填的结束日期 yyyy-MM-dd（含）；时间跨度不超过 3 个月，由服务校验。")
    page: int = Field(default=1, ge=1)
    size: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_dates(self) -> "DuanmianObservationsQuery":
        start = datetime.strptime(self.from_, "%Y-%m-%d")
        end = datetime.strptime(self.to, "%Y-%m-%d")
        if start > end:
            raise ValueError("from 不能晚于 to")
        return self


TOOL_NAME = "spatial_query_service"
TOOL_DESCRIPTION = (
    '工具名固定为 spatial_query_service；所有查询操作均通过 operation 参数选择，不是独立工具。'
    '例如目录查询应调用 spatial_query_service(operation="list_spatial_elements")，不得调用 list_spatial_elements 工具。'
    "查询已注册空间要素的目录、字段定义、属性和几何，以及观测站在线状态、最新观测和时序数据。"
    "海岛一览数量、按省/分类分布及面积排序列表必须用 island_overview；"
    "断面一览/东海区断面情况概览、断面数、测点数、要素清单、数据量及最近航次必须用 dm_duanmian_overview（无参数）。"
    "这两类统计与前端使用同一接口，直接采用返回值，不从文档、历史回答、分页列表或数据库重算；失败时说明未取得统计。"
    "断面/测点最新观测用 dm_duanmian_latest，航次列表用 dm_duanmian_voyages，两者必填 code。"
    "不超过 3 个月的断面/测点观测明细用 dm_duanmian_observations，必填 from/to（yyyy-MM-dd）；"
    "code 可省略，省略表示 E1/E2/E3 全部断面测点，返回 scope=all、code=null；此时超过 50000 条会报错，须收窄时间或指定断面/测点，不截断或抽样。"
    "dm_duanmian_observations 每次仅返回一页，page 默认 1、size 默认 100 且最多 1000；total 是总条数，rows 是当前页，不能把单页当成全部结果。"
    "断面接口保留原始 elements 字段和 chemistryFields/hydrometFields 标签映射，不用空间属性代替观测数据。"
    "通用查询先取得 list_spatial_elements 返回的 dataKey，再调用详情或查询；已有目录可复用，类别未注册时刷新目录核对。"
    "使用 query.fields、conditions 或 orderBy 前，缺少当前类别字段定义时须先调用 get_spatial_element_detail，"
    "收到定义后再查询；字段名、用途和运算符以定义为准，原样保留大小写及下划线，不自行转换命名。"
    "query_spatial_element 和 island_overview 默认在一次工具调用内自动拉取并合并全部后续页，fetch_all=false 才只取一页；"
    "分页始终保留类别、空间范围、过滤和排序条件。pagination.complete=false 表示未取全，必须说明 warning。"
    "需要几何时设置 includeGeometry=true，result_ref 引用本次合并取得的全部要素。"
    "返回 result_ref 时，地图展示直接调用 map_action(action_type=GEOJSON_ADD, payload={result_ref:该引用})，不要同时传 geojson，无需重新组装 features 或填写 layer_id。"
    "仅返回数据，不发送地图动作；地图展示使用 map_action。"
)
TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": [
                "station_status",
                "observation_summary",
                "observation_trend",
                "observation_range",
                "list_spatial_elements",
                "get_spatial_element_detail",
                "query_spatial_element",
                "island_overview",
                "dm_duanmian_overview",
                "dm_duanmian_latest",
                "dm_duanmian_observations",
                "dm_duanmian_voyages",
            ],
            "description": "通用空间或观测数据查询操作；枚举值不是工具名。目录查询传 operation=list_spatial_elements，无需其他参数。",
        },
        "dataKey": {
            "type": "string",
            "minLength": 1,
            "description": "get_spatial_element_detail 必填；原样使用 list_spatial_elements 返回的 dataKey，不得传地图 layer_id。",
        },
        "query": {
            **{key: value for key, value in QUERY_PARAMETERS.items() if key != "$defs"},
            "description": (
                "query_spatial_element 必填；对应 v2 /query 请求体，"
                "要素标识和可查询字段以空间目录及字段定义为准。"
            ),
        },
        "fetch_all": {
            "type": "boolean",
            "default": True,
            "description": (
                "仅 query_spatial_element 和 island_overview 使用；默认自动拉取并合并从指定 offset/page 开始的全部后续页。"
                "通常从 offset=0/page=1 开始，获取全部匹配信息；仅用户明确只要某一页时设为 false。"
                "limit/size 只是每次接口请求的批量大小，不限制合并总数；此参数不发送给空间服务。"
            ),
        },
        "tznos": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 50,
            "description": "station_status 的站号列表。",
        },
        "tzno": {
            "type": "string",
            "description": "观测数据查询的目标站号。",
        },
        "element": {
            "type": "string",
            "description": "observation_trend 必填的观测要素 key。",
        },
        "hours": {
            "type": "integer",
            "minimum": 1,
            "default": 24,
            "description": "observation_trend 向前查询的小时数。",
        },
        "data_type": {
            "type": "string",
            "enum": [
                "wave",
                "watertemp",
                "wind",
                "tidal",
                "salinity",
                "seacurrent",
                "bfdata",
            ],
            "description": (
                "observation_range 必填的数据类型：wave 波浪、watertemp 水温、"
                "wind 风、tidal 潮位、salinity 盐度、seacurrent 海流、bfdata 浮标数据。"
            ),
        },
        "start_time": {
            "type": "string",
            "description": "区间起始时间 yyyy-MM-dd HH:mm:ss",
        },
        "end_time": {
            "type": "string",
            "description": "区间结束时间 yyyy-MM-dd HH:mm:ss",
        },
        "order_by": {
            "type": "string",
            "enum": ["asc", "desc"],
            "default": "asc",
            "description": "观测记录时间排序：asc 从早到晚，desc 从晚到早。",
        },
        "max_records": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5000,
            "default": 2000,
            "description": "observation_range 最多返回的观测记录数。",
        },
        **DuanmianObservationsQuery.model_json_schema()["properties"],
        **IslandOverviewQuery.model_json_schema()["properties"],
        "code": {
            "type": "string",
            "minLength": 1,
            "description": "断面编号（如 E1/EK）或测点编码（如 E1001）；dm_duanmian_latest、dm_duanmian_voyages 必填；dm_duanmian_observations 可省略，表示 E1/E2/E3 全部断面测点。",
        },
        "page": {
            "type": "integer", "minimum": 1, "default": 1,
            "description": "island_overview 的起始页，或 dm_duanmian_observations 的单页页码，从 1 开始。",
        },
        "size": {
            "type": "integer", "minimum": 1, "maximum": 1000,
            "description": "每页条数；island_overview 默认 20、最多 100；dm_duanmian_observations 默认 100、最多 1000。",
        },
    },
    "required": ["operation"],
    "$defs": QUERY_PARAMETERS["$defs"],
    "allOf": [
        {
            "if": {
                "properties": {"operation": {"const": "island_overview"}},
                "required": ["operation"],
            },
            "then": {"properties": {"size": {"maximum": 100, "default": 20}}},
        },
        {
            "if": {
                "properties": {"operation": {"const": "dm_duanmian_latest"}},
                "required": ["operation"],
            },
            "then": {"required": ["code"]},
        },
        {
            "if": {
                "properties": {"operation": {"const": "dm_duanmian_voyages"}},
                "required": ["operation"],
            },
            "then": {"required": ["code"]},
        },
        {
            "if": {
                "properties": {"operation": {"const": "dm_duanmian_observations"}},
                "required": ["operation"],
            },
            "then": {
                "required": ["from", "to"],
                "properties": {"size": {"default": 100}},
            },
        },
        {
            "if": {
                "properties": {"operation": {"const": "get_spatial_element_detail"}},
                "required": ["operation"],
            },
            "then": {"required": ["dataKey"]},
        },
        {
            "if": {
                "properties": {"operation": {"const": "query_spatial_element"}},
                "required": ["operation"],
            },
            "then": {"required": ["query"]},
        },
        {
            "if": {
                "properties": {"operation": {"const": "station_status"}},
                "required": ["operation"],
            },
            "then": {"required": ["tznos"]},
        },
        {
            "if": {
                "properties": {
                    "operation": {"const": "observation_summary"}
                },
                "required": ["operation"],
            },
            "then": {"required": ["tzno"]},
        },
        {
            "if": {
                "properties": {"operation": {"const": "observation_trend"}},
                "required": ["operation"],
            },
            "then": {"required": ["tzno", "element"]},
        },
        {
            "if": {
                "properties": {"operation": {"const": "observation_range"}},
                "required": ["operation"],
            },
            "then": {
                "required": [
                    "tzno",
                    "data_type",
                    "start_time",
                    "end_time",
                ]
            },
        },
    ],
    "additionalProperties": False,
}


class SpatialQueryServiceTool:
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

    async def execute(self, operation: str, **arguments: Any) -> ToolOutput:
        if "render_map" in arguments:
            raise ValueError("render_map 已移除；本工具仅查询数据，地图展示请调用 map_action")
        allowed_arguments = {
            "list_spatial_elements": set(),
            "get_spatial_element_detail": {"dataKey"},
            "query_spatial_element": {"query", "fetch_all"},
            "island_overview": set(IslandOverviewQuery.model_fields) | {"fetch_all"},
            "dm_duanmian_overview": set(),
            "dm_duanmian_latest": {"code"},
            "dm_duanmian_observations": {"code", "from", "to", "page", "size"},
            "dm_duanmian_voyages": {"code"},
            "station_status": {"tznos"},
            "observation_summary": {"tzno"},
            "observation_trend": {"tzno", "element", "hours"},
            "observation_range": {
                "tzno", "data_type", "start_time", "end_time", "order_by", "max_records",
            },
        }
        if operation not in allowed_arguments:
            raise ValueError(f"不支持的查询操作：{operation}")
        allowed = allowed_arguments[operation]
        if arguments.keys() - allowed:
            raise ValueError(f"{operation} 仅接受参数：{', '.join(sorted(allowed)) or '无'}")
        fetch_all = arguments.pop("fetch_all", True)
        if not isinstance(fetch_all, bool):
            raise ValueError("fetch_all 必须为布尔值")
        if operation == "list_spatial_elements":
            data = {"elements": await self.client.list_spatial_elements()}
        elif operation == "island_overview":
            params = IslandOverviewQuery.model_validate(arguments).model_dump(
                exclude_unset=True, exclude_none=True,
            )
            data = (
                await self._fetch_all_pages(operation, params) if fetch_all
                else await self.client.get_island_overview(**params)
            )
        elif operation == "dm_duanmian_overview":
            data = await self.client.get_duanmian_overview()
        elif operation == "dm_duanmian_latest":
            data = await self.client.get_duanmian_latest(self._required_text(arguments.get("code"), "code"))
        elif operation == "dm_duanmian_voyages":
            data = await self.client.get_duanmian_voyages(self._required_text(arguments.get("code"), "code"))
        elif operation == "dm_duanmian_observations":
            params = DuanmianObservationsQuery.model_validate(arguments).model_dump(
                by_alias=True, exclude_unset=True, exclude_none=True,
            )
            data = await self.client.get_duanmian_observations(**params)
        elif operation == "get_spatial_element_detail":
            data_key = self._required_text(arguments.get("dataKey"), "dataKey")
            data = await self.client.get_spatial_element_detail(data_key)
        elif operation == "query_spatial_element":
            query = SpatialElementQuery.model_validate(arguments.get("query"))
            payload = query.model_dump(exclude_unset=True, exclude_none=True)
            json.dumps(payload, allow_nan=False)
            data = (
                await self._fetch_all_pages(operation, payload) if fetch_all
                else await self.client.query_spatial_element(payload)
            )
            results = spatial_map_results.get()
            if query.includeGeometry and results is not None:
                result_ref = "spatial-" + uuid4().hex
                results[result_ref] = {
                    "geojson": {"type": "FeatureCollection", "features": data["features"]},
                    "layer_id": "observation-stations" if query.element == "obs_station" else f"spatial-element:{query.element}",
                    "replace": query.offset == 0,
                    "fit_bounds": False,
                }
                data = {**data, "result_ref": result_ref}
        elif operation == "station_status":
            tznos = self._string_list(arguments.get("tznos"), "tznos", maximum=50)
            data = await self.client.query_station_status(tznos)
        elif operation == "observation_summary":
            tzno = self._required_text(arguments.get("tzno"), "tzno")
            data = await self.client.get_observation_summary(tzno)
        elif operation == "observation_trend":
            data = await self.client.get_element_trend(
                self._required_text(arguments.get("tzno"), "tzno"),
                self._required_text(arguments.get("element"), "element"),
                self._positive_int(arguments.get("hours", 24), "hours"),
            )
        elif operation == "observation_range":
            data = await self._observation_range(arguments)
        # Keep the reference and completion status ahead of potentially large geometry.
        return ToolOutput(content={
            "operation": operation,
            **{key: data[key] for key in ("result_ref", "pagination", "total", "returned") if key in data},
            **data,
        })

    async def _fetch_all_pages(self, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
        spatial = operation == "query_spatial_element"
        items_key = "features" if spatial else "items"
        params = dict(parameters)
        page_size = params.get("limit" if spatial else "size", 20)
        page_number = params.get("page", 1)
        start = params.get("offset", 0) if spatial else (page_number - 1) * page_size
        position = start
        first = None
        items: list[Any] = []
        seen_pages: set[str] = set()
        pages = 0
        warning = ""
        while True:
            try:
                data = (
                    await self.client.query_spatial_element(params) if spatial
                    else await self.client.get_island_overview(**params)
                )
                batch = data.get(items_key)
                total = data.get("total")
                if not isinstance(batch, list) or type(total) is not int or total < 0:
                    raise ValueError(f"空间服务分页结果无效：需要 {items_key} 数组和非负整数 total")
                if spatial and (type(data.get("returned", len(batch))) is not int or data.get("returned", len(batch)) != len(batch)):
                    raise ValueError("空间服务分页计数无效：returned 与 features 数量不一致")
                if first is not None and total != first["total"]:
                    raise ValueError("分页期间 total 发生变化，请重新查询以核对完整结果")
                if not spatial:
                    actual_size = data.get("size", page_size)
                    if type(actual_size) is not int or actual_size <= 0 or type(data.get("page", page_number)) is not int or data.get("page", page_number) != page_number:
                        raise ValueError("海岛服务分页页码或每页条数无效")
                    if first is None:
                        page_size = actual_size
                        start = position = (page_number - 1) * page_size
                    elif actual_size != page_size:
                        raise ValueError("分页期间 size 发生变化，无法保证结果完整")
                if (batch and position + len(batch) > total) or (not batch and position < total):
                    raise ValueError("空间服务分页计数无效：结果超出 total 或提前返回空页")
                fingerprint = json.dumps(batch, sort_keys=True, ensure_ascii=False, allow_nan=False)
                if batch and fingerprint in seen_pages:
                    raise ValueError("空间服务重复返回已取得的页面，已停止翻页")
                if not spatial and len(batch) < page_size and position + len(batch) < total:
                    raise ValueError("海岛服务提前返回不足一页的数据，无法确认后续页范围")
            except Exception as exc:
                if first is None:
                    raise
                warning = str(exc).strip() or type(exc).__name__
                break
            if first is None:
                first = data
            items.extend(batch)
            seen_pages.add(fingerprint)
            pages += 1
            position += len(batch)
            if position >= total:
                break
            if spatial:
                params = {**params, "offset": position}
            else:
                page_number += 1
                params = {**params, "page": page_number, "size": page_size}
        pagination = {
            "complete": not warning and start == 0 and position == first["total"],
            "pages": pages, "fetched": len(items), "start_offset": start,
            "next_offset" if spatial else "next_page": (
                (position if spatial else page_number) if position < first["total"] else None
            ),
        }
        if warning or start:
            pagination["warning"] = warning or f"本次从第 {start + 1} 条开始，未包含此前记录；查询全部信息请从 offset=0/page=1 开始"
        result = {**first, items_key: items, "pagination": pagination}
        if spatial:
            result["returned"] = len(items)
        return result

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def _observation_range(self, arguments: dict[str, Any]) -> dict[str, Any]:
        start_time = self._required_time(arguments.get("start_time"), "start_time")
        end_time = self._required_time(arguments.get("end_time"), "end_time")
        if start_time > end_time:
            raise ValueError("start_time 不能晚于 end_time")
        data_type = self._required_text(arguments.get("data_type"), "data_type")
        allowed_types = {
            "wave",
            "watertemp",
            "wind",
            "tidal",
            "salinity",
            "seacurrent",
            "bfdata",
        }
        if data_type not in allowed_types:
            raise ValueError("data_type 不受支持")
        order_by = str(arguments.get("order_by", "asc")).lower()
        if order_by not in {"asc", "desc"}:
            raise ValueError("order_by 只能为 asc 或 desc")
        max_records = self._positive_int(
            arguments.get("max_records", 2000), "max_records"
        )
        if max_records > 5000:
            raise ValueError("max_records 不能超过 5000")
        return await self.client.get_observation_range(
            tzno=self._required_text(arguments.get("tzno"), "tzno"),
            data_type=data_type,
            start_time=start_time.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=end_time.strftime("%Y-%m-%d %H:%M:%S"),
            order_by=order_by,
            max_records=max_records,
        )

    @staticmethod
    def _required_text(value: Any, name: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{name} 不能为空")
        return text

    @staticmethod
    def _string_list(value: Any, name: str, maximum: int = 100) -> list[str]:
        if not isinstance(value, list):
            raise ValueError(f"{name} 必须为字符串数组")
        items = [str(item).strip() for item in value if str(item).strip()]
        if not items:
            raise ValueError(f"{name} 不能为空")
        if len(items) > maximum:
            raise ValueError(f"{name} 最多允许 {maximum} 项")
        return items

    @staticmethod
    def _positive_int(value: Any, name: str) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须为整数") from exc
        if result <= 0:
            raise ValueError(f"{name} 必须大于 0")
        return result

    @staticmethod
    def _required_time(value: Any, name: str) -> datetime:
        text = SpatialQueryServiceTool._required_text(value, name)
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise ValueError(f"{name} 格式必须为 yyyy-MM-dd HH:mm:ss") from exc
