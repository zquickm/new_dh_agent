from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from agents.tools.executor import ToolOutput
from agents.tools.map_action import spatial_map_results
from agents.tools.spatial_query_schema import QUERY_PARAMETERS, SpatialElementQuery
from config.settings import get_settings
from tool_backends.spatial_backend import SpatialServiceClient


TOOL_NAME = "spatial_query_service"
TOOL_DESCRIPTION = (
    "查询已注册空间要素的目录、字段定义、属性和几何，以及观测站在线状态、最新观测和时序数据。"
    "使用 query.fields、conditions 或 orderBy 前，缺少当前类别字段定义时须先调用 get_spatial_element_detail，"
    "收到定义后再查询；字段名、用途和运算符以定义为准，不猜测。"
    "通用查询按 limit/offset 返回一页 GeoJSON；需要几何时设置 includeGeometry=true。"
    "返回 result_ref 时，地图展示直接调用 map_action(action_type=GEOJSON_ADD, payload={result_ref:该引用})，无需重新组装 features。"
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
            ],
            "description": "通用空间或观测数据查询操作；list_spatial_elements 无需其他参数。",
        },
        "dataKey": {
            "type": "string",
            "minLength": 1,
            "description": "get_spatial_element_detail 必填的目录要素标识。",
        },
        "query": {
            **{key: value for key, value in QUERY_PARAMETERS.items() if key != "$defs"},
            "description": (
                "query_spatial_element 必填；对应 v2 /query 请求体，"
                "要素标识和可查询字段以空间目录及字段定义为准。"
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
    },
    "required": ["operation"],
    "$defs": QUERY_PARAMETERS["$defs"],
    "allOf": [
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
            "query_spatial_element": {"query"},
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
        if operation == "list_spatial_elements":
            data = {"elements": await self.client.list_spatial_elements()}
        elif operation == "get_spatial_element_detail":
            data_key = self._required_text(arguments.get("dataKey"), "dataKey")
            data = await self.client.get_spatial_element_detail(data_key)
        elif operation == "query_spatial_element":
            query = SpatialElementQuery.model_validate(arguments.get("query"))
            payload = query.model_dump(exclude_unset=True, exclude_none=True)
            json.dumps(payload, allow_nan=False)
            data = await self.client.query_spatial_element(payload)
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
        return ToolOutput(content={"operation": operation, **data})

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
