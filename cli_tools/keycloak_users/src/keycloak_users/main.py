"""Interactive account onboarding with default-No write boundaries."""

import argparse
import re
import sys
from typing import cast

import httpx
import questionary

from keycloak_users.auth import BrowserSession, Json
from keycloak_users.client import Accounts, Catalog, Progress
from keycloak_users.profile import Profile, SafeError, load_profiles, save_profile, text

ACTION_LABELS = {
    "VERIFY_EMAIL": "Verify email",
    "UPDATE_PASSWORD": "Set password",
    "CONFIGURE_TOTP": "Configure authenticator (TOTP)",
}
NORMAL_USER_GROUPS = {
    "/access/neurwerk-librechat-users": "LibreChat",
    "/access/neurwerk-mcp-all-users": "MCP",
    "/access/neurwerk-llm-all-users": "LLM",
    "/access/neurwerk-studio-users": "Studio",
}


def say(message: str) -> None:
    """Write only deliberately constructed operator messages, never upstream errors."""
    sys.stdout.write(message + "\n")


def answer(question: questionary.Question) -> object:
    """Treat an interrupted question as cancellation, never approval or an empty choice."""
    result = question.unsafe_ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def prompt(label: str, default: str = "") -> str:
    """Read bounded printable user input."""
    return text(text(answer(questionary.text(f"{label.rstrip(':? ')}:", default=default))).strip())


def confirm(label: str) -> bool:
    """Require explicit consent, with a harmless No default."""
    return answer(questionary.confirm(f"{label.rstrip(':? ')}:", default=False)) is True


def checklist(label: str, values: dict[str, str], defaults: tuple[str, ...] = ()) -> list[str]:
    """Require explicit checkbox submission while preserving stable IDs as values."""
    choices = [
        questionary.Choice(label, value=value, checked=value in defaults)
        for value, label in values.items()
    ]
    if not choices:
        say(f"{label}: none available.")
        return []
    return cast(list[str], answer(questionary.checkbox(f"{label.rstrip(': ')}:", choices=choices)))


def normal_group_ids(catalog: Catalog) -> list[str]:
    """Resolve the exact preset before collecting account details or making writes."""
    groups: list[str] = []
    for path in NORMAL_USER_GROUPS:
        matches = [group_id for group_id, candidate in catalog.groups.items() if candidate == path]
        if len(matches) != 1:
            raise SafeError(f"Required default group missing or ambiguous: {path}")
        groups.extend(matches)
    return groups


def select_groups(catalog: Catalog, *, normal_user: bool) -> list[str]:
    """Resolve the exact normal-user preset; disable Dify without changing existing grants."""
    defaults = normal_group_ids(catalog) if normal_user else []
    if normal_user:
        say("User type: Normal user")
    choices = []
    selectable: set[str] = set()
    for group_id, path in catalog.groups.items():
        dify = any(part.startswith(("dify-", "neurwerk-dify-")) for part in path.split("/"))
        label = "Dify" if dify else NORMAL_USER_GROUPS.get(path, "Group")
        choices.append(
            questionary.Choice(
                f"{label}: {path}",
                value=group_id,
                checked=group_id in defaults,
                disabled="Disabled for now" if dify else None,
            )
        )
        if not dify:
            selectable.add(group_id)
    say("Dify: Disabled in this selector; existing memberships are unchanged.")
    selected = cast(list[str], answer(questionary.checkbox("User groups:", choices=choices)))
    if not set(selected) <= selectable:
        raise SafeError("Group selection includes an unavailable or disabled group.")
    return selected


