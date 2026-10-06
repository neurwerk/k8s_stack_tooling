"""Manage only the catalog-owned native ContextForge Vault token."""

from __future__ import annotations

from openbao_stack_setup.client import JsonValue, OpenBaoClient, OpenBaoError

TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
ROTATE_BEFORE_SECONDS = 7 * 24 * 60 * 60
_POLICY = "contextforge-oauth"
_PATH = "contextforge/internal"
_ROTATION_HELP = (
    "ContextForge Vault token is expired or unverifiable. Restore OpenBao connectivity, "
    "then use the two-custodian stack-setup reconcile --rotate-contextforge-token "
    "operation after suspending its HelmRelease and stopping all ContextForge Pods. "
    "Do not substitute a root, ESO, default-policy, or indefinitely lived token."
)


def reconcile_contextforge_token(
    client: OpenBaoClient, binding: dict[str, str], *, rotate: bool = False
) -> int:
    """Reuse a verified token, or issue one on first setup or explicit stopped rotation."""
    record = client.read_secret(_PATH)
    if record is None:
        raise OpenBaoError("ContextForge internal credentials must be reconciled first")
    metadata = {**binding, "purpose": _POLICY}
    remaining = _reuse_or_revoke(client, record.values, metadata, rotate=rotate)
    if remaining is not None:
        return remaining
    try:
        token = client.create_orphan_token(_POLICY, TOKEN_TTL_SECONDS, metadata)
    except OpenBaoError:
        raise OpenBaoError(
            "ContextForge token issuance outcome is unknown. Keep the application stopped; "
            "have an authorized operator inspect catalog-bound token accessors before retrying."
        ) from None
    persisted = False
    try:
        info = client.lookup_token(token)
        if info is None:
            raise OpenBaoError(_ROTATION_HELP)
        ttl = _validate_token(info, token, metadata)
        if ttl <= 0:
            raise OpenBaoError(_ROTATION_HELP)
        client.write_secret(_PATH, {**record.values, "vaultToken": token}, record.version)
        persisted = True
        return ttl
    finally:
        if not persisted:
            # Also covers interruption and an uncertain CAS write: revoke rather than leak a token.
            client.revoke_token(token)


def _reuse_or_revoke(
    client: OpenBaoClient,
    values: dict[str, JsonValue],
    metadata: dict[str, str],
    *,
    rotate: bool,
) -> int | None:
    if "vaultToken" not in values:
        return None
    existing = values["vaultToken"]
    if not isinstance(existing, str) or not existing.strip():
        raise OpenBaoError(_ROTATION_HELP)
    try:
        info = client.lookup_token(existing)
    except OpenBaoError:
        raise OpenBaoError(_ROTATION_HELP) from None
    if info is not None:
        ttl = _validate_token(info, existing, metadata)
        if not rotate and ttl > 0:
            return ttl
    if not rotate:
        raise OpenBaoError(_ROTATION_HELP)
    # A failed lookup is not enough to rotate: privileged revocation must succeed too.
    client.revoke_token(existing)
    revoked = client.lookup_token(existing)
    if revoked is not None and _validate_token(revoked, existing, metadata) > 0:
        raise OpenBaoError(
            "ContextForge token revocation is not confirmed; keep it stopped and retry"
        )
    return None


def _validate_token(info: dict[str, JsonValue], token: str, metadata: dict[str, str]) -> int:
    ttl = info.get("ttl")
    if (
        info.get("id") != token
        or info.get("policies") != [_POLICY]
        or info.get("orphan") is not True
        or info.get("renewable") is not False
        or info.get("type") != "service"
        or info.get("path") != "auth/token/create-orphan"
        or info.get("meta") != metadata
        or info.get("entity_id") not in (None, "")
        or info.get("identity_policies") not in (None, [])
        or info.get("external_namespace_policies") not in (None, {})
        or info.get("explicit_max_ttl") != TOKEN_TTL_SECONDS
        or info.get("creation_ttl") != TOKEN_TTL_SECONDS
        or info.get("num_uses") != 0
        or info.get("period", 0) != 0
        or type(ttl) is not int
        or ttl > TOKEN_TTL_SECONDS
    ):
        raise OpenBaoError(
            "ContextForge token does not match the approved policy, binding, or 30-day lifetime. "
            "Keep the application stopped and obtain authorized operator repair; "
            "the tool will not rotate an unrelated or overprivileged token."
        )
    return ttl
