"""Logging: readable text by default, one JSON object per line with MERIDIAN_LOG_FORMAT=json."""

import json
import logging
from datetime import datetime, timezone

from .config import get_settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def setup_logging() -> None:
    root = logging.getLogger()
    handler = logging.StreamHandler()
    if get_settings().log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    # httpx logs every request URL at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
