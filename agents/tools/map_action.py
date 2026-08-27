from __future__ import annotations

from typing import Any

from agents.tools.executor import ToolOutput


TOOL_NAME = "map_action"
TOOL_DESCRIPTION = (
    "驱动前端地图执行可视化动作，不查询或修改业务数据。"
    "单纯查询、输出或显示观测站点时禁止调用本工具，必须调用 observation_service；"
    "observation_service 的站点查询和单站分析会自动发送 CLEAR 和 MARKER_ADD 地图事件，"
    "不得再调用本工具重复清图、标点、定位或适配站点视野。"
    "只有用户明确要求额外执行独立的定位、飞行、绘图、清除图层、展示弹窗/表格、"
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
            "description": "地图动作类型",
        },
        "payload": {
            "type": "object",
            "description": (
                "动作参数；坐标系为 CGCS2000（EPSG:4490），坐标格式为 [经度, 纬度]。"
                "MARKER_ADD 的 markers 每项至少包含 id、name、coordinates；"
                "其余动作使用工具描述列出的必填字段。"
            ),
        },
    },
    "required": ["action_type", "payload"],
    "additionalProperties": False,
}

_REQUIRED_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    "LOCATE": ("points",),
    "MARKER_ADD": ("markers",),
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


def execute_map_action(action_type: str, payload: dict[str, Any]) -> ToolOutput:
    normalized_type = str(action_type or "").strip().upper()
    if normalized_type not in _REQUIRED_PAYLOAD_FIELDS:
        raise ValueError(f"不支持的地图动作：{action_type}")
    if not isinstance(payload, dict):
        raise ValueError("payload 必须为对象")

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

    event = {"action_type": normalized_type, "payload": normalized_payload}
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
            "message": "地图动作已发送",
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
