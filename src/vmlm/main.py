"""Application factory. Run with: uvicorn vmlm.main:create_app --factory"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from vmlm.api.errors import install_error_handlers
from vmlm.api.routers import public_router, router
from vmlm.config import Settings
from vmlm.db.session import init_db, make_engine, make_session_factory
from vmlm.domain.scheduler import Scheduler
from vmlm.domain.service import LifecycleService
from vmlm.logging_config import configure_logging
from vmlm.providers.base import VMProvider
from vmlm.providers.simulated import SimulatedProvider


def create_app(settings: Settings | None = None, provider: VMProvider | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings.database_url)
    init_db(engine)
    provider = provider or SimulatedProvider()
    service = LifecycleService(
        make_session_factory(engine),
        provider,
        Scheduler(settings.host_vcpus, settings.host_memory_mib),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        service.reconcile()  # resolve operations interrupted by a previous crash
        yield
        engine.dispose()

    app = FastAPI(
        title="VM Lifecycle Manager",
        version="0.1.0",
        description="Control plane over a SIMULATED VM provider. Not production-ready.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = service
    app.state.provider = provider
    app.state.engine = engine
    install_error_handlers(app)
    app.include_router(router)
    app.include_router(public_router)
    return app
