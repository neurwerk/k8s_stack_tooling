"""Strict non-secret workstation profiles."""

import json
import os
import re
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit


class SafeError(Exception):
    """An operator-facing error containing no upstream response or credentials."""


def text(value: object) -> str:
    """Require bounded, printable text before using it in paths or terminal output."""
    if not isinstance(value, str) or not value or len(value) > 512 or not value.isprintable():
        raise SafeError("Expected nonempty printable text (at most 512 characters).")
    return value


def identifier(value: object) -> str:
    """Require a single safe URL path segment or local profile name."""
    result = text(value)
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", result):
        raise SafeError("Invalid identifier; use letters, numbers, underscore, dot or hyphen.")
    return result


@dataclass(frozen=True)
class Profile:
    """An explicit realm and a fixed IPv4 loopback callback, never credentials."""

    name: str
    server_url: str
    realm: str
    client_id: str = "keycloak-users"
    port: int = 8765
    ca_path: str | None = None

    def __post_init__(self) -> None:
        """Reject ambiguous URLs, arbitrary redirects and malformed configuration."""
        for value in (self.name, self.realm, self.client_id):
            identifier(value)
        url = urlsplit(text(self.server_url))
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path not in ("", "/", "/auth", "/auth/")
            or "\\" in self.server_url
            or any(c.isspace() for c in self.server_url)
        ):
            raise SafeError("Server must be an HTTPS origin, optionally with /auth prefix.")
        try:
            _ = url.port
        except ValueError:
            raise SafeError("Invalid HTTPS server port.") from None
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise SafeError("Loopback port must be an integer between 1024 and 65535.")
        if self.ca_path is not None and not Path(text(self.ca_path)).is_absolute():
            raise SafeError("CA bundle path must be absolute.")

    @property
    def issuer(self) -> str:
        """Return the exact expected realm issuer."""
        return f"{self.server_url.rstrip('/')}/realms/{self.realm}"

    @property
    def callback(self) -> str:
        """Return the only permitted redirect URI."""
        return f"http://127.0.0.1:{self.port}/callback"


def config_path() -> Path:
    """Locate non-secret profiles without consulting cluster configuration."""
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    if not root.is_absolute():
        raise SafeError("XDG_CONFIG_HOME must be absolute.")
    return root / "keycloak-users" / "profiles.json"


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON keys instead of silently losing stored entries."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SafeError("Duplicate configuration key; repair profiles manually.")
        result[key] = value
    return result


def load_profiles() -> dict[str, Profile]:
    """Load every profile or fail; never partially accept a damaged file."""
    path = config_path()
    if not path.exists() and not path.is_symlink():
        return {}
    try:
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
            raise SafeError("Profile file must be a regular file, not a symlink.")
        if path.stat().st_mode & 0o077:
            raise SafeError("Profile file permissions must be 0600; repair before continuing.")
        data = json.loads(path.read_text(), object_pairs_hook=unique_object)
        if not isinstance(data, dict):
            raise SafeError("Profile configuration must be a JSON object.")
        profiles = {identifier(name): Profile(**value) for name, value in data.items()}
        if any(name != profile.name for name, profile in profiles.items()):
            raise SafeError("Profile name does not match its configuration key.")
    except (OSError, ValueError, TypeError):
        raise SafeError(
            "Cannot read profiles; repair malformed or inaccessible configuration."
        ) from None
    return profiles


def save_profile(profile: Profile) -> None:
    """Atomically preserve all profiles and save only the allowlisted dataclass fields."""
    profiles = load_profiles()
    profiles[profile.name] = profile
    path = config_path()
    temporary_files: list[Path] = []
    try:
        if path.parent.is_symlink():
            raise SafeError("Profile directory must not be a symlink.")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            temporary_files.append(temporary)
            temporary.chmod(0o600)
            json.dump({name: asdict(value) for name, value in profiles.items()}, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except OSError:
        raise SafeError("Cannot atomically save profile configuration.") from None
    finally:
        for temporary in temporary_files:
            temporary.unlink(missing_ok=True)
