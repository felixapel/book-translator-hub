#!/usr/bin/env python3
"""Validate the repository's durable documentation contract."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote


REPOSITORY = Path(__file__).resolve().parents[1]
DOCS = REPOSITORY / "docs"

CANONICAL_GUIDES = {
    "README.md",
    "install/authentik.md",
    "install/btctl.md",
    "install/community-applications.md",
    "install/kavita.md",
    "install/universal-hub.md",
    "maintainers/development.md",
    "maintainers/release.md",
    "operations/lifecycle.md",
    "operations/troubleshooting.md",
    "reference/architecture.md",
    "reference/compatibility.md",
    "reference/configuration.md",
    "decisions/README.md",
}

OBSOLETE_PATHS = {
    "docs/ARCHITECTURE.md",
    "docs/AUTHENTIK.md",
    "docs/COMPATIBILITY.md",
    "docs/DEPLOY_COMPOSE.md",
    "docs/DEPLOY_UNRAID.md",
    "docs/DEPLOY_UNRAID_CA.md",
    "docs/DEVELOPMENT.md",
    "docs/PRODUCTION_READINESS.md",
    "docs/RELEASE.md",
    "docs/TROUBLESHOOTING.md",
    "docs/launch",
}

LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
ADR_RE = re.compile(r"ADR-\d{3}-[a-z0-9-]+\.md$")
VERSION_RE = re.compile(
    r"(?<![A-Za-z0-9])v(\d+\.\d+\.\d+"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)"
    r"(?!(?:[A-Za-z0-9-]|\.[0-9A-Za-z-]))"
)
VALID_ADR_STATUSES = {"Accepted", "Deprecated", "Proposed", "Superseded"}


def _relative_markdown_files(repository: Path) -> list[Path]:
    roots = [
        repository / "README.md",
        repository / "CONTRIBUTING.md",
        repository / "SECURITY.md",
        repository / "CODE_OF_CONDUCT.md",
        repository / "AGENTS.md",
        repository / "CLAUDE.md",
    ]
    roots.extend(sorted((repository / "docs").rglob("*.md")))
    roots.extend(sorted((repository / ".github").rglob("*.md")))
    roots.extend(sorted((repository / ".gitea").rglob("*.md")))
    return [path for path in roots if path.is_file()]


def _markdown_links(path: Path) -> list[str]:
    return LINK_RE.findall(path.read_text(encoding="utf-8"))


def _slug(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"!?\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", "-", text)


def _anchors(path: Path) -> set[str]:
    counts: dict[str, int] = {}
    anchors: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = HEADING_RE.match(line)
        if not match:
            continue
        base = _slug(match.group(2))
        duplicate = counts.get(base, 0)
        counts[base] = duplicate + 1
        anchors.add(base if duplicate == 0 else f"{base}-{duplicate}")
    return anchors


def _link_target(source: Path, raw_target: str) -> tuple[Path, str] | None:
    target = raw_target.strip().strip("<>")
    if not target or target.startswith(("#", "/", "mailto:")):
        return None
    if re.match(r"^[a-z][a-z0-9+.-]*:", target, flags=re.IGNORECASE):
        return None
    file_part, separator, fragment = target.partition("#")
    resolved = (source.parent / unquote(file_part)).resolve()
    return resolved, unquote(fragment) if separator else ""


def _indexed_targets(index: Path, base: Path) -> list[str]:
    targets: list[str] = []
    for raw_target in _markdown_links(index):
        resolved = _link_target(index, raw_target)
        if resolved is None:
            continue
        path, _fragment = resolved
        if path.suffix == ".md" and path.is_relative_to(base):
            targets.append(path.relative_to(base).as_posix())
    return targets


def _adr_metadata(path: Path) -> tuple[str | None, str | None]:
    source = path.read_text(encoding="utf-8")
    status_match = re.search(r"^- Status:\s*([^;\n]+)", source, re.MULTILINE)
    date_match = re.search(r"^- Date:\s*(\d{4}-\d{2}-\d{2})\s*$", source, re.MULTILINE)
    return (
        status_match.group(1).strip() if status_match else None,
        date_match.group(1) if date_match else None,
    )


def collect_errors(repository: Path = REPOSITORY) -> list[str]:
    errors: list[str] = []
    docs = repository / "docs"

    for relative in sorted(OBSOLETE_PATHS):
        if (repository / relative).exists():
            errors.append(f"obsolete documentation path still exists: {relative}")

    actual_guides = {
        path.relative_to(docs).as_posix()
        for path in docs.rglob("*.md")
        if not ADR_RE.match(path.name)
    }
    missing = sorted(CANONICAL_GUIDES - actual_guides)
    extra = sorted(actual_guides - CANONICAL_GUIDES)
    if missing:
        errors.append(f"canonical documentation missing: {', '.join(missing)}")
    if extra:
        errors.append(f"unclassified durable documentation: {', '.join(extra)}")

    docs_index = docs / "README.md"
    if docs_index.is_file():
        indexed = _indexed_targets(docs_index, docs)
        expected = sorted(CANONICAL_GUIDES - {"README.md"})
        if sorted(indexed) != expected:
            errors.append(
                "docs/README.md must link every canonical guide exactly once "
                f"(expected {expected!r}, found {sorted(indexed)!r})"
            )

    decisions = docs / "decisions"
    adr_files = sorted(path.name for path in decisions.glob("ADR-*.md"))
    adr_index = decisions / "README.md"
    if adr_index.is_file():
        indexed_adrs = [
            target
            for target in _indexed_targets(adr_index, decisions)
            if ADR_RE.match(target)
        ]
        if sorted(indexed_adrs) != adr_files:
            errors.append("docs/decisions/README.md must link every ADR exactly once")

    for adr_name in adr_files:
        adr = decisions / adr_name
        status, date = _adr_metadata(adr)
        if status not in VALID_ADR_STATUSES:
            errors.append(f"{adr.relative_to(repository).as_posix()} has invalid or missing Status")
        if date is None:
            errors.append(f"{adr.relative_to(repository).as_posix()} has invalid or missing Date")

    for markdown in _relative_markdown_files(repository):
        for raw_target in _markdown_links(markdown):
            resolved = _link_target(markdown, raw_target)
            if resolved is None:
                continue
            target, fragment = resolved
            if not target.exists():
                errors.append(
                    f"broken relative link in {markdown.relative_to(repository).as_posix()}: {raw_target}"
                )
                continue
            if fragment and target.is_file() and target.suffix == ".md":
                if fragment not in _anchors(target):
                    errors.append(
                        f"broken heading link in {markdown.relative_to(repository).as_posix()}: {raw_target}"
                    )

    version = (repository / "VERSION").read_text(encoding="utf-8").strip()
    readme = (repository / "README.md").read_text(encoding="utf-8")
    published_checkout = "git switch --detach vX.Y.Z"
    if published_checkout not in readme:
        errors.append(
            "README install must direct operators to select a published immutable tag"
        )
    if f"git switch --detach v{version}" in readme:
        errors.append("README must not infer that the source version already has a tag")
    if len(readme.splitlines()) > 220:
        errors.append("README exceeds the 220-line public-entrypoint budget")

    version_parts = version.split(".")
    current_series = ".".join(version_parts[:2]) + "."
    current_version_files = [
        path
        for path in _relative_markdown_files(repository)
        if not (path.parent == docs / "decisions" and ADR_RE.match(path.name))
    ]
    for path in current_version_files:
        if not path.is_file():
            continue
        versions = set(VERSION_RE.findall(path.read_text(encoding="utf-8")))
        stale = sorted(
            item
            for item in versions
            if item.startswith(current_series) and item != version
        )
        if stale:
            errors.append(
                f"{path.relative_to(repository).as_posix()} contains stale current-series releases: "
                + ", ".join(f"v{item}" for item in stale)
            )

    env_heading = (repository / ".env.example").read_text(encoding="utf-8").splitlines()[0]
    if re.search(r"v\d+\.\d+", env_heading):
        errors.append(".env.example heading must be version-neutral")

    return errors


def main() -> int:
    errors = collect_errors()
    if errors:
        for error in errors:
            print(f"docs: {error}", file=sys.stderr)
        return 1
    print("documentation contract: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
