from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from agents.tools.executor import ToolOutput
from config.settings import get_settings
from tool_backends.spatial_backend import SpatialServiceClient


TOOL_NAME = "observation_service"
TOOL_DESCRIPTION = (
    "查询海洋观测站的空间分布、类型、详情、在线状态、最新观测、要素趋势或指定时间区间观测数据。"
    "需要真实观测站或观测要素数据时使用；search_stations 的结果会自动联动地图。"
    "只能使用 operation 枚举中列出的操作，不得编造操作名。当前不支持跨多个站点批量比较或排名"
    "最新观测值，不能仅凭站点位置判断哪里浪最大；能力不足时应直接说明。"
)
TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": [
                "list_station_types",
                "search_stations",
                "station_status",
                "station_detail",
                "observation_summary",
                "observation_trend",
                "observation_range",
            ],
            "description": "要执行的观测服务操作",
        },
        "query_type": {
            "type": "string",
            "enum": ["bbox", "circle", "polygon"],
            "description": "search_stations 必填",
        },
        "bbox": {
            "type": "array",
            "items": {"type": "number"},
            "minItems": 4,
            "maxItems": 4,
            "description": "bbox 查询范围 [minLon,minLat,maxLon,maxLat]",
        },
        "center": {
            "type": "array",
            "items": {"type": "number"},
            "minItems": 2,
            "maxItems": 2,
            "description": "circle 圆心 [lon,lat]",
        },
        "radius": {
            "type": "number",
            "exclusiveMinimum": 0,
            "description": "circle 半径，单位米",
        },
        "polygon": {
            "type": "array",
            "items": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 2,
                "maxItems": 2,
            },
            "minItems": 3,
        },
        "station_types": {"type": "array", "items": {"type": "string"}},
        "tznos": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 50,
            "description": "station_status 必填",
        },
        "tzno": {
            "type": "string",
            "description": "详情、最新观测、趋势和区间查询必填",
        },
        "element": {"type": "string", "description": "趋势查询的要素 key"},
        "hours": {"type": "integer", "minimum": 1, "default": 24},
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
            "description": "区间查询的数据类型",
        },
        "start_time": {
            "type": "string",
            "description": "区间起始时间 yyyy-MM-dd HH:mm:ss",
        },
        "end_time": {
            "type": "string",
            "description": "区间结束时间 yyyy-MM-dd HH:mm:ss",
        },
        "order_by": {"type": "string", "enum": ["asc", "desc"], "default": "asc"},
        "max_records": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5000,
            "default": 2000,
        },
    },
    "required": ["operation"],
    "additionalProperties": False,
}


class ObservationServiceTool:
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
            )
        return self._client

    async def execute(self, operation: str, **arguments: Any) -> ToolOutput:
        if operation == "list_station_types":
            data = await self.client.list_station_types()
        elif operation == "search_stations":
            data = await self.client.query_stations(
                self._station_query_payload(arguments)
            )
            return ToolOutput(
                content={"operation": operation, **data},
                client_events=tuple(self._station_map_actions(data)),
            )
        elif operation == "station_status":
            data = await self.client.query_station_status(
                self._string_list(arguments.get("tznos"), "tznos", maximum=50)
            )
        elif operation == "station_detail":
            data = await self.client.get_station_detail(
                self._required_text(arguments.get("tzno"), "tzno")
            )
        elif operation == "observation_summary":
            data = await self.client.get_observation_summary(
                self._required_text(arguments.get("tzno"), "tzno")
            )
        elif operation == "observation_trend":
            data = await self.client.get_element_trend(
                self._required_text(arguments.get("tzno"), "tzno"),
                self._required_text(arguments.get("element"), "element"),
                self._positive_int(arguments.get("hours", 24), "hours"),
            )
        elif operation == "observation_range":
            data = await self._observation_range(arguments)
        else:
            raise ValueError(f"不支持的观测操作：{operation}")
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

    def _station_query_payload(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query_type = self._required_text(arguments.get("query_type"), "query_type")
        payload: dict[str, Any] = {"query_type": query_type}
        if query_type == "bbox":
            bbox = self._coordinates(arguments.get("bbox"), "bbox", 4)
            if bbox[0] > bbox[2] or bbox[1] > bbox[3]:
                raise ValueError("bbox 最小坐标不能大于最大坐标")
            payload["bbox"] = bbox
        elif query_type == "circle":
            payload["center"] = self._coordinates(
                arguments.get("center"), "center", 2
            )
            radius = float(arguments.get("radius") or 0)
            if radius <= 0:
                raise ValueError("radius 必须大于 0")
            payload["radius"] = radius
        elif query_type == "polygon":
            polygon = arguments.get("polygon")
            if not isinstance(polygon, list) or len(polygon) < 3:
                raise ValueError("polygon 至少需要三个坐标点")
            payload["polygon"] = [
                self._coordinates(point, "polygon point", 2) for point in polygon
            ]
        else:
            raise ValueError("query_type 只能为 bbox、circle 或 polygon")
        if arguments.get("station_types") is not None:
            payload["station_types"] = self._string_list(
                arguments["station_types"], "station_types"
            )
        return payload

    @staticmethod
    def _station_map_actions(data: dict[str, Any]) -> list[dict[str, Any]]:
        raw_stations = data.get("stations")
        if not isinstance(raw_stations, list):
            return []
        markers = []
        for station in raw_stations:
            if not isinstance(station, dict):
                continue
            tzno = str(station.get("tzno") or "").strip()
            name = str(station.get("name") or "").strip()
            lon = station.get("lon")
            lat = station.get("lat")
            if (
                not tzno
                or not name
                or isinstance(lon, bool)
                or isinstance(lat, bool)
                or not isinstance(lon, (int, float))
                or not isinstance(lat, (int, float))
            ):
                continue
            marker = {
                "id": tzno,
                "name": name,
                "coordinates": [float(lon), float(lat)],
            }
            station_type = str(station.get("type") or "").strip()
            if station_type:
                marker["type"] = station_type
            markers.append(marker)

        actions = [
            {
                "action_type": "CLEAR",
                "payload": {"layer_ids": ["observation-stations"]},
            },
        ]
        if markers:
            actions.append(
                {
                    "action_type": "MARKER_ADD",
                    "payload": {
                        "layer_id": "observation-stations",
                        "markers": markers,
                        "fit_bounds": True,
                    },
                }
            )
        return actions

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
    def _coordinates(value: Any, name: str, length: int) -> list[float]:
        if not isinstance(value, list) or len(value) != length:
            raise ValueError(f"{name} 必须包含 {length} 个数值")
        try:
            result = [float(item) for item in value]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须为数值数组") from exc
        return result

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
        text = ObservationServiceTool._required_text(value, name)
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise ValueError(f"{name} 格式必须为 yyyy-MM-dd HH:mm:ss") from exc
