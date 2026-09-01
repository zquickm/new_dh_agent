from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from config.settings import LoggingSettings, PROJECT_DIR


def configure_logging(settings: LoggingSettings) -> Path:
    level = getattr(logging, settings.level.strip().upper(), None)
    if not isinstance(level, int):
        raise ValueError(f"无效日志级别：{settings.level}")

    directory = Path(settings.directory)
    if not directory.is_absolute():
        directory = PROJECT_DIR / directory
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "dh-backend.log"

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s pid=%(process)d %(message)s"
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        backupCount=settings.retention_days,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logging.basicConfig(
        level=level,
        handlers=[console, file_handler],
        force=True,
    )

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    return log_path
