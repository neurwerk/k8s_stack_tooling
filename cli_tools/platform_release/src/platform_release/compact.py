"""Edit release prose without generating summaries or discarding authored instructions."""

from __future__ import annotations

import difflib
import re
import sys

from platform_release import main as m

GENERATED = {
    "> TODO: Replace every TODO with reviewed release-specific evidence.",
    "- TODO: Replace this scaffold with reviewed release notes.",
    "- TODO: Describe exact compatibility and recovery behavior.",
}
HEADINGS = {
    "Support",
    "Prerequisites",
    "Client Actions",
    "Breaking Changes",
    "Stateful And API Effects",
    "Pre-Deployment Checks",
    "Post-Deployment Checks",
    "Recovery",
    "Exclusions",
}


def clean(text: str, *, migration: bool = False) -> str:
    """Remove only recognized scaffold lines outside fenced or indented code."""
    lines: list[tuple[str, bool]] = []
    fence = ""
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if re.fullmatch(
                r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line
            ):
                fence = ""
            lines.append((line, True))
            continue
        if marker:
            fence = marker[1]
            lines.append((line, True))
            continue
        value = line.rstrip()
        if value in GENERATED:
            continue
        lines.append((line, False))
    # Only known scaffold headings may disappear; unknown authored structure stays.
    headings = (
        {f"## {name}" for name in HEADINGS}
        if migration
        else {"### Compatibility", "### TODO: Curate Changes"}
    )
    for index in range(len(lines) - 1, -1, -1):
        line, protected = lines[index]
        if protected or line.rstrip() not in headings:
            continue
        level = len(line) - len(line.lstrip("#"))
        end = next(
            (
                i
                for i in range(index + 1, len(lines))
                if not lines[i][1] and re.match(r"^#{1," + str(level) + r"} ", lines[i][0])
            ),
            len(lines),
        )
        body = lines[index + 1 : end]
        replacement = _empty_scaffold(body, recovery=line.rstrip() == "## Recovery")
        if replacement is not None:
            lines[index + 1 : end] = replacement
        if replacement == []:
            lines.pop(index)
    return "".join(line for line, _ in lines)


def _empty_scaffold(
    body: list[tuple[str, bool]], *, recovery: bool
) -> list[tuple[str, bool]] | None:
    """Recognize entire empty scaffold bodies, never literal values within authored prose."""
    if any(protected for _, protected in body):
        return None
    entries = [line.rstrip() for line, _ in body if line.strip()]
    if not entries or (
        len(entries) == 1 and re.fullmatch(r"(?:- )?(?:n/a|NULL|TODO\.)", entries[0], re.IGNORECASE)
    ):
        return []
    # This exact two-line pattern is the old generator's Recovery scaffold.
    if (
        recovery
        and len(entries) == 2
        and entries[1] == "TODO."
        and re.fullmatch(
            r"Recovery classification: (?:Forward fix|Configuration revert|"
            r"Component native restore|Replacement restore)\.",
            entries[0],
        )
    ):
        return [(line, protected) for line, protected in body if line.rstrip() != "TODO."]
    return None


def multiline(prompt: m.Prompt, message: str) -> str:
    """Collect plain Markdown; a single period finishes, blank input keeps existing notes."""
    sys.stdout.write(message + "\nEnter Markdown, then a line containing only . to finish.\n")
    lines = []
    while True:
        line = prompt.ask("> ")
        if line == "." or (not lines and not line):
            break
        lines.append(line)
    text = "\n".join(lines).strip()
    return "" if re.fullmatch(r"(?:- )?(?:none|n/a|null)", text, re.IGNORECASE) else text


def section_bounds(changelog: str, version: str) -> tuple[int, int]:
    """Locate the selected release without treating code examples as Markdown headings."""
    start = None
    offset = 0
    fence = ""
    for line in changelog.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if re.fullmatch(
                r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line
            ):
                fence = ""
        elif marker:
            fence = marker[1]
        elif line.startswith("## ["):
            if start is not None:
                return start, offset
            if re.match(r"^## \[" + re.escape(version) + r"\](?:\s|$)", line):
                start = offset + len(line)
        offset += len(line)
    if start is None:
        return offset, offset
    return start, offset


def draft(
    migration: str, changelog: str, scaffold: str, version: str, prompt: m.Prompt
) -> tuple[str, str]:
    """Offer conservative conversion and a compact human-authored notes editor."""
    start, end = section_bounds(changelog, version)
    body = changelog[start:end]
    cleaned_migration = clean(migration, migration=True)
    cleaned_body = clean(body)
    if (cleaned_migration, cleaned_body) != (migration, body):
        sys.stdout.write(
            "\nGenerated scaffold conversion preview (technical metadata stays internal):\n"
        )
        for name, old, new in (
            ("migration", migration, cleaned_migration),
            ("release notes", body, cleaned_body),
        ):
            sys.stdout.write(
                "".join(
                    difflib.unified_diff(
                        old.splitlines(True), new.splitlines(True), fromfile=name, tofile=name
                    )
                )
            )
        if prompt.ask("Remove only these generated placeholders? [y/N]: ").lower() in ("y", "yes"):
            migration, body = cleaned_migration, cleaned_body
    sys.stdout.write("\nExisting release notes:\n" + (body.strip() or "(No entries yet.)") + "\n")
    if prompt.ask("Edit the main release notes? [y/N]: ").lower() in ("y", "yes"):
        replacement = multiline(prompt, "Write the release notes. Blank keeps the current notes.")
        if replacement:
            body = replacement + "\n"
    if prompt.ask("Add special notes or upgrade instructions? [y/N]: ").lower() in ("y", "yes"):
        special = multiline(prompt, "Special notes (optional; blank means None):")
        if special:
            body = body.rstrip() + "\n\n" + special + "\n"
    if body and start == len(changelog) and start == end:
        body = f"\n## [{version}]\n\n{body}"
    return migration, changelog[:start] + body + changelog[end:]
