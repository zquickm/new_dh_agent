from __future__ import annotations

from typing import Any

from agents.tools.executor import ToolOutput


TOOL_NAME = "map_action"
TOOL_DESCRIPTION = (
    "驱动前端地图执行可视化动作，不查询或修改业务数据。"
    "单纯查询、输出或显示观测站点时禁止调用本工具，必须调用 observation_service；"
    "observation_service.search_stations 会自动发送 CLEAR 和 MARKER_ADD 地图事件，"
    "不得再调用本工具重复清图、标点、定位或适配站点视野。"
    "只有用户明确要求额外执行独立的定位、飞行、绘图、清除图层、展示弹窗/表格、"
    "播放时序或轨迹等地图操作时，才使用本工具。"
    "关键参数：LOCATE.points，MARKER_ADD.markers（每项含 id、name、coordinates），"
    "MARKER_REMOVE.marker_ids，POLYGON_DRAW.coordinates，"
    "CIRCLE_DRAW.center/radius，POPUP_SHOW.coordinates/title/content_html，"
    "TABLE_SHOW.title/columns/data，FLY_TO.center，TIMELINE_PLAY.scene，"
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
    "FLY_TO": ("center",),
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

    missing = [
        field
        for field in _REQUIRED_PAYLOAD_FIELDS[normalized_type]
        if field not in payload or payload[field] is None
    ]
    if missing:
        raise ValueError(
            f"{normalized_type} 缺少必填参数：{', '.join(missing)}"
        )

    event = {"action_type": normalized_type, "payload": payload}
    return ToolOutput(
        content={
            "success": True,
            "message": "地图动作已发送",
            "action_type": normalized_type,
        },
        client_events=(event,),
    )
