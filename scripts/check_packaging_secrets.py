#!/usr/bin/env python3
"""Fail closed if packaging would ship secrets or accidental artifacts (DWCS-502).

Checks .dockerignore exclusions and scans **git-tracked** files for secrets and
forbidden research/database artifacts. Untracked local dumps are ignored so
developer working trees do not false-fail CI packaging gates.

Canary detection builds the forbidden assignment from parts so this script never
contains the contiguous literal in its own source.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Built from parts so the contiguous canary string is not present in this file.
_CANARY_KEY = "THE_ODDS_API_KEY"
_CANARY_VALUE = "real"
_CANARY_FINDING = "forbidden canary API key assignment"
_CANARY_REGEX = re.compile(
    r"(?i)" + re.escape(_CANARY_KEY) + r"\s*=\s*" + re.escape(_CANARY_VALUE) + r"\b"
)

FORBIDDEN_BASENAMES = frozenset(
    {
        ".env",
        "mma.db",
        "policies-and-apis.json",
    }
)

FORBIDDEN_SUFFIXES = (
    ".db",
    ".sqlite",
    ".sqlite3",
)

FORBIDDEN_NAME_PATTERNS = (
    re.compile(r".*feasibility\.json$", re.I),
    re.compile(r".*pages\.json$", re.I),
    re.compile(r".*-search\.json$", re.I),
    re.compile(r".*-search-summary\.txt$", re.I),
)

SKIP_SCAN_PREFIXES = (
    "tests/",
    "docs/",
    ".github/",
)

# Scanner / packaging helpers may mention API key *names*; never treat those as leaks.
SELF_SCAN_ALLOWLIST = frozenset(
    {
        "scripts/check_packaging_secrets.py",
    }
)


def _git_tracked_files(root: Path) -> list[Path]:
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    out = proc.stdout.split(b"\0")
    paths: list[Path] = []
    for raw in out:
        if not raw:
            continue
        paths.append(root / raw.decode("utf-8", errors="replace"))
    return paths


def _is_forbidden_artifact(path: Path) -> str | None:
    name = path.name
    if name in FORBIDDEN_BASENAMES:
        return f"forbidden basename {name}"
    if name.startswith(".env.") and name != ".env.example":
        return f"forbidden env file {name}"
    lower = name.lower()
    for suffix in FORBIDDEN_SUFFIXES:
        if lower.endswith(suffix):
            return f"forbidden database artifact {name}"
    for pattern in FORBIDDEN_NAME_PATTERNS:
        if pattern.match(name):
            return f"forbidden research dump {name}"
    if name == ".firecrawl" or ".firecrawl" in path.parts:
        return "forbidden .firecrawl path"
    return None


def check_dockerignore(root: Path) -> list[str]:
    path = root / ".dockerignore"
    issues: list[str] = []
    if not path.is_file():
        return [".dockerignore missing"]
    text = path.read_text(encoding="utf-8")
    for token in (".env", "data/", "*.db", ".firecrawl/", "node_modules"):
        if token not in text:
            issues.append(f".dockerignore missing exclusion for {token}")
    return issues


def _scan_text_for_secrets(rel: str, text: str) -> list[str]:
    findings: list[str] = []
    if rel in SELF_SCAN_ALLOWLIST:
        return findings
    if _CANARY_REGEX.search(text):
        findings.append(f"{rel}: {_CANARY_FINDING}")
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "PLACEHOLDER" in stripped.upper():
            continue
        if _CANARY_REGEX.search(line):
            findings.append(f"{rel}:{idx}: {_CANARY_FINDING}")
            continue
        # Live-looking key assignments that are not placeholders / empty.
        match = re.search(
            r"(?i)\b(THE_ODDS_API_KEY|ODDS_API_KEY|BALLDONTLIE_API_KEY|SPORTSDATAIO_API_KEY)"
            r"\s*=\s*([^\s#]+)",
            line,
        )
        if not match:
            continue
        value = match.group(2).strip().strip("'\"")
        if not value:
            continue
        if value.upper() in {"PLACEHOLDER", "EXAMPLE", "CHANGEME", "YOUR_KEY_HERE"}:
            continue
        if value.lower().startswith("your_"):
            continue
        if len(value) >= 12:
            findings.append(
                f"{rel}:{idx}: possible live API key assignment ({match.group(1)})"
            )
    return findings


def scan_tracked(root: Path) -> list[str]:
    findings: list[str] = []
    tracked = _git_tracked_files(root)
    if not tracked:
        # Fallback for non-git test roots: scan all files under root.
        tracked = [p for p in root.rglob("*") if p.is_file()]

    for path in tracked:
        if not path.is_file():
            continue
        try:
            rel = str(path.relative_to(root)).replace("\\", "/")
        except ValueError:
            continue

        reason = _is_forbidden_artifact(path)
        if reason is not None:
            findings.append(f"{rel}: {reason}")
            continue

        if any(rel.startswith(prefix) for prefix in SKIP_SCAN_PREFIXES):
            # Still catch the canary assignment in tests/docs when present as text.
            if path.suffix.lower() in {".py", ".md", ".yml", ".yaml", ".txt", ".env"}:
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                if _CANARY_REGEX.search(text):
                    findings.append(f"{rel}: {_CANARY_FINDING}")
            continue

        if path.suffix.lower() not in {
            ".yml",
            ".yaml",
            ".toml",
            ".txt",
            ".md",
            ".env",
            ".example",
            ".py",
            ".sh",
            ".json",
        } and path.name not in {"Dockerfile", ".dockerignore", "mma.env.example"}:
            continue
        if path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(_scan_text_for_secrets(rel, text))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()

    findings = check_dockerignore(root)
    findings.extend(scan_tracked(root))

    seen: set[str] = set()
    unique: list[str] = []
    for item in findings:
        if item not in seen:
            seen.add(item)
            unique.append(item)

    if unique:
        print("secret/artifact packaging scan FAILED:", file=sys.stderr)
        for item in unique:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print("secret/artifact packaging scan OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
