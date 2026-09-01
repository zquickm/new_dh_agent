from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from agents.tools.executor import ToolOutput
from config.settings import get_settings
from tool_backends.spatial_backend import SpatialServiceClient


logger = logging.getLogger(__name__)

_POPUP_LABELS = {
    "watertemp": "水温",
    "salinity": "盐度",
    "wave_height": "波高",
    "wind_speed": "风速",
    "tidal": "潮位",
    "current_speed": "流速",
}
_GLOBAL_STATION_BBOX = [-180.0, -90.0, 180.0, 90.0]
_STATION_TYPE_ALIASES = {
    "buoy": "buoy",
    "浮标": "buoy",
    "浮标站": "buoy",
    "浮标观测站": "buoy",
    "radar": "radar",
    "radar_station": "radar",
    "雷达": "radar",
    "雷达站": "radar",
    "雷达观测站": "radar",
}

TOOL_NAME = "observation_service"
TOOL_DESCRIPTION = (
    "查询海洋观测站的空间分布、类型、详情、在线状态、最新观测、要素趋势或指定时间区间观测数据。"
    "这是地图选区观测站和明确站名的站点数据的唯一业务工具；用户继续询问前一轮返回的某个站点时，"
    "无论问题涉及水温、盐度、风、浪、潮位、状态、最新值还是趋势，都必须继续使用本工具，"
    "不得改用 database_query。站点查询和单站分析结果会自动清除旧站点图层并联动地图。"
    "查询明确指定的站点时，如需先用 search_stations 解析站号，应传 station_name；"
    "任何仅为解析站号或查找目标地点附近站点的检索都应设置 render_map=false，"
    "得到站号后再执行单站查询；不得把内部查找的候选站点全部展示到地图。"
    "search_stations 返回的 total、type_counts 和 stations 已按同一结果集完成类型过滤与去重；"
    "指定 station_types 后不得取消过滤重新查询全部站点。"
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
            "description": (
                "要执行的观测服务操作。参数要求：search_stations 需要 query_type "
                "及对应几何参数；station_status 需要 tznos；station_detail 和 "
                "observation_summary 需要 tzno；observation_trend 需要 tzno、element；"
                "observation_range 需要 tzno、data_type、start_time、end_time。"
            ),
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
        "station_types": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "站点类型编码；浮标、浮标站使用 buoy，雷达、雷达站使用 radar"
            ),
        },
        "station_name": {
            "type": "string",
            "description": "search_stations 可选；按站名收窄候选结果",
        },
        "render_map": {
            "type": "boolean",
            "default": True,
            "description": "search_stations 是否展示结果；仅为解析站号而检索时设为 false",
        },
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
    "allOf": [
        {
            "if": {
                "properties": {"operation": {"const": "search_stations"}},
                "required": ["operation"],
            },
            "then": {"required": ["query_type"]},
        },
        {
            "if": {
                "properties": {
                    "operation": {"const": "search_stations"},
                    "query_type": {"const": "bbox"},
                },
                "required": ["operation", "query_type"],
            },
            "then": {"required": ["bbox"]},
        },
        {
            "if": {
                "properties": {
                    "operation": {"const": "search_stations"},
                    "query_type": {"const": "circle"},
                },
                "required": ["operation", "query_type"],
            },
            "then": {"required": ["center", "radius"]},
        },
        {
            "if": {
                "properties": {
                    "operation": {"const": "search_stations"},
                    "query_type": {"const": "polygon"},
                },
                "required": ["operation", "query_type"],
            },
            "then": {"required": ["polygon"]},
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
                "properties": {"operation": {"const": "station_detail"}},
                "required": ["operation"],
            },
            "then": {"required": ["tzno"]},
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
                max_retries=settings.max_retries,
                retry_backoff=settings.retry_backoff,
            )
        return self._client

    async def execute(self, operation: str, **arguments: Any) -> ToolOutput:
        client_events: tuple[dict[str, Any], ...] = ()
        if operation == "list_station_types":
            data = await self.client.list_station_types()
        elif operation == "search_stations":
            station_name = arguments.get("station_name")
            if station_name is not None:
                station_name = self._required_text(station_name, "station_name")
            render_map = self._optional_bool(
                arguments.get("render_map"),
                "render_map",
                default=True,
            )
            query_payload = self._station_query_payload(arguments)
            requested_station_types = query_payload.pop("station_types", None)
            data = await self.client.query_stations(query_payload)
            data = self._filter_stations_by_name(
                data,
                station_name,
            )
            data = self._normalize_station_search_result(
                data,
                requested_station_types,
            )
            return ToolOutput(
                content={"operation": operation, **data},
                client_events=(
                    tuple(self._station_map_actions(data)) if render_map else ()
                ),
            )
        elif operation == "station_status":
            tznos = self._string_list(
                arguments.get("tznos"), "tznos", maximum=50
            )
            data = await self.client.query_station_status(tznos)
            if len(tznos) == 1:
                status_list = data.get("statusList")
                status_data = next(
                    (
                        item
                        for item in status_list
                        if isinstance(item, dict) and item.get("tzno") == tznos[0]
                    ),
                    None,
                ) if isinstance(status_list, list) else None
                client_events = tuple(
                    await self._single_station_map_actions(
                        tznos[0],
                        popup_data=status_data,
                    )
                )
            else:
                client_events = tuple(
                    self._station_map_actions({"stations": data.get("statusList")})
                )
        elif operation == "station_detail":
            tzno = self._required_text(arguments.get("tzno"), "tzno")
            data = await self.client.get_station_detail(tzno)
            client_events = tuple(
                self._station_map_actions(
                    {"stations": [data]},
                    popup_data=data,
                    focus_single=True,
                )
            )
        elif operation == "observation_summary":
            tzno = self._required_text(arguments.get("tzno"), "tzno")
            data = await self.client.get_observation_summary(tzno)
            client_events = tuple(
                await self._single_station_map_actions(tzno, popup_data=data)
            )
        elif operation == "observation_trend":
            tzno = self._required_text(arguments.get("tzno"), "tzno")
            data = await self.client.get_element_trend(
                tzno,
                self._required_text(arguments.get("element"), "element"),
                self._positive_int(arguments.get("hours", 24), "hours"),
            )
            client_events = tuple(
                await self._single_station_map_actions(tzno, popup_data=data)
            )
        elif operation == "observation_range":
            tzno = self._required_text(arguments.get("tzno"), "tzno")
            data = await self._observation_range(arguments)
            client_events = tuple(
                await self._single_station_map_actions(tzno, popup_data=data)
            )
        else:
            raise ValueError(f"不支持的观测操作：{operation}")
        return ToolOutput(
            content={"operation": operation, **data},
            client_events=client_events,
        )

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
        if arguments.get("station_name") is not None:
            arguments = {
                **arguments,
                "query_type": "bbox",
                "bbox": list(_GLOBAL_STATION_BBOX),
            }
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
            payload["station_types"] = self._normalize_station_types(
                arguments["station_types"]
            )
        return payload

    @classmethod
    def _normalize_station_types(cls, value: Any) -> list[str]:
        normalized: list[str] = []
        for station_type in cls._string_list(value, "station_types"):
            key = "".join(station_type.split()).casefold()
            canonical = _STATION_TYPE_ALIASES.get(key, station_type)
            if canonical not in normalized:
                normalized.append(canonical)
        return normalized

    @classmethod
    def _normalize_station_search_result(
        cls,
        data: dict[str, Any],
        station_types: Any,
    ) -> dict[str, Any]:
        requested_types = (
            set(cls._normalize_station_types(station_types))
            if station_types is not None
            else None
        )
        raw_stations = data.get("stations")
        if not isinstance(raw_stations, list):
            raw_stations = []

        stations: list[dict[str, Any]] = []
        seen_station_ids: set[str] = set()
        type_counts: dict[str, int] = {}
        for station in raw_stations:
            if not isinstance(station, dict):
                continue
            marker_data = cls._station_marker_data(station)
            if marker_data is None:
                continue
            station_id = marker_data[0].casefold()
            if station_id in seen_station_ids:
                continue
            station_type = cls._canonical_station_type(station)
            if requested_types is not None and station_type not in requested_types:
                continue
            seen_station_ids.add(station_id)
            stations.append(station)
            type_key = station_type or "unknown"
            type_counts[type_key] = type_counts.get(type_key, 0) + 1

        return {
            **data,
            "total": len(stations),
            "type_counts": type_counts,
            "stations": stations,
        }

    @staticmethod
    def _canonical_station_type(station: dict[str, Any]) -> str:
        name = "".join(str(station.get("name") or "").split()).casefold()
        if "雷达" in name:
            return "radar"
        if "浮标" in name:
            return "buoy"
        raw_type = str(
            station.get("type") or station.get("station_type") or ""
        ).strip()
        key = "".join(raw_type.split()).casefold()
        return _STATION_TYPE_ALIASES.get(key, raw_type)

    @staticmethod
    def _station_marker_data(
        station: dict[str, Any],
    ) -> Optional[tuple[str, str, float, float]]:
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
            return None
        return tzno, name, float(lon), float(lat)

    @classmethod
    def _filter_stations_by_name(
        cls,
        data: dict[str, Any],
        station_name: Any,
    ) -> dict[str, Any]:
        if station_name is None:
            return data
        keyword = cls._station_name_key(
            cls._required_text(station_name, "station_name")
        )
        raw_stations = data.get("stations")
        if not isinstance(raw_stations, list):
            raw_stations = []
        stations = [
            station
            for station in raw_stations
            if isinstance(station, dict)
            and keyword == cls._station_name_key(station.get("name"))
        ]
        return {**data, "total": len(stations), "stations": stations}

    @staticmethod
    def _station_name_key(value: Any) -> str:
        text = "".join(str(value or "").split()).casefold()
        for suffix in (
            "浮标观测站",
            "观测平台",
            "平台站",
            "观测站",
            "浮标站",
            "平台",
            "浮标",
            "站",
        ):
            if text.endswith(suffix):
                return text[: -len(suffix)]
        return text

    async def _single_station_map_actions(
        self,
        tzno: str,
        *,
        popup_data: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        try:
            station = await self.client.get_station_detail(tzno)
        except Exception:
            logger.warning(
                "Failed to load station detail for map marker: tzno=%s",
                tzno,
                exc_info=True,
            )
            station = None
        return self._station_map_actions(
            {"stations": [station]},
            popup_data=popup_data,
            focus_single=True,
        )

    @classmethod
    def _station_map_actions(
        cls,
        data: dict[str, Any],
        *,
        popup_data: Optional[dict[str, Any]] = None,
        focus_single: bool = False,
    ) -> list[dict[str, Any]]:
        raw_stations = data.get("stations")
        if not isinstance(raw_stations, list):
            raw_stations = []
        markers = []
        for station in raw_stations:
            if not isinstance(station, dict):
                continue
            marker_data = cls._station_marker_data(station)
            if marker_data is None:
                continue
            tzno, name, lon, lat = marker_data
            marker = {
                "id": tzno,
                "name": name,
                "coordinates": [lon, lat],
            }
            station_type = str(
                station.get("type") or station.get("station_type") or ""
            ).strip()
            if station_type:
                marker["type"] = station_type
            status = str(station.get("status") or "").strip()
            if status:
                marker["status"] = status
            marker["popup_content"] = cls._station_popup_content(
                station,
                popup_data if len(raw_stations) == 1 else None,
            )
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
                        "fit_bounds": not focus_single,
                    },
                }
            )
        if focus_single and len(markers) == 1:
            actions.append(
                {
                    "action_type": "FLY_TO",
                    "payload": {
                        "center": markers[0]["coordinates"],
                        "zoom": 12,
                    },
                }
            )
        return actions

    @classmethod
    def _station_popup_content(
        cls,
        station: dict[str, Any],
        popup_data: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        fields: list[dict[str, str]] = []
        seen_labels: set[str] = set()

        def add(label: str, value: Any) -> None:
            if value is None or value == "" or label in seen_labels:
                return
            seen_labels.add(label)
            fields.append({"label": label, "value": cls._popup_value(value)})

        add("站号", station.get("tzno"))
        add("站点类型", station.get("station_type") or station.get("type"))
        for key, label in (
            ("status", "状态"),
            ("province", "省份"),
            ("management", "管理单位"),
            ("operator", "运营单位"),
            ("sub_type", "子类型"),
            ("last_update", "最后更新时间"),
        ):
            add(label, station.get(key))

        if isinstance(popup_data, dict):
            for key, label in (
                ("status", "状态"),
                ("lastUpdateTime", "最后数据时间"),
                ("element", "观测要素"),
                ("dataType", "数据类型"),
                ("startTime", "开始时间"),
                ("endTime", "结束时间"),
                ("totalCount", "总记录数"),
                ("returnedCount", "返回记录数"),
                ("sampled", "是否采样"),
            ):
                add(label, popup_data.get(key))
            for container_key in ("statusRecord", "summary"):
                values = popup_data.get(container_key)
                if isinstance(values, dict):
                    for key, value in values.items():
                        add(_POPUP_LABELS.get(key, key), value)
            points = popup_data.get("points")
            if isinstance(points, list):
                add("趋势点数", len(points))
            records = popup_data.get("records")
            if isinstance(records, list):
                add("记录数", len(records))

        name = str(station.get("name") or station.get("tzno") or "观测站").strip()
        return {"title": f"{name} - 观测信息", "fields": fields}

    @staticmethod
    def _popup_value(value: Any) -> str:
        if isinstance(value, bool):
            return "是" if value else "否"
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return str(value)

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
    def _optional_bool(value: Any, name: str, *, default: bool) -> bool:
        if value is None:
            return default
        if not isinstance(value, bool):
            raise ValueError(f"{name} 必须为布尔值")
        return value

    @staticmethod
    def _required_time(value: Any, name: str) -> datetime:
        text = ObservationServiceTool._required_text(value, name)
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise ValueError(f"{name} 格式必须为 yyyy-MM-dd HH:mm:ss") from exc
