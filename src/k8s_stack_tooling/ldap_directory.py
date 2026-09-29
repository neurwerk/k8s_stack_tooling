"""Read directory names from inside the cluster; never print bind credentials."""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
from urllib.parse import urlsplit

from ldap3 import NONE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException


def main() -> int:
    """Bind once and emit one JSON record per entry for the workstation CLI."""
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode not in ("users", "groups", "both"):
        print("Choose users, groups or both", file=sys.stderr)
        return 2
    try:
        url = os.environ["LDAP_URL"]
        parsed = urlsplit(url)
        allowed = (parsed.scheme, parsed.port) in (
            {("ldaps", 636), ("ldap", 389)}
            if os.environ.get("LDAP_ALLOW_INSECURE") == "true"
            else {("ldaps", 636)}
        )
        if (
            not allowed
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or re.search(r"\s", url)
        ):
            raise ValueError("Invalid LDAP connection settings")
        username_attribute = os.environ["LDAP_USERNAME_ATTRIBUTE"]
        if username_attribute not in ("sAMAccountName", "userPrincipalName"):
            raise ValueError("Unsupported username attribute")
        tls = (
            Tls(validate=ssl.CERT_REQUIRED, ca_certs_file="/run/ldap-ca/ca.crt")
            if parsed.scheme == "ldaps"
            else None
        )
        server = Server(
            parsed.hostname,
            port=parsed.port,
            use_ssl=bool(tls),
            tls=tls,
            connect_timeout=5,
            get_info=NONE,
        )
        with Connection(
            server,
            user=os.environ["LDAP_BIND_DN"],
            password=os.environ["LDAP_BIND_CREDENTIAL"],
            read_only=True,
            auto_referrals=False,
            raise_exceptions=True,
            receive_timeout=10,
            auto_bind=True,
        ) as connection:
            for kind, base, query, attribute in (
                (
                    "user",
                    os.environ["LDAP_USERS_DN"],
                    "(&(objectCategory=person)(objectClass=user))",
                    username_attribute,
                ),
                ("group", os.environ["LDAP_GROUPS_DN"], "(objectCategory=group)", "cn"),
            ):
                if mode != "both" and mode != f"{kind}s":
                    continue
                for entry in connection.extend.standard.paged_search(
                    search_base=base,
                    search_filter=query,
                    search_scope=SUBTREE,
                    attributes=[attribute],
                    paged_size=500,
                    generator=True,
                ):
                    if entry.get("type") != "searchResEntry":
                        continue
                    value = entry.get("attributes", {}).get(attribute)
                    if isinstance(value, list):
                        value = value[0] if len(value) == 1 else None
                    if isinstance(value, str) and value:
                        print(json.dumps({"kind": kind, "name": value}), flush=True)
    except (KeyError, ValueError, LDAPException, OSError):
        print("LDAP query failed; check configuration, connection and bind access", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
