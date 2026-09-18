from __future__ import annotations

import json
from typing import Any

from agents.tools.executor import ToolOutput


TOOL_NAME = "map_action"
TOOL_DESCRIPTION = (
    "驱动前端地图执行可视化动作，不查询或修改业务数据。"
    "也可用 CLEAR 清除会话选区：设置顶层 clear_selection=true、payload={}；"
    "这是后端参数，不放入 payload。有效选区下改问其他地点、无关问题或取消选区时，"
    "必须先单独执行此操作，收到结果后再回答或查询新对象。普通区域追问、选区内单站查询"
    "和清理标记不清除选区；清选区时沿用既有 CLEAR 事件通知前端清理地图。"
    "除清除失效选区外，单纯查询、输出或显示业务观测站点时禁止调用本工具，必须调用 observation_service；"
    "observation_service 的站点查询和单站分析会自动发送 CLEAR 和 MARKER_ADD 地图事件，"
    "不得再调用本工具重复清图、标点、定位或适配站点视野。"
    "observation_service 的通用空间查询自动发送点标记 MARKER_ADD 和线面 GEOJSON_ADD，不重复绘制。"
    "断面测点地图展示除外：按断面业务 Skill 查询 database_query 后，用 MARKER_ADD 展示真实点位，"
    "使用独立图层 transect-stations；断面专题分析图使用 dm_duanmian_analysis。"
    "成功回执仅表示已提交地图指令，没有前端渲染确认，不得声称地图已显示或渲染完成。"
    "GEOJSON_ADD 用于展示已有的 GeoJSON FeatureCollection，支持点、线、面及多部件几何；"
    "必填 layer_id、geojson，可选 replace（默认 true）、group_id、fit_bounds（默认 false）。"
    "空间联动对话中，用户转而询问另一个明确地点的信息时，若已有该地点的可信坐标，"
    "应调用 FLY_TO 将视角移到该地点；观测服务已自动定位时不重复调用，不编造坐标。"
    "其他情况下，只有用户明确要求额外执行独立的定位、飞行、绘图、清除图层、展示弹窗/表格、"
    "播放时序或轨迹等地图操作时，才使用本工具。"
    "独立调用 FLY_TO 时会先清除旧的站点和定位标记，再添加目标点标记并移动视野；"
    "使用 center（兼容 coordinates）和 zoom，可通过 marker_name 指定标记名称。"
    "关键参数：LOCATE.points，MARKER_ADD.markers（每项含 id、name、coordinates），"
    "MARKER_REMOVE.marker_ids，POLYGON_DRAW.coordinates，"
    "CIRCLE_DRAW.center/radius，POPUP_SHOW.coordinates/title/content_html，"
    "TABLE_SHOW.title/columns/data，FLY_TO.center/zoom，TIMELINE_PLAY.scene，"
    "HEATMAP_SHOW.data_type，TRACK_PLAY.track_type/track_id，"
    "COMPARE_STATIONS.station_ids/data_types；CLEAR 可使用空 payload。"
)
MAP_ACTION_TYPES = (
    "LOCATE",
    "MARKER_ADD",
    "GEOJSON_ADD",
    "MARKER_REMOVE",
    "POLYGON_DRAW",
    "CIRCLE_DRAW",
    "POPUP_SHOW",
    "CLEAR",
    "TABLE_SHOW",
    "FLY_TO",
    "TIMELINE_PLAY",
    "HEATMAP_SHOW",
    "TRACK_PLAY",
    "COMPARE_STATIONS",
)
TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action_type": {
            "type": "string",
            "enum": list(MAP_ACTION_TYPES),
            "description": (
                "要执行的地图动作类型；payload 必须使用对应动作所需的字段。"
            ),
        },
        "clear_selection": {
            "type": "boolean",
            "default": False,
            "description": (
                "仅 CLEAR 可设为 true，清除后端会话选区及本轮选区上下文；"
                "不属于前端 payload。清选区并通知前端清图时 payload 使用 {}；"
                "普通清图、自动定位和选区内单站查询保持 false。"
            ),
        },
        "payload": {
            "type": "object",
            "description": (
                "动作参数；坐标系为 CGCS2000（EPSG:4490），坐标格式为 [经度, 纬度]。"
                "MARKER_ADD 的 markers 每项至少包含 id、name、coordinates；"
                "其余动作使用工具描述列出的必填字段。"
            ),
            "properties": {
                "geojson": {
                    "type": "object",
                    "description": "GEOJSON_ADD 必填的 GeoJSON FeatureCollection，保留完整 geometry 和 properties。",
                    "properties": {
                        "type": {"type": "string", "enum": ["FeatureCollection"]},
                        "features": {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["type", "features"],
                },
                "replace": {
                    "type": "boolean", "default": True,
                    "description": "GEOJSON_ADD 是否替换目标图层；false 为追加分页结果。",
                },
                "group_id": {
                    "type": "string",
                    "description": "GEOJSON_ADD 可选图层组；同一 run_id 首次使用该组时清除该组旧结果，后续类别共存。",
                },
                "points": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "number",
                            "description": "按点坐标位置排列的经度或纬度，单位为度。",
                        },
                        "minItems": 2,
                        "maxItems": 2,
                        "description": "一个定位点，格式为 [经度,纬度]。",
                    },
                    "description": "LOCATE 必填的定位点坐标数组。",
                },
                "markers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "description": "一个需要添加到地图的标记。",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "标记的唯一 ID，用于后续移除或更新。",
                            },
                            "name": {
                                "type": "string",
                                "description": "标记在地图上展示的名称。",
                            },
                            "coordinates": {
                                "type": "array",
                                "items": {
                                    "type": "number",
                                    "description": (
                                        "按点坐标位置排列的经度或纬度，单位为度。"
                                    ),
                                },
                                "minItems": 2,
                                "maxItems": 2,
                                "description": "标记位置，格式为 [经度,纬度]。",
                            },
                            "type": {
                                "type": "string",
                                "description": "可选的标记或站点类型。",
                            },
                            "status": {
                                "type": "string",
                                "description": "可选的标记或站点状态。",
                            },
                            "popup_content": {
                                "type": "object",
                                "description": "可选的标记弹窗结构化内容。",
                            },
                        },
                        "required": ["id", "name", "coordinates"],
                    },
                    "description": "MARKER_ADD 必填的地图标记数组。",
                },
                "marker_ids": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "description": "一个需要移除的标记 ID。",
                    },
                    "description": "MARKER_REMOVE 必填的标记 ID 列表。",
                },
                "coordinates": {
                    "type": "array",
                    "items": {
                        "description": "一个坐标数值，或多边形中的一个点坐标。"
                    },
                    "description": (
                        "POLYGON_DRAW 必填时格式为 [[经度,纬度],...]；"
                        "POPUP_SHOW 或兼容的 FLY_TO 输入时格式为 [经度,纬度]。"
                    ),
                },
                "center": {
                    "type": "array",
                    "items": {
                        "type": "number",
                        "description": "按点坐标位置排列的经度或纬度，单位为度。",
                    },
                    "minItems": 2,
                    "maxItems": 2,
                    "description": (
                        "CIRCLE_DRAW 或 FLY_TO 的中心位置，格式为 [经度,纬度]。"
                    ),
                },
                "radius": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "CIRCLE_DRAW 必填的圆半径，单位为米，必须大于 0。",
                },
                "title": {
                    "type": "string",
                    "description": "POPUP_SHOW 或 TABLE_SHOW 必填的展示标题。",
                },
                "content_html": {
                    "type": "string",
                    "description": "POPUP_SHOW 必填的弹窗 HTML 内容。",
                },
                "columns": {
                    "type": "array",
                    "items": {
                        "description": "一个表格列定义，字段应与前端地图契约一致。"
                    },
                    "description": "TABLE_SHOW 必填的表格列定义数组。",
                },
                "data": {
                    "type": "array",
                    "items": {
                        "description": "一条与 columns 对应的表格数据记录。"
                    },
                    "description": "TABLE_SHOW 必填的表格数据记录数组。",
                },
                "zoom": {
                    "type": "number",
                    "default": 12,
                    "description": "FLY_TO 的地图缩放级别，省略时默认 12。",
                },
                "marker_name": {
                    "type": "string",
                    "default": "定位点",
                    "description": (
                        "FLY_TO 自动添加的目标点标记名称，省略时默认“定位点”。"
                    ),
                },
                "scene": {
                    "description": "TIMELINE_PLAY 必填的时序播放场景数据。",
                },
                "data_type": {
                    "type": "string",
                    "description": "HEATMAP_SHOW 必填的热力图业务数据类型。",
                },
                "track_type": {
                    "type": "string",
                    "description": "TRACK_PLAY 必填的轨迹类型。",
                },
                "track_id": {
                    "type": "string",
                    "description": "TRACK_PLAY 必填的轨迹唯一 ID。",
                },
                "station_ids": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "description": "一个参与对比的观测站 ID。",
                    },
                    "description": "COMPARE_STATIONS 必填的观测站 ID 列表。",
                },
                "data_types": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "description": "一个需要对比的业务数据类型。",
                    },
                    "description": "COMPARE_STATIONS 必填的数据类型列表。",
                },
                "layer_id": {
                    "type": "string",
                    "description": "MARKER_ADD 可选、GEOJSON_ADD 必填的目标图层 ID。",
                },
                "layer_ids": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "description": "一个需要清除的地图图层 ID。",
                    },
                    "description": (
                        "CLEAR 可选的图层 ID 列表；省略时表示清除 Map Action 创建的全部图层。"
                    ),
                },
                "fit_bounds": {
                    "type": "boolean",
                    "description": (
                        "MARKER_ADD 或 GEOJSON_ADD 是否根据图层内容调整地图视野；通用选区查询保持 false。"
                    ),
                },
            },
        },
    },
    "required": ["action_type", "payload"],
    "additionalProperties": False,
}