def setup_profile() -> Profile | None:
    """Explain exact manual client settings, never provisioning client or role resources."""
    name = prompt("Local non-secret profile name")
    server = prompt("HTTPS Keycloak server URL (optional /auth prefix)")
    realm = prompt("Explicit target realm")
    client = prompt("Dedicated public client ID", "keycloak-users")
    port = int(prompt("Fixed loopback port", "8765"))
    ca = cast(
        str, answer(questionary.text("Optional absolute CA bundle path (blank for system trust):"))
    )
    profile = Profile(name, server, realm, client, port, ca.strip() or None)
    say(
        f"Admin Console: {profile.server_url.rstrip('/')}/admin/{profile.realm}/console/\n"
        f"Realm: {profile.realm}\n"
        "Navigation: Clients > Create client\n"
        "Client type: OpenID Connect\n"
        f"Client ID: {profile.client_id}\n"
        "Existing application clients: Do not modify\n\n"
        "Navigation: Settings > Capability config\n"
        "Client authentication: OFF\n"
        "Standard flow: ON\n"
        "Implicit flow: OFF\n"
        "Direct access grants: OFF\n"
        "Device authorization: OFF\n"
        "Service accounts: OFF\n"
        "CIBA: OFF\n"
        "PKCE method: S256\n"
        "Older-version fallback: Advanced > "
        "Proof Key for Code Exchange Code Challenge Method: S256\n\n"
        "Navigation: Settings > Login settings\n"
        f"Valid redirect URIs: {profile.callback}\n"
        "Web origins: Empty\n"
        "Wildcard redirects: None\n"
        "Signing algorithm: RS256\n\n"
        "Navigation: Client scopes > dedicated scope > Scope\n"
        "Full Scope Allowed: ON\n"
        "Standard roles client scope: Default\n"
        "Operator: Sign in with existing approved administrative permissions\n"
        "Required realm-management roles: manage-users and view-realm (or realm-admin)\n"
        "Redundant roles: Separate query-users, view-users and query-groups are not required "
        "with manage-users\n"
        "Additional operator group: Not needed if existing grants suffice\n"
        "Managed role/group grants: Remain Git-owned; no direct human role grants\n"
        "Authority: manage-users is broad realm authority, NOT CLI-constrained delegation\n"
        "Read visibility: Realm-wide; filtered reads cannot prove absence\n"
        "Optional hardening: Full Scope Allowed: OFF, with explicitly permitted scope mappings. "
        "See README; scope mappings do not grant roles.\n\n"
        "Navigation: Realm settings > Email\n"
        "SMTP: Configure and test independently\n"
        "Doctor: Verifies token authority and onboarding prerequisites without writes\n"
        "Limits: Fine-grained-only authorization is unsupported; SMTP delivery is not tested"
    )
    return profile if confirm("Have you completed and reviewed the manual configuration?") else None


def doctor(accounts: Accounts) -> Catalog:
    """Require verified authority, probe reads, and report SMTP without making writes."""
    accounts.connection.session.require_management()
    say("Token verification: Passed (signature, issuer, expiry, audience, client and subject)")
    say("User-management authority: Passed (verified standard realm-management roles)")
    catalog = accounts.catalog()
    accounts.users()
    say("Read probes passed: users, group hierarchy, actions, realm defaults, user storage.")
    say(
        "SMTP configuration: "
        + (
            "Host and sender configured"
            if catalog.smtp_ready
            else "Missing host or sender; email is unavailable"
        )
    )
    say("Preflight limits: Server policy may change; SMTP delivery has not been tested.")
    return catalog


def creation_preflight(catalog: Catalog) -> None:
    """Stop before identity prompts if the selected realm cannot support local onboarding."""
    blockers = []
    if catalog.federation:
        blockers.append(
            "Active or unknown-state user-storage provider; local creation is disabled."
        )
    if catalog.duplicate_emails:
        blockers.append("Realm permits duplicate emails; enable unique email addresses.")
    if not {"VERIFY_EMAIL", "UPDATE_PASSWORD"} <= set(catalog.actions):
        blockers.append("Required actions: Enable VERIFY_EMAIL and UPDATE_PASSWORD in the realm.")
    try:
        normal_group_ids(catalog)
    except SafeError as exc:
        blockers.append(str(exc))
    if blockers:
        raise SafeError(
            "Creation preflight: Blocked\n  "
            + "\n  ".join(blockers)
            + "\nResult: No account details requested or writes attempted."
        )
    say(
        "Creation preflight: Passed "
        "(local accounts, unique emails, required actions, four preset groups)"
    )


def identity(accounts: Accounts, operation: str) -> Json:
    """Collect a new identity or explicitly confirm an existing local account."""
    if operation == "create":
        first, last = prompt("First name"), prompt("Last name")
        email = prompt("Email")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise SafeError("Enter a valid email address.")
        return {
            "firstName": first,
            "lastName": last,
            "email": email,
            "username": prompt("Editable username", email),
        }
    user = accounts.existing(prompt("Exact existing local username"))
    say(
        f"Existing account ID: {text(user.get('id'))}; username: {text(user.get('username'))}; "
        f"email: {text(user.get('email'))}; enabled: {user.get('enabled') is True}"
    )
    if prompt("Type the existing username to confirm this identity") != user["username"]:
        raise SafeError("Identity confirmation did not match; no writes performed.")
    return user


