"""Client-owned selector preferences; server permissions remain authoritative."""

import json
from dataclasses import dataclass
from pathlib import Path

from keycloak_users.profile import SafeError, text


@dataclass(frozen=True)
class OnboardingPolicy:
    """Choose default and disabled group paths without granting any authority."""

    default_groups: tuple[str, ...] = ()
    disabled_groups: tuple[str, ...] = ()


def load_policy(path: Path) -> OnboardingPolicy:
    """Read bounded non-secret selector settings before connecting to a realm."""
    with path.open("rb") as source:
        raw = source.read(65_537)
    if len(raw) > 65_536:
        raise SafeError("Onboarding policy exceeds 64 KiB.")
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {"default_groups", "disabled_groups"}:
        raise SafeError("Policy requires default_groups and disabled_groups lists.")
    for paths in data.values():
        if not isinstance(paths, list) or len(paths) > 1000:
            raise SafeError("Policy group paths must be bounded lists.")
        for path_value in paths:
            if not text(path_value).startswith("/"):
                raise SafeError("Policy groups require absolute group paths.")
        if len(set(paths)) != len(paths):
            raise SafeError("Policy group paths must be unique.")
    if set(data["default_groups"]) & set(data["disabled_groups"]):
        raise SafeError("Default groups cannot be disabled.")
    return OnboardingPolicy(tuple(data["default_groups"]), tuple(data["disabled_groups"]))
