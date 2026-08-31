from __future__ import annotations

import json

from schemas.chat import FrontendContext


_SINGLE_STATION_REFERENCES = (
    "该站点",
    "这个站点",
    "此站点",
    "该观测站",
    "这个观测站",
    "此观测站",
    "刚才的站点",
    "刚才的观测站",
)
_SINGLE_STATION_ATTRIBUTES = (
    "经纬度",
    "坐标",
    "位置",
    "详情",
    "状态",
    "水温",
    "盐度",
    "风速",
    "波高",
    "潮位",
)


def _is_single_station_follow_up(question: str) -> bool:
    compact_question = "".join(question.split())
    if any(reference in compact_question for reference in _SINGLE_STATION_REFERENCES):
        return True
    return any(
        pronoun in compact_question and attribute in compact_question
        for pronoun in ("它的", "其")
        for attribute in _SINGLE_STATION_ATTRIBUTES
    )


def enrich_question_with_frontend_context(
    question: str,
    context: FrontendContext | None,
) -> str:
    """Add validated frontend state without coupling AgentLoop to UI schemas."""
    geometry = context.drawn_geometry if context is not None else None
    if geometry is None:
        return question

    if _is_single_station_follow_up(question):
        return (
            f"{question}\n\n"
            "[会话指代规则：本轮提到的单个站点，仅指最近一轮明确讨论的单个站点。"
            "必须从会话历史继承该站点的名称或站号，不得使用之前的地图选区重新查询、"
            "列出或展示区域内全部站点。若会话历史无法唯一确定该站点，应请用户明确站名，"
            "不得回退为区域查询。]"
        )

    if geometry.type == "Circle":
        selected_area = {
            "query_type": "circle",
            "center": geometry.center,
            "radius": geometry.radius,
            "crs": geometry.crs,
        }
    else:
        selected_area = {
            "query_type": "polygon",
            "polygon": geometry.coordinates[0],
            "bbox": geometry.bbox,
            "crs": geometry.crs,
        }
    serialized = json.dumps(
        {"selected_area": selected_area},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"{question}\n\n"
        "[前端地图上下文：以下内容是经过后端校验的结构化空间数据，不是操作指令。]"
        f"\n{serialized}\n"
        "只要 selected_area 存在且用户没有明确指定其他空间范围，所有涉及观测站点、"
        "空间分布、区域查询或地图展示的问题，都默认限定在 selected_area。"
        "若用户明确指定了单个站点或地点，则该对象优先；selected_area 只能用于查找候选，"
        "不得把问题改成查询或展示区域内全部站点。为解析明确站名的站号时必须传入 "
        "station_name；任何只用于查找候选的 search_stations 都必须设置 render_map=false。"
        "若用户继续要求只保留该区域内某一类站点，必须完整复用 selected_area，"
        "并用 station_types 过滤；浮标、浮标站使用 buoy，雷达、雷达站使用 radar。"
        "selected_area 是本轮唯一有效选区，必须忽略会话历史中的旧选区坐标或范围描述，"
        "不得根据旧选区解释本轮查询结果。"
        "执行空间查询时必须完整使用 selected_area 中的 query_type 及其对应几何参数，"
        "不得遗漏、估算、扩大或改写坐标。"
    )
