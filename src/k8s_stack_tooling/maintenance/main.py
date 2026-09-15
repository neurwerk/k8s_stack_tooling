"""Fixed, preloaded WSGI responses; no authentication or upstream calls."""

import os
import re
import stat
from collections.abc import Callable
from html import escape
from pathlib import Path
from typing import Any

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring
from gunicorn.app.base import BaseApplication
from gunicorn.glogging import Logger
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ASSET_PREFIX = "/_maintenance/assets/"
MAX_LOGO_BYTES = 1024 * 1024
SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"
SVG_ELEMENTS = {
    "svg",
    "g",
    "path",
    "rect",
    "circle",
    "ellipse",
    "line",
    "polyline",
    "polygon",
    "title",
    "desc",
    "defs",
    "linearGradient",
    "radialGradient",
    "stop",
    "clipPath",
}
SVG_ATTRIBUTES = set(
    "id viewBox width height x y x1 y1 x2 y2 cx cy r rx ry d points fill "
    "stroke stroke-width opacity fill-opacity stroke-opacity transform "
    "fill-rule clip-rule clip-path offset stop-color stop-opacity "
    "gradientUnits gradientTransform spreadMethod version".split()
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAINTENANCE_")

    company_name: str = Field(default="neurwerk", max_length=512)
    logo_path: Path | None = None
    retry_after: int = Field(default=300, ge=0, le=2147483647)

    @field_validator("logo_path")
    @classmethod
    def absolute_logo(cls, value: Path | None) -> Path | None:
        if value is not None and (
            not value.is_absolute() or value.suffix.lower() not in {".png", ".svg"}
        ):
            raise ValueError("logo must be an absolute PNG or SVG path")
        return value


def load_logo(path: Path) -> tuple[bytes, str] | None:
    """Read only a bounded regular file; reject active or referencing SVG content."""
    try:
        # NONBLOCK avoids hanging on an accidentally mounted FIFO. Projected-volume
        # symlinks are allowed; only this operator-configured path is ever opened.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                return None
            data = file.read(MAX_LOGO_BYTES + 1)
        if not data or len(data) > MAX_LOGO_BYTES:
            return None
        if path.suffix.lower() == ".png":
            return (data, "image/png") if data.startswith(b"\x89PNG\r\n\x1a\n") else None
        # Forbid processing instructions as well as DTDs/entities. No CSS, image,
        # use, links, animation, foreignObject, event handlers or external URLs.
        text = data.decode("utf-8")
        text = re.sub(r"^\s*<\?xml\s[^?]*\?>", "", text, count=1)
        if "<?" in text or "<!" in text:
            return None
        root = fromstring(text, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        if root.tag != SVG_NAMESPACE + "svg":
            return None
        for element in root.iter():
            if element.tag not in {SVG_NAMESPACE + name for name in SVG_ELEMENTS}:
                return None
            for name, value in element.attrib.items():
                if name not in SVG_ATTRIBUTES:
                    return None
                # Only literal presentation values or local paint/clip references.
                clean = re.sub(r"url\(#[A-Za-z_][A-Za-z0-9_.-]*\)", "", value)
                if any(token in clean.lower() for token in ("url", ":", "\\", "@", "&")):
                    return None
        return data, "image/svg+xml"
    except (OSError, ValueError, DefusedXmlException, SyntaxError):
        return None


def create_app(settings: Settings | None = None) -> Callable[..., list[bytes]]:
    settings = settings or Settings()
    resources = Path(__file__).parent / "assets"
    assets = {
        ASSET_PREFIX + name: ((resources / name).read_bytes(), content_type)
        for name, content_type in {
            "maintenance.css": "text/css; charset=utf-8",
            "Inter-Regular.ttf": "font/ttf",
            "Inter-SemiBold.ttf": "font/ttf",
            "logo_black.png": "image/png",
        }.items()
    }
    logo = load_logo(settings.logo_path) if settings.logo_path else None
    company = escape(settings.company_name, quote=True)
    brand = f'<p class="brand-name">{company}</p>'
    if logo:
        assets[ASSET_PREFIX + "company-logo"] = logo
        brand = f'<img class="company-logo" src="{ASSET_PREFIX}company-logo" alt="{company}">'
    page = re.sub(
        r"\{\{(brand|company)\}\}",
        lambda match: {"brand": brand, "company": company}[match[1]],
        (resources / "maintenance.html").read_text(encoding="utf-8"),
    ).encode("utf-8")

    def application(environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        path = environ.get("PATH_INFO", "")
        status, body, content_type = "503 Service Unavailable", page, "text/html; charset=utf-8"
        if path == "/_maintenance/healthz":
            status, body, content_type = "200 OK", b"ok\n", "text/plain; charset=utf-8"
        elif path in assets and environ.get("REQUEST_METHOD") in {"GET", "HEAD"}:
            status = "200 OK"
            body, content_type = assets[path]
        headers = [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Platform-Maintenance", "true"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            (
                "Content-Security-Policy",
                "default-src 'none'; style-src 'self'; "
                "font-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'none'; sandbox allow-same-origin",
            ),
        ]
        if status.startswith("503"):
            headers.append(("Retry-After", str(settings.retry_after)))
        start_response(status, headers)
        return [] if environ.get("REQUEST_METHOD") == "HEAD" else [body]

    return application


class SafeLogger(Logger):
    """Gunicorn parser/worker errors can contain raw URLs and invalid headers."""

    def warning(self, msg: object, *args: object, **kwargs: Any) -> None:
        super().warning("Maintenance server warning (details suppressed)")

    def error(self, msg: object, *args: object, **kwargs: Any) -> None:
        super().error("Maintenance server error (details suppressed)")

    def exception(self, msg: object, *args: object, **kwargs: Any) -> None:
        self.error(msg)


class MaintenanceServer(BaseApplication):
    def load_config(self) -> None:
        assert self.cfg is not None
        # Do not load gunicorn.conf.py, command-line options or GUNICORN_CMD_ARGS.
        for name, value in {
            "bind": "0.0.0.0:8080",
            "workers": 2,
            "worker_class": "sync",
            "timeout": 15,
            "graceful_timeout": 15,
            "backlog": 128,
            "limit_request_line": 4094,
            "limit_request_fields": 32,
            "limit_request_field_size": 4096,
            "accesslog": None,
            "logger_class": SafeLogger,
            "loglevel": "info",
            "forwarded_allow_ips": "",
        }.items():
            self.cfg.set(name, value)

    def load(self) -> Callable[..., list[bytes]]:
        return create_app()


def main() -> None:
    MaintenanceServer().run()
