"""Static validators for DWCS-004 deploy examples (not production deploy).

These checks keep example topology honest: reuse host Caddy, no public app/DB
ports, no embedded secrets, and the documented path/permission/rollback shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"

FORBIDDEN_PUBLIC_BIND_PATTERNS = (
    re.compile(r"\b0\.0\.0\.0\b"),
    re.compile(r"(?m)^\s*ports\s*:"),
    re.compile(r"(?m)^\s*publish\s*:"),
    re.compile(r"\bhost\s*=\s*0\.0\.0\.0\b", re.I),
    # Explicit app binds to privileged web ports in unit/compose command lines.
    re.compile(r"(?i)--publish\s+.*\b(80|443)\b"),
    re.compile(r"(?i)-p\s+0\.0\.0\.0:(80|443)"),
)

SECOND_PROXY_PATTERNS = (
    re.compile(r"\bnginx\b", re.I),
    re.compile(r"\btraefik\b", re.I),
    re.compile(r"\bhaproxy\b", re.I),
    re.compile(r"\benvoy\b", re.I),
)

SECRET_PATTERNS = (
    re.compile(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{22,}"),
    re.compile(r"\$apr1\$[^\s]+"),
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(password|secret|token)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
)

REQUIRED_PATHS = (
    "/srv/mma/public",
    "/srv/mma/data",
    "/etc/mma-model/mma.env",
)

IPV4_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    message: str


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def iter_example_files(examples_dir: Path = EXAMPLES_DIR) -> list[Path]:
    if not examples_dir.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(examples_dir.rglob("*")):
        if path.is_file() and path.name != ".gitkeep":
            files.append(path)
    return files


def validate_not_installed_banner(text: str, rel: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    upper = text.upper()
    if "NOT INSTALLED" not in upper and "EXAMPLE ONLY" not in upper:
        issues.append(
            ValidationIssue(
                rel,
                "example must be marked EXAMPLE ONLY / NOT INSTALLED",
            )
        )
    return issues


def validate_no_public_app_ports(text: str, rel: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for pattern in FORBIDDEN_PUBLIC_BIND_PATTERNS:
        if pattern.search(text):
            issues.append(
                ValidationIssue(
                    rel,
                    f"forbidden public bind/publish pattern matched: {pattern.pattern}",
                )
            )
    return issues


def validate_reuse_existing_caddy(text: str, rel: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    lower = text.lower()
    rel_l = rel.lower().replace("\\", "/")
    is_caddy_snippet = rel_l.endswith("caddyfile.mma.snippet")
    if is_caddy_snippet:
        if "mma.shermandavison.com" not in lower:
            issues.append(
                ValidationIssue(rel, "Caddy example must target mma.shermandavison.com")
            )
        if "/srv/mma/public" not in text:
            issues.append(
                ValidationIssue(rel, "Caddy example must serve /srv/mma/public")
            )
        if "basic_auth" not in lower and "basicauth" not in lower:
            issues.append(
                ValidationIssue(rel, "Caddy example must include basic_auth")
            )
    for pattern in SECOND_PROXY_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        start = max(0, match.start() - 24)
        window = text[start : match.start()].lower()
        # Strip the boilerplate banner so "NOT INSTALLED" does not count as negation.
        window = window.replace("not installed", "").replace("example only", "")
        if re.search(r"\b(do not|don't|never|no|without|forbid|reject)\b", window):
            continue
        issues.append(
            ValidationIssue(
                rel,
                f"second reverse proxy must not be proposed ({pattern.pattern})",
            )
        )
    return issues


def validate_no_embedded_secrets(text: str, rel: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            token = match.group(0)
            if "PLACEHOLDER" in token.upper() or "REDACTED" in token.upper():
                continue
            if "changeme" in token.lower():
                continue
            issues.append(
                ValidationIssue(rel, f"possible embedded secret matched: {pattern.pattern}")
            )
    # Public IPs must not appear in committed examples.
    for match in IPV4_PATTERN.finditer(text):
        ip = match.group(0)
        if ip.startswith("127."):
            continue
        if ip in {"0.0.0.0"}:
            # handled by public-bind checks
            continue
        issues.append(ValidationIssue(rel, f"public IPv4 must not appear: {ip}"))
    return issues


def validate_compose_invariants(text: str, rel: str) -> list[ValidationIssue]:
    if not rel.endswith(("docker-compose.yml", "compose.yml", "compose.yaml")):
        return []
    issues: list[ValidationIssue] = []
    if re.search(r"(?m)^\s*ports\s*:", text):
        issues.append(ValidationIssue(rel, "compose must not publish ports"))
    if "/srv/mma/data" not in text or "/srv/mma/public" not in text:
        issues.append(
            ValidationIssue(rel, "compose must mount /srv/mma/data and /srv/mma/public")
        )
    if "/etc/mma-model/mma.env" not in text:
        issues.append(
            ValidationIssue(rel, "compose must reference /etc/mma-model/mma.env")
        )
    if "sha256:" not in text and "@sha256" not in text:
        issues.append(
            ValidationIssue(
                rel,
                "compose image must be digest-pinned (sha256 placeholder allowed)",
            )
        )
    return issues


def validate_systemd_invariants(text: str, rel: str) -> list[ValidationIssue]:
    if not (rel.endswith(".service") or rel.endswith(".timer")):
        return []
    issues: list[ValidationIssue] = []
    lower = text.lower()
    if rel.endswith(".service"):
        has_compose = ("docker compose" in lower) or ("docker-compose" in lower)
        has_run = ("run --rm" in lower) or bool(re.search(r"\brun\b.*\b--rm\b", lower))
        if not (has_compose and has_run):
            issues.append(
                ValidationIssue(
                    rel,
                    "systemd service example must use docker compose run --rm worker",
                )
            )
        if "flock" not in lower:
            issues.append(ValidationIssue(rel, "systemd service must use flock"))
    if rel.endswith(".timer") and "persistent=true" not in lower:
        issues.append(ValidationIssue(rel, "timer should set Persistent=true"))
    return issues


def validate_required_path_mentions(files: dict[str, str]) -> list[ValidationIssue]:
    """Across the example set, required paths and rollback language must appear."""
    blob = "\n".join(files.values())
    issues: list[ValidationIssue] = []
    for path in REQUIRED_PATHS:
        if path not in blob:
            issues.append(
                ValidationIssue(
                    "deploy/examples",
                    f"required path missing across examples: {path}",
                )
            )
    if "0600" not in blob:
        issues.append(
            ValidationIssue(
                "deploy/examples",
                "secret file mode 0600 must be documented in examples",
            )
        )
    if "rollback" not in blob.lower():
        issues.append(
            ValidationIssue(
                "deploy/examples",
                "rollback invariant must be documented in examples",
            )
        )
    if "existing" not in blob.lower() or "caddy" not in blob.lower():
        issues.append(
            ValidationIssue(
                "deploy/examples",
                "examples must document reuse of existing host Caddy",
            )
        )
    return issues


def validate_examples(examples_dir: Path = EXAMPLES_DIR) -> list[ValidationIssue]:
    files = {str(p.relative_to(examples_dir.parent)): _read(p) for p in iter_example_files(examples_dir)}
    # keys like examples/foo — normalize to deploy-relative if possible
    normalized: dict[str, str] = {}
    for key, text in files.items():
        if key.startswith("examples/"):
            normalized[f"deploy/{key}"] = text
        else:
            normalized[key] = text

    issues: list[ValidationIssue] = []
    if not normalized:
        return [ValidationIssue("deploy/examples", "no example files found")]

    for rel, text in normalized.items():
        issues.extend(validate_not_installed_banner(text, rel))
        issues.extend(validate_no_public_app_ports(text, rel))
        issues.extend(validate_reuse_existing_caddy(text, rel))
        issues.extend(validate_no_embedded_secrets(text, rel))
        issues.extend(validate_compose_invariants(text, rel))
        issues.extend(validate_systemd_invariants(text, rel))
    issues.extend(validate_required_path_mentions(normalized))
    return issues


def main() -> int:
    issues = validate_examples()
    if not issues:
        print("deploy examples: OK")
        return 0
    for issue in issues:
        print(f"{issue.path}: {issue.message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
