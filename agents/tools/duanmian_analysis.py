from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

from agents.tools.executor import ToolOutput


TOOL_NAME = "dm_duanmian_analysis"
TOOL_DESCRIPTION = (
    "打开前端断面专题分析浮层，由前端按参数查询并出图。"
    "用户要求断面专题出图、沿水深曲线、断面剖面图、T-S 点聚图、营养盐结构、"
    "溶解氧垂向分布或表层沿程变化图时使用。"
    "仅发送展示动作，不返回实测数据或分析结论，不表示前端已完成出图；"
    "用户只要求查询数据、统计或文字分析时使用 database_query。"
    "不要为出图额外调用 database_query 或 map_action。"
    "analysis 必填，无法确定分析类型时先询问用户。"
    "可选参数只传用户指定或上下文明确的值；voyage 缺省时仅打开浮层，不自动查询。"
    "ts-scatter 固定合并全部断面，会忽略 section；temp-section 固定使用 chemistry。"
)
ANALYSIS_TYPES = (
    "depth-curve", "temp-section", "ts-scatter", "nutrient", "do-profile", "surface",
)
TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "analysis": {
            "type": "string",
            "enum": list(ANALYSIS_TYPES),
            "description": (
                "分析类型：depth-curve=单要素沿水深变化曲线；"
                "temp-section=断面剖面图（化学要素色阶剖面）；"
                "ts-scatter=T-S 点聚图（水团识别）；nutrient=营养盐结构分析；"
                "do-profile=溶解氧垂向分布；surface=表层沿程变化图。"
            ),
        },
        "section": {
            "type": "string",
            "enum": ["E1", "E2", "E3"],
            "description": "可选断面编码 E1/E2/E3；ts-scatter 合并全部断面，不需要传。",
        },
        "voyage": {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}(-\d{2})?$",
            "description": "可选航次，格式 yyyy-MM 或 yyyy-MM-dd；缺省不自动查询，不猜测航次。",
        },
        "elementType": {
            "type": "string",
            "enum": ["chemistry", "hydromet"],
            "description": "可选要素类型：chemistry=海水化学，hydromet=水文气象；temp-section 固定 chemistry。",
        },
        "element": {
            "type": "string",
            "description": "可选要素物理列名或中文短名，例如 temperature_c 或 温度；保留原名称。",
        },
    },
    "required": ["analysis"],
    "additionalProperties": False,
}


def execute_duanmian_analysis(
    analysis: str,
    section: Optional[str] = None,
    voyage: Optional[str] = None,
    elementType: Optional[str] = None,
    element: Optional[str] = None,
) -> ToolOutput:
    payload = {"analysis": analysis}
    payload.update({
        name: value
        for name, value in {
            "section": section,
            "voyage": voyage,
            "elementType": elementType,
            "element": element,
        }.items()
        if value is not None
    })
    for name, value in payload.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 必须为非空字符串")
        value = value.strip()
        allowed = TOOL_PARAMETERS["properties"][name].get("enum")
        if allowed is not None and value not in allowed:
            raise ValueError(f"{name} 必须为 {'、'.join(allowed)} 之一")
        payload[name] = value

    if "voyage" in payload:
        value = payload["voyage"]
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}(-[0-9]{2})?", value):
            raise ValueError("voyage 必须为 yyyy-MM 或 yyyy-MM-dd")
        try:
            date.fromisoformat(value + "-01" if len(value) == 7 else value)
        except ValueError as exc:
            raise ValueError("voyage 必须为有效的年月或日期") from exc

    if payload["analysis"] == "ts-scatter":
        payload.pop("section", None)
    elif payload["analysis"] == "temp-section":
        payload["elementType"] = "chemistry"

    return ToolOutput(
        content={"success": True, "message": "action received", "parameters": payload},
        client_events=({"action_type": TOOL_NAME, "payload": payload},),
    )
