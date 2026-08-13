from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from common.json_result import JsonResult
from config.settings import get_settings
from controller.agent_controller import router as agent_router
from controller.conversation_controller import router as conversation_router
from controller.health_controller import router as health_router
from controller.recommend_controller import router as recommend_router


# Fail fast when YAML, environment overrides, or required secrets are invalid.
get_settings()

app = FastAPI(title="New DH ReAct Agent")
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
    return JSONResponse(
        status_code=422,
        content=JsonResult.error(422, str(exc)).model_dump(),
    )


if __name__ == "__main__":
    import uvicorn

    settings = get_settings().server
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
    )
