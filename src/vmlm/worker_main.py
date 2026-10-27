"""Standalone worker process: `python -m vmlm.worker_main`.

Use with VMLM_WORKER_ENABLED=false on the API process. Several of these may run against the
same database; the claim transaction guarantees each operation is executed by one of them.
Handles SIGTERM/SIGINT by finishing the current operation (bounded by the provider timeout)
and exiting 0.
"""

import logging
import signal
import sys
import threading

from vmlm.config import Settings
from vmlm.db.session import init_db, make_engine, make_session_factory
from vmlm.domain.failures import FailureInjector
from vmlm.domain.scheduler import Scheduler
from vmlm.domain.worker import Worker
from vmlm.logging_config import configure_logging
from vmlm.providers.factory import build_provider

log = logging.getLogger("vmlm.worker_main")


def main() -> int:
    settings = Settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings.database_url)
    init_db(engine)
    worker = Worker(
        make_session_factory(engine),
        build_provider(settings),
        Scheduler(),
        lease_s=settings.worker_lease_s,
        provider_timeout_s=settings.provider_timeout_s,
        retry_base_delay_s=settings.retry_base_delay_s,
        retry_max_delay_s=settings.retry_max_delay_s,
        injector=FailureInjector(enabled=settings.failure_injection_enabled),
    )
    stop = threading.Event()

    def _signal(signum: int, _frame: object) -> None:
        log.info("signal_received", extra={"signal": signal.Signals(signum).name})
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _signal)
    worker.run_forever(stop, settings.worker_poll_interval_s)
    worker.close()
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