_REQUIRED_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    "LOCATE": ("points",),
    "MARKER_ADD": ("markers",),
    "GEOJSON_ADD": ("layer_id", "geojson"),
    "MARKER_REMOVE": ("marker_ids",),
    "POLYGON_DRAW": ("coordinates",),
    "CIRCLE_DRAW": ("center", "radius"),
    "POPUP_SHOW": ("coordinates", "title", "content_html"),
    "CLEAR": (),
    "TABLE_SHOW": ("title", "columns", "data"),
    "FLY_TO": (),
    "TIMELINE_PLAY": ("scene",),
    "HEATMAP_SHOW": ("data_type",),
    "TRACK_PLAY": ("track_type", "track_id"),
    "COMPARE_STATIONS": ("station_ids", "data_types"),
}


def execute_map_action(
    action_type: str, payload: dict[str, Any], clear_selection: bool = False,
) -> ToolOutput:
    normalized_type = str(action_type or "").strip().upper()
    if normalized_type not in _REQUIRED_PAYLOAD_FIELDS:
        raise ValueError(f"不支持的地图动作：{action_type}")
    if not isinstance(payload, dict):
        raise ValueError("payload 必须为对象")
    if not isinstance(clear_selection, bool):
        raise ValueError("clear_selection 必须为布尔值")
    if clear_selection and normalized_type != "CLEAR":
        raise ValueError("clear_selection 只能用于 CLEAR")
    if "clear_selection" in payload:
        raise ValueError("clear_selection 是顶层工具参数，不得放入前端 payload")

    normalized_payload = payload
    if normalized_type == "FLY_TO":
        location = payload.get("center")
        if location is None:
            location = payload.get("coordinates")
        center = _point(location, "center 或 coordinates")
        zoom = payload.get("zoom", 12)
        if isinstance(zoom, bool) or not isinstance(zoom, (int, float)):
            raise ValueError("FLY_TO zoom 必须为数值")
        normalized_payload = {
            **payload,
            "center": center,
            "zoom": zoom,
        }
        normalized_payload.pop("coordinates", None)
        normalized_payload.pop("marker_name", None)

    missing = [
        field
        for field in _REQUIRED_PAYLOAD_FIELDS[normalized_type]
        if field not in normalized_payload or normalized_payload[field] is None
    ]
    if missing:
        raise ValueError(
            f"{normalized_type} 缺少必填参数：{', '.join(missing)}"
        )

    if normalized_type == "GEOJSON_ADD":
        for field in ("layer_id", "group_id"):
            if field in payload and (not isinstance(payload[field], str) or not payload[field].strip()):
                raise ValueError(f"GEOJSON_ADD {field} 必须为非空字符串")
        for field in ("replace", "fit_bounds"):
            if field in payload and not isinstance(payload[field], bool):
                raise ValueError(f"GEOJSON_ADD {field} 必须为布尔值")
        validate_feature_collection(payload["geojson"])

    event = {"action_type": normalized_type, "payload": normalized_payload}
    if clear_selection:
        # Consumed by the answer loop and ChatService, never serialized to SSE.
        event["clear_selection"] = True
    client_events = (event,)
    if normalized_type == "FLY_TO":
        marker_name = str(payload.get("marker_name") or "定位点").strip() or "定位点"
        client_events = (
            {
                "action_type": "CLEAR",
                "payload": {
                    "layer_ids": [
                        "observation-stations",
                        "map-location-markers",
                    ]
                },
            },
            {
                "action_type": "MARKER_ADD",
                "payload": {
                    "layer_id": "map-location-markers",
                    "markers": [
                        {
                            "id": "fly-to-target",
                            "name": marker_name,
                            "coordinates": normalized_payload["center"],
                        }
                    ],
                    "fit_bounds": False,
                },
            },
            event,
        )
    return ToolOutput(
        content={
            "success": True,
            "message": "会话选区清除请求已提交" if clear_selection else "地图动作已发送",
            "action_type": normalized_type,
        },
        client_events=client_events,
    )


def _point(value: Any, name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"FLY_TO {name} 必须包含两个数值")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise ValueError(f"FLY_TO {name} 必须包含两个数值")
    return [float(value[0]), float(value[1])]


def validate_feature_collection(value: Any) -> None:
    """Validate the envelope here; geometry topology is validated by the spatial service."""
    from agents.tools.spatial_query import SpatialCondition

    if not isinstance(value, dict) or value.get("type") != "FeatureCollection" or not isinstance(value.get("features"), list):
        raise ValueError("geojson 必须为 FeatureCollection")
    for feature in value["features"]:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError("geojson.features 必须为 Feature 数组")
        if not isinstance(feature.get("properties"), (dict, type(None))):
            raise ValueError("Feature.properties 必须为对象或 null")
        geometry = feature.get("geometry")
        if geometry is not None:
            SpatialCondition(relation="intersects", geometry=geometry)
    json.dumps(value, allow_nan=False)
