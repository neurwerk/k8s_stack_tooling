import io
from collections.abc import Callable
from pathlib import Path
from typing import Any
from wsgiref.validate import validator

import pytest
from pydantic import ValidationError

from k8s_stack_tooling.maintenance.main import (
    ASSET_PREFIX,
    MAX_LOGO_BYTES,
    MaintenanceServer,
    SafeLogger,
    Settings,
    create_app,
    load_logo,
)


class UnreadBody(io.BytesIO):
    def read(self, *args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("Request bodies must not be read")

    def readline(self, *args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("Request bodies must not be read")


def request(
    app: Callable[..., Any], path: str = "/", method: str = "GET"
) -> tuple[str, dict[str, str], bytes]:
    response = []

    def start_response(
        status: str, headers: list[tuple[str, str]], *args: Any
    ) -> Callable[[bytes], None]:
        response.append((status, dict(headers)))
        return lambda data: None

    result = validator(app)(
        {
            "REQUEST_METHOD": method,
            "SCRIPT_NAME": "",
            "PATH_INFO": path,
            "QUERY_STRING": "token=secret-query",
            "HTTP_COOKIE": "session=secret-cookie",
            "HTTP_AUTHORIZATION": "Bearer secret-token",
            "CONTENT_LENGTH": "999999999999",
            "SERVER_NAME": "localhost",
            "SERVER_PORT": "8080",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
            "wsgi.input": UnreadBody(b"secret-body"),
            "wsgi.errors": io.StringIO(),
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
        },
        start_response,
    )
    try:
        body = b"".join(result)
    finally:
        close = getattr(result, "close", None)
        if callable(close):
            close()
    return *response[0], body


@pytest.mark.filterwarnings("ignore:Unknown REQUEST_METHOD.*CUSTOM")
@pytest.mark.parametrize(
    "method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CUSTOM"]
)
def test_maintenance_all_methods(method: str, caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(Settings(company_name='<script>alert("x")</script>', retry_after=42))
    status, headers, body = request(app, "/any/application", method)
    assert status == "503 Service Unavailable"
    assert headers["Retry-After"] == "42"
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Platform-Maintenance"] == "true"
    assert headers["X-Content-Type-Options"] == "nosniff"
    if method == "HEAD":
        assert body == b""
    else:
        assert b"Temporarily unavailable" in body
        assert b"&lt;script&gt;" in body
        assert b"<script>" not in body
        assert len(body) == int(headers["Content-Length"])
    assert "secret" not in caplog.text
    assert b"secret" not in body


@pytest.mark.parametrize(
    "path",
    [
        "/_maintenance/healthz",
        *[
            ASSET_PREFIX + name
            for name in [
                "maintenance.css",
                "Inter-Regular.ttf",
                "Inter-SemiBold.ttf",
                "logo_black.png",
            ]
        ],
    ],
)
def test_health_assets_and_head(path: str) -> None:
    app = create_app()
    status, headers, body = request(app, path)
    assert status == "200 OK"
    assert len(body) == int(headers["Content-Length"])
    assert "Retry-After" not in headers
    head_status, head_headers, head_body = request(app, path, "HEAD")
    assert (head_status, head_headers, head_body) == (status, headers, b"")


@pytest.mark.parametrize(
    "path",
    [
        "/healthz",
        ASSET_PREFIX + "missing",
        ASSET_PREFIX + "../../main.py",
        ASSET_PREFIX + "%2e%2e/main.py",
        ASSET_PREFIX + "OFL.txt",
        "/etc/passwd",
    ],
)
def test_unknown_paths_do_not_serve_files(path: str) -> None:
    assert request(create_app(), path)[0].startswith("503")


def test_assets_reject_write_methods() -> None:
    assert request(create_app(), ASSET_PREFIX + "maintenance.css", "POST")[0].startswith("503")


@pytest.mark.parametrize(
    "content",
    [
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.invalid/x"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><path fill="url(https://example.invalid/x)"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><style>@import "x";</style></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>',
        '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg>&x;</svg>',
        '<?xml-stylesheet href="https://example.invalid/x"?><svg/>',
        '<svg xmlns="http://www.w3.org/2000/svg"><animate attributeName="href"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><path fill="u\\72l(x)"/></svg>',
        "<svg",
    ],
)
def test_unsafe_logos_are_omitted(tmp_path: Path, content: str) -> None:
    logo = tmp_path / "logo.svg"
    logo.write_text(content)
    app = create_app(Settings(logo_path=logo))
    assert request(app, ASSET_PREFIX + "company-logo")[0].startswith("503")
    assert b'class="company-logo"' not in request(app)[2]


def test_safe_logo_is_preloaded(tmp_path: Path) -> None:
    logo = tmp_path / "logo.svg"
    data = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path d="M0 0"/></svg>'
    logo.write_bytes(data)
    app = create_app(Settings(logo_path=logo))
    logo.unlink()
    status, headers, body = request(app, ASSET_PREFIX + "company-logo")
    assert status == "200 OK"
    assert headers["Content-Type"] == "image/svg+xml"
    assert body == data


def test_png_and_bounded_logo_loading(tmp_path: Path) -> None:
    logo = tmp_path / "logo.png"
    assert load_logo(logo) is None
    logo.mkdir()
    assert load_logo(logo) is None
    logo.rmdir()
    logo.write_bytes(b"not png")
    assert load_logo(logo) is None
    logo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * MAX_LOGO_BYTES)
    assert load_logo(logo) is None
    bundled = Path(__file__).parents[2] / "src/k8s_stack_tooling/maintenance/assets/logo_black.png"
    logo.write_bytes(bundled.read_bytes())
    assert load_logo(logo) == (logo.read_bytes(), "image/png")


def test_settings_and_server_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAINTENANCE_COMPANY_NAME", "Example & Co")
    monkeypatch.setenv("MAINTENANCE_RETRY_AFTER", "120")
    assert Settings().company_name == "Example & Co"
    assert Settings().retry_after == 120
    for path in ["relative.png", "/tmp/logo.html"]:
        with pytest.raises(ValidationError):
            Settings(logo_path=Path(path))
    with pytest.raises(ValidationError):
        Settings(retry_after=-1)
    monkeypatch.setenv("GUNICORN_CMD_ARGS", "--access-logfile - --bind 127.0.0.1:9000")
    server = MaintenanceServer()
    assert server.cfg is not None
    assert server.cfg.bind == ["0.0.0.0:8080"]
    assert server.cfg.accesslog is None
    assert server.cfg.worker_class_str == "sync"
    assert server.cfg.limit_request_fields == 32
    assert server.cfg.limit_request_field_size == 4096
    assert server.cfg.limit_request_line == 4094
    assert server.cfg.timeout == 15


def test_parser_errors_are_redacted(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    logger = SafeLogger(MaintenanceServer().cfg)
    logger.error("secret-token %s", "secret-cookie")
    logger.warning("secret-query")
    logger.exception("secret-body")
    output = capsys.readouterr().err + caplog.text
    assert "details suppressed" in output
    assert "secret" not in output
