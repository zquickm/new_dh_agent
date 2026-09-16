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
from controller.feedback_controller import admin_router as admin_feedback_router
from controller.feedback_controller import user_router as user_feedback_router
from controller.recommend_controller import router as recommend_router
from controller.system_prompt_controller import router as system_prompt_router
from controller.skill_controller import router as skill_router
from database.runtime import database_runtime
from middleware.internal_request_auth import InternalRequestAuthMiddleware
from services.chat_service import chat_service
from services.dictionary_refresh_service import dictionary_refresh_service
from services.system_prompt_service import system_prompt_service
from services.skill_service import skill_service


settings = get_settings()
configure_logging(settings.logging)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    previous_prompt_provider = chat_service._prompt_provider
    previous_skill_provider = chat_service._skill_provider
    await database_runtime.start()
    try:
        await system_prompt_service.start()
        await skill_service.start()
        chat_service.set_prompt_provider(system_prompt_service)
        chat_service.set_skill_provider(skill_service)
        await chat_service.start()
        await dictionary_refresh_service.start()
        logger.info("application_started profile=%s", settings.app_profile)
        yield
    finally:
        chat_service.set_prompt_provider(previous_prompt_provider)
        chat_service.set_skill_provider(previous_skill_provider)
        await dictionary_refresh_service.stop()
        await chat_service.close()
        await database_runtime.close()
        logger.info("application_stopped")


app = FastAPI(title="New DH ReAct Agent", lifespan=lifespan)
app.add_middleware(
    InternalRequestAuthMiddleware,
    header_name=settings.internal_request.header_name,
    token=settings.internal_request.token.get_secret_value(),
)
app.include_router(health_router)
app.include_router(conversation_router)
app.include_router(user_feedback_router)
app.include_router(recommend_router)
app.include_router(agent_router)
app.include_router(system_prompt_router)
app.include_router(skill_router)
app.include_router(admin_feedback_router)


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
