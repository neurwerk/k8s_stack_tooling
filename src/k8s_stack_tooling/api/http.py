"""Shared HTTP helpers for init scripts.

Uses the ``requests`` library instead of Python stdlib ``urllib``.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests


def log(msg: str, prefix: str | None = None) -> None:
    """Print a log message with an optional prefix from env."""
    if not prefix:
        prefix = os.environ.get("KC_LOG_PREFIX", "init")
    print(f"[{prefix}] {msg}")


def request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: Any | None = None,
    verify: str | bool | None = None,
    form_data: bool = False,
    timeout: float = 30,
) -> tuple[int | None, Any]:
    """Make an HTTP request and return ``(status_code, parsed_body)``.

    Parameters
    ----------
    url:
        Full request URL.
    method:
        HTTP method (``GET``, ``POST``, ``PUT``, ``PATCH``, ``DELETE``, …).
    headers:
        Optional HTTP headers.
    body:
        Request body.  By default sent as ``application/json``.  Set
        *form_data* to ``True`` to send as ``application/x-www-form-urlencoded``.
    verify:
        Passed through to ``requests.request(verify=…)``.
    form_data:
        If ``True``, *body* is sent as URL-encoded form data
        instead of JSON.
    timeout:
        Maximum request duration in seconds.

    Returns
    -------
    A ``(status_code, parsed_json)`` tuple.  ``status_code`` is ``None``
    when the request could not be made at all.
    """
    kwargs: dict[str, Any] = {"headers": headers or {}}
    if verify is not None:
        kwargs["verify"] = verify
    if body is not None:
        if form_data:
            kwargs["data"] = body
        else:
            kwargs["json"] = body
    try:
        resp = requests.request(method, url, timeout=timeout, **kwargs)
        parsed: Any = None
        try:
            parsed = resp.json() if resp.text else None
        except ValueError:
            parsed = resp.text
        return (resp.status_code, parsed)
    except requests.exceptions.RequestException as exc:
        log(f"Request error on {method} {url}: {exc}")
        return (None, None)


def wait_for_service(
    url: str,
    *,
    timeout: int = 300,
    interval: int = 5,
    prefix: str | None = None,
) -> None:
    """Poll *url* every *interval* seconds until it returns 200.

    Raises ``SystemExit(1)`` if *timeout* is reached.
    """
    log(f"Waiting for {url} …", prefix=prefix)
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            status, _ = request(url, timeout=min(30, remaining))
            if status == 200:
                log("Service is ready.", prefix=prefix)
                return
        except Exception as exc:
            log(f"  Health check exception (attempt {attempt}): {exc}", prefix=prefix)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        log(
            f"  Not ready yet (attempt {attempt}) — sleeping up to {interval}s …",
            prefix=prefix,
        )
        time.sleep(min(interval, remaining))
    log(f"ERROR: Service did not become ready after {timeout} seconds. Aborting.", prefix=prefix)
    raise SystemExit(1)
