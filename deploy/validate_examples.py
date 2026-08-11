"""Static validators for DWCS-004 deploy examples (not production deploy).

These checks keep example topology honest: reuse host Caddy, no public app/DB
ports, no host networking, no embedded secrets, and the documented
path/permission/rollback shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"

FORBIDDEN_PUBLIC_BIND_PATTERNS = (
    re.compile(r"\b0\.0\.0\.0\b"),
    re.compile(r"(?m)^\s*ports\s*:"),
    re.compile(r"(?m)^\s*expose\s*:"),
    re.compile(r"(?m)^\s*publish\s*:"),
    re.compile(r"(?i)network[_-]?mode\s*:\s*[\"']?host[\"']?\b"),
    # Top-level Compose network driver host (mapping/list/inline forms).
    re.compile(r"(?i)\bdriver\s*:\s*[\"']?host[\"']?\b"),
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
COMPOSE_SUFFIXES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    message: str


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _is_compose_rel(rel: str) -> bool:
    name = Path(rel).name.lower()
    return name in COMPOSE_SUFFIXES or name.endswith(".compose.yml")


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


def _strip_hash_comments(text: str) -> str:
    """Remove `# ...` comments so documentary mentions do not false-positive."""
    lines: list[str] = []
    for line in text.splitlines():
        if "#" not in line:
            lines.append(line)
            continue
        in_single = False
        in_double = False
        out: list[str] = []
        for ch in line:
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "#" and not in_single and not in_double:
                break
            out.append(ch)
        lines.append("".join(out))
    return "\n".join(lines)


def validate_no_public_app_ports(text: str, rel: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    # Compose/unit files: ignore comments. Markdown may document forbidden keys;
    # only enforce bind patterns there when not clearly rejecting them.
    scan = _strip_hash_comments(text) if _is_compose_rel(rel) or rel.endswith(
        (".service", ".timer", ".env.example", ".env")
    ) else text
    rel_l = rel.lower()
    for pattern in FORBIDDEN_PUBLIC_BIND_PATTERNS:
        if not pattern.search(scan):
            continue
        if rel_l.endswith("readme.md") or rel_l.endswith(".md"):
            # Allow docs that reject the pattern ("no ports", "never network_mode host").
            if re.search(
                r"(?i)\b(no|never|not|without|forbid|reject|do not|don't)\b.{0,40}"
                + pattern.pattern,
                text,
            ) or re.search(
                r"(?i)" + pattern.pattern + r".{0,40}\b(forbidden|not allowed|must not)\b",
                text,
            ):
                continue
        issues.append(
            ValidationIssue(
                rel,
                f"forbidden public bind/publish pattern matched: {pattern.pattern}",
            )
        )
    return issues


def _network_mode_is_host(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() == "host"
    return False


def _driver_is_host(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() == "host"
    return False


def _iter_network_defs(networks: Any) -> list[tuple[str, Any]]:
    """Yield (name, definition) pairs from Compose top-level networks mapping/list."""
    items: list[tuple[str, Any]] = []
    if networks is None:
        return items
    if isinstance(networks, dict):
        for name, definition in networks.items():
            items.append((str(name), definition))
        return items
    if isinstance(networks, list):
        for idx, definition in enumerate(networks):
            if isinstance(definition, dict):
                name = str(definition.get("name") or definition.get("key") or idx)
            else:
                name = str(idx)
            items.append((name, definition))
    return items


def _network_def_uses_host_driver(definition: Any) -> bool:
    """True when a Compose network definition selects the host driver."""
    if definition is None:
        return False
    if isinstance(definition, str):
        lowered = definition.strip().lower()
        # Literal host, or YAML-collapsed "driver:host" / "driver: host" forms.
        if lowered == "host":
            return True
        if re.search(r"(?i)\bdriver\s*:\s*host\b", lowered):
            return True
        return False
    if not isinstance(definition, dict):
        return False
    if _driver_is_host(definition.get("driver")):
        return True
    # Rare alternate key spellings observed in hand-edited YAML.
    if _driver_is_host(definition.get("Driver")):
        return True
    return False


def _top_level_host_network_violations(data: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    if _network_mode_is_host(data.get("network_mode")):
        violations.append("compose uses top-level network_mode host")
    for name, definition in _iter_network_defs(data.get("networks")):
        if _network_def_uses_host_driver(definition):
            violations.append(
                f"compose top-level network {name!r} uses driver host"
            )
    return violations


def _service_has_publish_surface(service: Any) -> list[str]:
    """Return structured invariant violations for one Compose service mapping."""
    violations: list[str] = []
    if not isinstance(service, dict):
        return violations
    if "ports" in service and service.get("ports") not in (None, [], {}):
        violations.append("compose service publishes ports")
    if "expose" in service and service.get("expose") not in (None, [], {}):
        violations.append("compose service uses expose")
    if _network_mode_is_host(service.get("network_mode")):
        violations.append("compose service uses network_mode host")
    return violations


def validate_compose_structured(text: str, rel: str) -> list[ValidationIssue]:
    """YAML-aware Compose checks (preferred over regex-only)."""
    if not _is_compose_rel(rel):
        return []
    issues: list[ValidationIssue] = []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [ValidationIssue(rel, f"compose YAML parse failed: {exc}")]

    if data is None:
        return [ValidationIssue(rel, "compose YAML is empty")]
    if not isinstance(data, dict):
        return [ValidationIssue(rel, "compose YAML root must be a mapping")]

    for message in _top_level_host_network_violations(data):
        issues.append(ValidationIssue(rel, message))

    services = data.get("services")
    if not isinstance(services, dict) or not services:
        issues.append(ValidationIssue(rel, "compose must define services mapping"))
        return issues

    for name, service in services.items():
        for message in _service_has_publish_surface(service):
            issues.append(ValidationIssue(rel, f"service {name!r}: {message}"))

    # Path / secret / digest invariants on source text remain useful with comments.
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
        if re.search(r"(?i)network[_-]?mode\s*=\s*host", text):
            issues.append(ValidationIssue(rel, "systemd unit must not set network_mode=host"))
        if re.search(r"(?i)--network[= ]+host\b", text):
            issues.append(ValidationIssue(rel, "systemd unit must not pass --network host"))
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
    files = {
        str(p.relative_to(examples_dir.parent)): _read(p)
        for p in iter_example_files(examples_dir)
    }
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
        issues.extend(validate_compose_structured(text, rel))
        issues.extend(validate_systemd_invariants(text, rel))
    issues.extend(validate_required_path_mentions(normalized))
    return issues


def assert_rendered_compose_has_no_publish_surface(rendered: dict[str, Any]) -> None:
    """Assert docker compose config output has no ports/expose/host networking."""
    for message in _top_level_host_network_violations(rendered):
        raise AssertionError(f"rendered compose: {message}")
    services = rendered.get("services") or {}
    if not isinstance(services, dict):
        raise AssertionError("rendered compose missing services mapping")
    for name, service in services.items():
        for message in _service_has_publish_surface(service):
            raise AssertionError(f"rendered service {name!r}: {message}")


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