def workflow(accounts: Accounts, operation: str, catalog: Catalog) -> None:
    """Show complete intended stages before any mutation, then expose partial progress."""
    accounts.connection.session.require_management()
    if operation == "create":
        creation_preflight(catalog)
    elif operation == "send-actions-email":
        accounts.require_email(catalog)
    user = identity(accounts, operation)
    available = (
        catalog.actions
        if operation == "create"
        else tuple(
            action for action in catalog.actions if action in user.get("requiredActions", [])
        )
    )
    actions = checklist(
        "Required actions (enrollment, NOT login MFA enforcement)",
        {action: f"{ACTION_LABELS.get(action, 'Action')}: {action}" for action in available},
        tuple(
            dict.fromkeys(
                (
                    "VERIFY_EMAIL",
                    "UPDATE_PASSWORD",
                    *(
                        value.removeprefix("required action: ")
                        for value in catalog.defaults
                        if value.startswith("required action: ")
                    ),
                )
            )
        )
        if operation == "create"
        else available,
    )
    groups = (
        []
        if operation == "send-actions-email"
        else select_groups(catalog, normal_user=operation == "create")
    )
    send_email = bool(actions) and (
        operation == "send-actions-email"
        or answer(questionary.confirm("Send onboarding email:", default=catalog.smtp_ready)) is True
    )
    if send_email:
        accounts.require_email(catalog)
    say(
        "All selected groups may grant privileged, inherited or composite permissions. "
        "Effective grants are not enumerated here; review managed mappings before approval."
    )
    if groups:
        say(
            "Privileged access: CONFIGURE_TOTP enrollment is recommended when enabled. "
            "Login MFA still depends on realm authentication flows."
        )
    say(
        f"Realm: {accounts.realm}\nOperation: {operation}\n"
        f"Username: {text(user.get('username'))}\nEmail: {text(user.get('email'))}"
    )
    say("Selected groups: " + (", ".join(catalog.groups[group] for group in groups) or "none"))
    say("Realm defaults (apply independently): " + "; ".join(catalog.defaults))
    say("Actions: " + (", ".join(actions) or "none; no email will be requested"))
    say("Request onboarding email: " + ("yes" if send_email else "no"))
    say(
        "Email link lifespan: 604800 seconds (seven days). "
        "Accepted request does not prove delivery."
    )
    if not confirm("Proceed with these independent writes? No automatic rollback or retry"):
        say("Cancelled; no writes performed.")
        return
    progress = Progress()
    try:
        if operation == "create":
            accounts.create(user, groups, actions, catalog, progress, send_email=send_email)
        else:
            accounts.resume(user, groups, actions, catalog, progress, send_email=send_email)
    finally:
        say(progress.summary())


def select_target(
    command: str | None, profile_name: str | None
) -> tuple[str | None, Profile | None]:
    """Offer first-run setup or an explicit named target; never select a realm silently."""
    if command == "setup":
        return command, setup_profile()
    profiles = load_profiles()
    if profile_name is not None:
        if profile_name not in profiles:
            raise SafeError("Select an existing named profile with --profile, or run setup.")
        return command, profiles[profile_name]
    choices = [
        questionary.Choice(
            f"{name}: {profile.issuer}",
            value=name,
        )
        for name, profile in profiles.items()
    ]
    choices.extend(
        [
            questionary.Choice("Setup: Add a Keycloak connection", value=True),
            questionary.Choice("Exit: Cancel", value=False),
        ]
    )
    selected = answer(
        questionary.select("Select target:" if profiles else "First-time setup:", choices=choices)
    )
    if selected is False:
        return command, None
    if selected is True:
        return "setup", setup_profile()
    return command, profiles[text(selected)]


def run(command: str | None, profile_name: str | None) -> None:
    """Authenticate anew for one explicitly selected realm and discard the session afterward."""
    command, profile = select_target(command, profile_name)
    if profile is None:
        say("Cancelled; no connection or profile change.")
        return
    session = BrowserSession(profile)
    try:
        say(f"Opening browser for {profile.issuer}. Ctrl-C cancels; login timeout is 180 seconds.")
        session.login()
        say("Login: Successful")
        accounts = Accounts(session)
        catalog = doctor(accounts)
        if command in ("setup", "doctor"):
            creation_preflight(catalog)
        if command == "setup":
            if confirm(f"Save non-secret profile {profile.name} (replace it if it exists)?"):
                save_profile(profile)
                say("Non-secret profile saved; browser tokens were not saved.")
            return
        if command == "doctor":
            return
        operation = command or cast(
            str,
            answer(
                questionary.select(
                    "Operation:",
                    choices=[
                        questionary.Choice("Cancel: Exit without changes", value="cancel"),
                        questionary.Choice("Create: New user", value="create"),
                        questionary.Choice("Resume: Existing user onboarding", value="resume"),
                        questionary.Choice(
                            "Email: Retry onboarding invitation", value="send-actions-email"
                        ),
                    ],
                )
            ),
        )
        if operation != "cancel":
            workflow(accounts, operation, catalog)
    finally:
        session.close()


def main() -> int:
    """Run the CLI without exposing tracebacks, callback URLs or server response bodies."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", nargs="?", choices=["setup", "doctor", "create", "resume", "send-actions-email"]
    )
    parser.add_argument("--profile", help="Explicit local non-secret profile name")
    args = parser.parse_args()
    try:
        run(args.command, args.profile)
    except KeyboardInterrupt:
        say(
            "Cancelled. Inspect any reported partial progress "
            "before an explicit resume or email retry."
        )
        return 130
    except SafeError as exc:
        say(str(exc))
        return 1
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
        say(
            "Operation failed: transport, configuration or response validation error. "
            "No automatic retry; inspect any reported partial progress."
        )
        return 1
    return 0
