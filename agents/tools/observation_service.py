from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from agents.tools.executor import ToolOutput
from agents.tools.map_action import execute_map_action, validate_feature_collection
from agents.tools.spatial_query import QUERY_PARAMETERS, SpatialElementQuery
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
    "也查询已注册空间要素的位置和属性（不含断面）：list_spatial_elements 发现目录，"
    "断面及其测点统一使用 database_query，不调用本工具的 dm_duanmian 查询，也不回退为属性查询。"
    "get_spatial_element_detail 获取字段定义，query_spatial_element 执行通用查询。"
    "有有效 selected_area 时，用户泛问‘有哪些’或‘这个区域有哪些要素’，"
    "先发现目录，再对每个 dataKey 使用同一 selected_area.spatial 做 mode=spatial 查询，"
    "不能只查观测站，也不能把目录列表当作区域内的结果。"
    "同阶段各要素的独立调用应在同一轮提交；仅自选字段、属性过滤或排序时需先获取详情，"
    "纯区域查询可直接使用默认字段。按要素中文名、total 和 features 汇总，"
    "returned 仅表示本页文字记录数，自动绘图覆盖范围看 map_result.fetched、mapped、missing_geometry；"
    "complete 仅表示全部匹配要素已包含在绘图指令中，不代表前端已显示。"
    "地图动作回执不含前端渲染确认，只能说明已提交展示指令；查询失败不等于该类要素不存在。"
    "通用查询参数放在 query 对象中，字段及运算符以详情为准，不编造；结果为 GeoJSON，"
    "mode=spatial 默认自动分页取全，点要素发送 MARKER_ADD，线面发送 GEOJSON_ADD，勿重复调用 map_action；"
    "属性查询默认只返回数据，可设顶层 render_map=true 展示。render_map=false 仅查询一页。"
    "这是地图选区观测站和明确站名的业务观测站数据的唯一业务工具；用户继续询问前一轮返回的某个业务观测站时，"
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
                "list_spatial_elements",
                "get_spatial_element_detail",
                "query_spatial_element",
            ],
            "description": (
                "要执行的观测服务操作。参数要求：search_stations 需要 query_type "
                "及对应几何参数；station_status 需要 tznos；station_detail 和 "
                "observation_summary 需要 tzno；observation_trend 需要 tzno、element；"
                "observation_range 需要 tzno、data_type、start_time、end_time。"
                "get_spatial_element_detail 需要 dataKey；query_spatial_element 需要 query；"
                "list_spatial_elements 不需要其他参数。"
            ),
        },
        "query_type": {
            "type": "string",
            "enum": ["bbox", "circle", "polygon"],
            "description": (
                "search_stations 使用的空间查询类型：bbox 为外接矩形，circle 为圆形，"
                "polygon 为多边形。"
            ),
        },
        "dataKey": {
            "type": "string",
            "minLength": 1,
            "description": "get_spatial_element_detail 必填的目录要素标识。",
        },
        "query": {
            **{key: value for key, value in QUERY_PARAMETERS.items() if key != "$defs"},
            "description": (
                "query_spatial_element 必填；对应 v2 /query 请求体。选区查询传入目录中的 "
                "element=dataKey、mode=spatial、spatial=selected_area.spatial；"
                "勿混用旧站点查询参数 query_type/center/radius/polygon。"
            ),
        },
        "bbox": {
            "type": "array",
            "items": {
                "type": "number",
                "description": "按数组位置排列的一项经度或纬度数值，单位为度。",
            },
            "minItems": 4,
            "maxItems": 4,
            "description": (
                "query_type=bbox 时必填的查询范围，格式为 "
                "[最小经度,最小纬度,最大经度,最大纬度]，坐标系为 EPSG:4490。"
            ),
        },
        "center": {
            "type": "array",
            "items": {
                "type": "number",
                "description": "按数组位置排列的经度或纬度数值，单位为度。",
            },
            "minItems": 2,
            "maxItems": 2,
            "description": (
                "query_type=circle 时必填的圆心，格式为 [经度,纬度]，"
                "坐标系为 EPSG:4490。"
            ),
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
                "items": {
                    "type": "number",
                    "description": "按点坐标位置排列的经度或纬度数值，单位为度。",
                },
                "minItems": 2,
                "maxItems": 2,
                "description": "多边形的一个顶点，格式为 [经度,纬度]。",
            },
            "minItems": 3,
            "description": (
                "query_type=polygon 时必填的多边形顶点数组，至少包含三个顶点，"
                "坐标系为 EPSG:4490。"
            ),
        },
        "station_types": {
            "type": "array",
            "items": {
                "type": "string",
                "description": "一个站点类型编码，例如 buoy 或 radar。",
            },
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
            "description": (
                "search_stations 默认 true；query_spatial_element 在 mode=spatial 时默认 true，"
                "属性查询默认 false。通用展示会强制 includeGeometry=true，从 offset=0 自动取全，"
                "limit 为每页大小；非零 offset 请设 false。仅内部查找时设 false。"
            ),
        },
        "tznos": {
            "type": "array",
            "items": {
                "type": "string",
                "description": "一个观测站的唯一站号。",
            },
            "minItems": 1,
            "maxItems": 50,
            "description": (
                "station_status 必填的观测站号列表，一次可查询 1 到 50 个站点。"
            ),
        },
        "tzno": {
            "type": "string",
            "description": (
                "目标观测站的唯一站号，station_detail、observation_summary、"
                "observation_trend 和 observation_range 必填。"
            ),
        },
        "element": {
            "type": "string",
            "description": "observation_trend 必填的观测要素 key。",
        },
        "hours": {
            "type": "integer",
            "minimum": 1,
            "default": 24,
            "description": (
                "observation_trend 向前查询的小时数，必须大于 0；省略时默认 24。"
            ),
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
            "description": (
                "observation_range 的时间排序方向：asc 从早到晚，desc 从晚到早；"
                "省略时默认 asc。"
            ),
        },
        "max_records": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5000,
            "default": 2000,
            "description": (
                "observation_range 最多返回的观测记录数，范围 1 到 5000；"
                "省略时默认 2000。"
            ),
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
        if operation in {
            "list_spatial_elements", "get_spatial_element_detail", "query_spatial_element",
        }:
            allowed = {
                "list_spatial_elements": set(),
                "get_spatial_element_detail": {"dataKey"},
                "query_spatial_element": {"query", "render_map"},
            }[operation]
            if arguments.keys() - allowed:
                raise ValueError(f"{operation} 仅接受参数：{', '.join(sorted(allowed)) or '无'}")
            if operation == "list_spatial_elements":
                data = {"elements": [
                    item for item in await self.client.list_spatial_elements()
                    if item.get("dataKey") != "dm_duanmian"
                ]}
            elif operation == "get_spatial_element_detail":
                data_key = self._required_text(arguments.get("dataKey"), "dataKey")
                if data_key == "dm_duanmian":
                    raise ValueError("断面及其测点请使用 database_query，不使用通用空间查询")
                data = await self.client.get_spatial_element_detail(data_key)
            else:
                query = SpatialElementQuery.model_validate(arguments.get("query"))
                if query.element == "dm_duanmian":
                    raise ValueError("断面及其测点请使用 database_query，不使用通用空间查询")
                payload = query.model_dump(exclude_unset=True, exclude_none=True)
                json.dumps(payload, allow_nan=False)
                render_map = self._optional_bool(arguments.get("render_map"), "render_map", default=query.mode == "spatial")
                if render_map:
                    if query.offset != 0:
                        raise ValueError("自动地图展示从 offset=0 取全；单页查询请设置 render_map=false")
                    return await self._spatial_query_with_map(payload)
                data = await self.client.query_spatial_element(payload)
            return ToolOutput(content={**data, "operation": operation})
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

    async def _spatial_query_with_map(self, payload: dict[str, Any]) -> ToolOutput:
        # Keep paging inside the tool; the model sees one page, the map receives all pages.
        payload = {**payload, "includeGeometry": True, "offset": 0}
        detail = await self.client.get_spatial_element_detail(payload["element"])
        max_limit = detail.get("maxQueryLimit")
        if type(max_limit) is int and max_limit > 0:
            payload["limit"] = min(payload.get("limit", 20), max_limit)
        public_fields = {field.get("name") for field in detail.get("fields", []) if isinstance(field, dict)}
        id_column = detail.get("idColumn")
        if not payload.get("orderBy") and id_column and id_column in public_fields:
            payload["orderBy"] = id_column
        offset = 0
        first_page = None
        total = None
        mapped = 0
        seen_ids: set[str] = set()
        events: list[dict[str, Any]] = []
        markers: list[dict[str, Any]] = []
        is_station = payload["element"] == "obs_station"
        layer_id = "observation-stations" if is_station else f"spatial-element:{payload['element']}"
        geometry_layer_id = f"spatial-element:{payload['element']}:shapes"
        while True:
            page = await self.client.query_spatial_element({**payload, "offset": offset})
            validate_feature_collection(page)
            features = page["features"]
            page_total, returned = page.get("total"), page.get("returned")
            if type(page_total) is not int or page_total < 0 or type(returned) is not int or returned != len(features):
                raise ValueError("空间服务分页计数无效，未发送地图结果")
            if total is None:
                total, first_page = page_total, page
            if page_total != total or offset + returned > total or (not returned and offset < total):
                raise ValueError("空间服务分页结果不一致，未发送地图结果，请重新查询")
            for feature in features:
                if feature.get("id") is not None:
                    feature_id = str(feature["id"])
                    if feature_id in seen_ids:
                        raise ValueError("空间服务返回重复要素，未发送地图结果，请重新查询")
                    seen_ids.add(feature_id)
            drawable = [feature for feature in features if feature.get("geometry") is not None]
            mapped += len(drawable)
            shapes = []
            for index, feature in enumerate(drawable):
                geometry = feature["geometry"]
                if geometry["type"] not in ("Point", "MultiPoint"):
                    shapes.append(feature)
                    continue
                properties = feature.get("properties") or {}
                feature_id = properties.get("tzno") if is_station else None
                if feature_id is None:
                    feature_id = feature.get("id")
                if feature_id is None:
                    feature_id = properties.get(detail.get("idColumn"))
                if feature_id is None:
                    feature_id = f"{payload['element']}:{offset + index}"
                coordinates = ([geometry["coordinates"]] if geometry["type"] == "Point"
                               else geometry["coordinates"])
                for point_index, point in enumerate(coordinates):
                    marker = {
                        "id": str(feature_id) if geometry["type"] == "Point" else f"{feature_id}:{point_index}",
                        "name": str(properties.get("name") or properties.get("code") or feature_id),
                        "coordinates": point[:2],
                        "popup_content": {
                            "title": str(properties.get("name") or properties.get("code") or feature_id),
                            "fields": [{"label": key, "value": self._popup_value(value)}
                                       for key, value in properties.items() if value is not None],
                        },
                    }
                    for key in ("type", "status"):
                        value = properties.get(key) or (properties.get("station_type") if key == "type" else None)
                        if value is not None:
                            marker[key] = str(value)
                    markers.append(marker)
            if shapes:
                events.extend(execute_map_action("GEOJSON_ADD", {
                    "group_id": "spatial-query-results",
                    "layer_id": geometry_layer_id,
                    "geojson": {"type": "FeatureCollection", "features": shapes},
                    "replace": not events,
                    "fit_bounds": False,
                }).client_events)
            offset += returned
            if offset >= total:
                break
        # Clear only this category once; submit all point pages together so none are overwritten.
        point_events = [{
            "action_type": "CLEAR", "payload": {"layer_ids": [layer_id, geometry_layer_id]},
        }]
        if markers:
            point_events.extend(execute_map_action("MARKER_ADD", {
                "layer_id": layer_id, "markers": markers, "fit_bounds": False,
            }).client_events)
        return ToolOutput(content={
            **first_page, "operation": "query_spatial_element",
            "map_result": {
                "layer_id": layer_id,
                "geometry_layer_id": geometry_layer_id,
                "fetched": offset, "mapped": mapped, "missing_geometry": offset - mapped,
                "complete": mapped == total, "status": "actions_sent",
                "render_confirmed": False,
                "message": (
                    f"已查询 {offset} 条，已为 {mapped} 条生成绘图指令，"
                    f"{offset - mapped} 条缺少几何未绘制；未确认前端渲染结果。"
                ),
            },
        }, client_events=tuple(point_events + events))

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
