from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bravo import db
from bravo.api import deps
from bravo.api.routes import router
from bravo.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    deps.reset_limiters()
    if not get_settings().is_production:
        db.create_all()  # production schema is managed by Alembic migrations
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Jarvis: Bravo Vision operations", version="0.1.0", lifespan=lifespan,
                  docs_url=None if settings.is_production else "/docs", redoc_url=None, openapi_url=None if settings.is_production else "/openapi.json")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_methods=["GET", "POST", "PUT", "PATCH"],
                       allow_headers=["Authorization", "Content-Type"], max_age=600)

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    app.include_router(router)
    return app


app = create_app()
