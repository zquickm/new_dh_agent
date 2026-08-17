from __future__ import annotations

import json
import re
from datetime import datetime


TOOL_NAME = "CurrentServerTime"
TOOL_DESCRIPTION = "获取当前服务器的本地日期、时间、时区和 UTC 偏移，无需参数"


def matches_current_server_time(query: str) -> bool:
    normalized = re.sub(r"\s+", "", query).lower()
    patterns = (
        r"(?:当前|现在).*(?:时间|几点)",
        r"(?:服务器|系统|本机).*(?:时间|几点)",
        r"(?:服务器|系统|本机).*时区",
        r"几点了?",
        r"current.*(?:server)?time",
        r"what.*time.*(?:server|system)",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def get_current_server_time(argument: str = "") -> str:
    """Return the current local time of the server process."""

    del argument
    current = datetime.now().astimezone()
    return json.dumps(
        {
            "datetime": current.isoformat(timespec="seconds"),
            "timezone": current.tzname() or "",
            "utc_offset": current.strftime("%z"),
        },
        ensure_ascii=False,
    )
