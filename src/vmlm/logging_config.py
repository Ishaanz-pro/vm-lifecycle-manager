"""Structured (JSON-lines) logging for the `vmlm` logger tree.

Every record is stamped with request_id / operation_id from contextvars *at creation time*
(via the log-record factory), so one request can be followed across the API, the worker and
the reconciler, and so log capture tools that format later still see the right ids.
Secrets are never logged: settings are not dumped and API keys are never echoed.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from vmlm.observability.context import operation_id_var, request_id_var

_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}
_FACTORY_INSTALLED = False


def _install_record_factory() -> None:
    global _FACTORY_INSTALLED
    if _FACTORY_INSTALLED:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        if record.name.startswith("vmlm"):
            if (rid := request_id_var.get()) is not None:
                record.request_id = rid
            if (oid := operation_id_var.get()) is not None:
                record.operation_id = oid
        return record

    logging.setLogRecordFactory(factory)
    _FACTORY_INSTALLED = True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update({k: v for k, v in vars(record).items() if k not in _STANDARD})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    _install_record_factory()
    logger = logging.getLogger("vmlm")
    logger.setLevel(level.upper())
    if not any(isinstance(h.formatter, JsonFormatter) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
