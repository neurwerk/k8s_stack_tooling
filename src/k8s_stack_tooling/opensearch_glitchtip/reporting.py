"""Studio links anchored to the occurrence, not notification or ingestion time."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from .settings import stamp, utc

if TYPE_CHECKING:
    from .state import Record


def quoted(value: str) -> str:
    # Lucene's < and > operators cannot be escaped. Never embed them or controls.
    if any(char in "<>" or ord(char) < 32 for char in value):
        raise ValueError("unsafe log-link literal")
    return '"' + re.sub(r'([+\-=&|!(){}\[\]^"~*?:\\/])', r"\\\1", value) + '"'


def log_links(studio_url: str | None, record: Record) -> tuple[str | None, str | None]:
    if studio_url is None:
        return None, None
    at = utc(record.timestamp)
    params = {
        "namespace": record.namespace,
        "start": stamp(at - timedelta(minutes=10)),
        "end": stamp(at + timedelta(minutes=5)),
        "q": f"kubernetes.container_name.keyword:{quoted(record.container)}",
    }
    base = studio_url.rstrip("/") + "/logs?"
    surrounding = base + urlencode(params)
    # Exact source evidence avoids pretending a text search over JSON is the policy
    # evaluator. No raw log contents or request data are copied into the URL.
    try:
        params["q"] += (
            f" AND stack_log.failure_type.keyword:{quoted(record.failure_type)}"
            f" AND _index:{quoted(record.index)} AND _id:{quoted(record.id)}"
        )
    except ValueError:
        return None, surrounding
    return base + urlencode(params), surrounding
