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
        "它的" in compact_question and attribute in compact_question
        for attribute in _SINGLE_STATION_ATTRIBUTES
    )


def enrich_question_with_frontend_context(
    question: str,
    context: FrontendContext | None,
) -> str:
    """Add validated frontend state without coupling AgentLoop to UI schemas."""
    geometry = context.drawn_geometry if context is not None else None
    if geometry is None:
        if context is not None:
            return (
                f"{question}\n\n"
                "[当前空间范围：会话当前没有有效选区，不得从历史问答恢复旧框选范围。"
                "明确站点或地点按本轮问题及最近明确对象处理；区域查询缺少明确范围时，"
                "请用户指定范围，不得猜测边界或半径。]"
            )
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
            "spatial": {
                "relation": "dwithin",
                "point": {"lon": geometry.center[0], "lat": geometry.center[1]},
                "distance": geometry.radius,
            },
        }
    else:
        selected_area = {
            "query_type": "polygon",
            "polygon": geometry.coordinates[0],
            "bbox": geometry.bbox,
            "crs": geometry.crs,
            "spatial": {
                "relation": "intersects",
                "geometry": {"type": "Polygon", "coordinates": geometry.coordinates},
            },
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
        "这是会话当前有效选区，selected_area 是本轮唯一有效选区，不从历史恢复旧范围。"
        "仅在本轮问题与选区相关时使用；明确单个对象时以该对象为准，无关问题不套用范围。"
        "用户询问区域内有什么、空间分布或类别筛选且未指定其他范围时，默认限定在 selected_area。"
        "区域总览应覆盖可查询的各类要素，具体查询流程由本轮任务对应的 Skill 指导。"
        "完整保留 selected_area 的几何与坐标，不用外接矩形替代圆或多边形，不扩大范围。"
        "切换地点后的追问沿用新地点，不因仍有选区就查询旧区域；缺少新范围时请用户明确。"
        "查询本轮新选区、选区内筛选和单个对象追问不取消选区；清理标记不等于取消选区。"
    )
