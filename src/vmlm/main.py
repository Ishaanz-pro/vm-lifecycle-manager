"""Application factory. Run with: uvicorn vmlm.main:create_app --factory

Process layout (single process by default):
  * FastAPI request handlers           - accept requests, write Operation rows
  * worker thread                      - executes Operation rows against the provider
  * reconcile thread                   - periodic lease recovery, heartbeat and drift checks
Set VMLM_WORKER_ENABLED=false here and run `python -m vmlm.worker_main` in another process to
split them; both talk only through the database.

Shutdown: uvicorn handles SIGTERM/SIGINT and runs the lifespan exit, which stops the loops,
waits up to `shutdown_grace_s` for an in-flight operation, and disposes the engine. An
operation still running at that point stays claimed; its lease expires and it is re-queued.
"""

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from vmlm.api.errors import install_error_handlers
from vmlm.api.middleware import RequestContextMiddleware
from vmlm.api.routers import admin, debug, public, router
from vmlm.api.ui import install_ui
from vmlm.config import Settings
from vmlm.db.models import utcnow
from vmlm.db.session import init_db, make_engine, make_session_factory
from vmlm.domain.core import Clock
from vmlm.domain.failures import FailureInjector
from vmlm.domain.reconciler import Reconciler
from vmlm.domain.scheduler import Scheduler
from vmlm.domain.service import LifecycleService
from vmlm.domain.worker import Worker
from vmlm.logging_config import configure_logging
from vmlm.providers.base import VMProvider
from vmlm.providers.factory import build_provider

log = logging.getLogger("vmlm.main")

DESCRIPTION = """
Mini cloud control plane over a **simulated** VM provider (a capability-gated QEMU provider
exists for local experiments). A local learning project: not hardened for production or
multi-tenant use. See the README for limitations.
"""


def create_app(
    settings: Settings | None = None,
    provider: VMProvider | None = None,
    clock: Clock = utcnow,
) -> FastAPI:
    """`clock` is injectable so tests can advance time (leases, backoff, key expiry)."""
    settings = settings or Settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    provider = provider or build_provider(settings)
    scheduler = Scheduler()
    injector = FailureInjector(enabled=settings.failure_injection_enabled)
    service = LifecycleService(
        session_factory,
        scheduler,
        max_attempts=settings.max_attempts,
        idempotency_ttl_s=settings.idempotency_ttl_s,
        clock=clock,
    )
    worker = Worker(
        session_factory,
        provider,
        scheduler,
        lease_s=settings.worker_lease_s,
        provider_timeout_s=settings.provider_timeout_s,
        retry_base_delay_s=settings.retry_base_delay_s,
        retry_max_delay_s=settings.retry_max_delay_s,
        injector=injector,
        clock=clock,
    )
    reconciler = Reconciler(
        session_factory,
        provider,
        service,
        worker,
        heartbeat_stale_s=settings.heartbeat_stale_s,
        clock=clock,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = threading.Event()  # fresh per startup so the app can be stopped and restarted
        threads: list[threading.Thread] = []

        def reconcile_loop() -> None:
            while not stop.wait(settings.reconcile_interval_s):
                try:
                    reconciler.run_once()
                except Exception:  # noqa: BLE001
                    log.exception("reconcile_iteration_failed")

        service.seed_hosts(settings.hosts)
        reconciler.run_once()  # recover anything interrupted before the previous shutdown
        if settings.worker_enabled:
            t = threading.Thread(
                target=worker.run_forever,
                args=(stop, settings.worker_poll_interval_s),
                name="vmlm-worker",
                daemon=True,
            )
            worker.thread = t  # type: ignore[attr-defined]
            threads.append(t)
        if settings.reconcile_interval_s > 0:
            threads.append(
                threading.Thread(target=reconcile_loop, name="vmlm-reconcile", daemon=True)
            )
        for t in threads:
            t.start()
        log.info(
            "app_started",
            extra={"provider": provider.describe()["name"], "worker": settings.worker_enabled},
        )
        try:
            yield
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=settings.shutdown_grace_s)
            worker.close()
            engine.dispose()
            log.info("app_stopped")

    app = FastAPI(
        title="VM Lifecycle Manager",
        version="0.2.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.provider = provider
    app.state.service = service
    app.state.worker = worker
    app.state.reconciler = reconciler
    app.state.injector = injector
    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(router)
    app.include_router(admin)
    app.include_router(debug)
    app.include_router(public)
    install_ui(app)
    return app
