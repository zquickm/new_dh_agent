import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from common.logging_config import configure_logging
from common.json_result import JsonResult
from config.settings import get_settings
from controller.agent_controller import router as agent_router
from controller.conversation_controller import router as conversation_router
from controller.health_controller import router as health_router
from controller.recommend_controller import router as recommend_router
from middleware.internal_request_auth import InternalRequestAuthMiddleware
from services.chat_service import chat_service


settings = get_settings()
configure_logging(settings.logging)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("application_started profile=%s", settings.app_profile)
    try:
        yield
    finally:
        await chat_service.close()
        logger.info("application_stopped")


app = FastAPI(title="New DH ReAct Agent", lifespan=lifespan)
app.add_middleware(
    InternalRequestAuthMiddleware,
    header_name=settings.internal_request.header_name,
    token=settings.internal_request.token.get_secret_value(),
)
app.include_router(health_router)
app.include_router(conversation_router)
app.include_router(recommend_router)
app.include_router(agent_router)


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=JsonResult.error(exc.status_code, str(exc.detail)).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    errors = [
        {
            key: value
            for key, value in error.items()
            if key not in {"input", "ctx", "url"}
        }
        for error in exc.errors()
    ]
    logger.warning("Agent request validation failed: %s", errors)
    message = "; ".join(
        f"{'.'.join(str(part) for part in error.get('loc', ()))}: "
        f"{error.get('msg', '请求参数无效')}"
        for error in errors
    )
    return JSONResponse(
        status_code=422,
        content=JsonResult.error(422, message or "请求参数无效").model_dump(),
    )


if __name__ == "__main__":
    import uvicorn

    server = settings.server
    uvicorn.run(
        "main:app",
        host=server.host,
        port=server.port,
        reload=server.reload,
        log_config=None,
    )
