"""DWCS-503 production Caddy snippet + deploy helper invariants."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CADDY_PROD = REPO_ROOT / "deploy" / "Caddyfile.mma"
COMPOSE_PATH = REPO_ROOT / "deploy" / "compose.yaml"
DIGEST_PATH = REPO_ROOT / "deploy" / "image-digest.txt"
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"
ROLLBACK_SH = REPO_ROOT / "deploy" / "rollback.sh"
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "deploy.md"
EVIDENCE = REPO_ROOT / "docs" / "deployment" / "dwcs-503-evidence.md"

PINNED_DIGEST = (
    "sha256:99d0aeb4c3af1ad4d733a793ac4e0407fed0fcf84d3a8c1f3a4f0fc6b943a5ae"
)

FORBIDDEN_DOC_TOKENS = (
    "golf-vps",
    "ubuntu-8gb-hel1-1",
    "hetzner-bot",
    "hel1-dc2",
    "golf.shermandavison.com",
    "/api/golf",
    "golf-backup",
    "golf-live",
    "golf-disk",
    "golf-grading",
    "golf-retention",
    "machine-id",
    "243c7c05a76c",
)

SECRET_PATTERNS = (
    re.compile(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{22,}"),
    # Assignment of a quoted secret literal (not shell expansions / file reads).
    re.compile(
        r"(?i)(password|secret|token)\s*[:=]\s*['\"][A-Za-z0-9_\-+/=]{8,}['\"]"
    ),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bghr_[A-Za-z0-9]{20,}\b"),
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_production_caddy_snippet_exists_and_targets_subdomain():
    assert CADDY_PROD.is_file()
    text = _read(CADDY_PROD)
    assert "mma.shermandavison.com" in text
    assert "/srv/mma/public" in text
    assert "basicauth" in text.lower()
    # Caddy 2.6.2 incompatible directive spelling must not be used as a directive.
    assert re.search(r"(?m)^\s*basic_auth\b", text) is None
    assert "REDACTED_HASH_PLACEHOLDER" in text
    assert "file_server" in text
    assert "encode" in text
    assert "Content-Security-Policy" in text
    assert "X-Content-Type-Options" in text
    assert "X-Frame-Options" in text


def test_production_caddy_cache_and_nocache_invariants():
    text = _read(CADDY_PROD)
    assert "/assets/*" in text
    assert "immutable" in text
    assert "max-age=31536000" in text
    for path in (
        "/live/*",
        "/current-event.json",
        "/health.json",
        "/matchups.json",
        "/performance.json",
        "/history.json",
        "/release.json",
        "/manifest.json",
    ):
        assert path in text
    assert "no-store" in text
    assert "no-cache" in text


def test_compose_still_has_no_publish_surface_and_pinned_digest():
    source = _read(COMPOSE_PATH)
    assert re.search(r"(?m)^\s*ports\s*:", source) is None
    assert re.search(r"(?m)^\s*expose\s*:", source) is None
    assert re.search(r"(?i)network[_-]?mode\s*:\s*[\"']?host[\"']?", source) is None
    assert PINNED_DIGEST in source
    assert "pull_policy: never" in source
    data = yaml.safe_load(source)
    worker = data["services"]["worker"]
    assert "ports" not in worker
    assert "expose" not in worker
    assert worker["image"].endswith(f"@{PINNED_DIGEST}")


def test_image_digest_file_pins_release_digest():
    text = _read(DIGEST_PATH)
    assert f"current={PINNED_DIGEST}" in text
    assert "previous=" in text
    # First real pin: previous may be empty; must not invent a fake digest.
    previous_line = next(
        line for line in text.splitlines() if line.startswith("previous=")
    )
    previous_val = previous_line.split("=", 1)[1].strip()
    assert previous_val == "" or previous_val.startswith("sha256:")
    assert "ghcr.io/0xaidan/mma-model" in text


def test_install_and_rollback_scripts_exist():
    assert INSTALL_SH.is_file()
    assert ROLLBACK_SH.is_file()
    install = _read(INSTALL_SH)
    rollback = _read(ROLLBACK_SH)
    assert "basicauth" in install.lower() or "hash-password" in install
    assert "/srv/mma/public" in install
    assert "/etc/mma-model/mma.env" in install
    assert "0600" in install
    assert "caddy validate" in install
    assert "pull_policy" in install or "docker pull" in install
    assert "--resolve" in install  # loopback SNI baseline (avoid hairpin hangs)
    assert "--max-time" in install
    assert "already present locally" in install
    assert "Caddyfile.bak-dwcs503" in rollback
    assert "--public-release" in rollback
    assert "live/" in rollback
    assert "sort -r" in rollback  # filename-timestamp backup ordering
    # Scripts must not propose a second reverse proxy.
    for blob in (install, rollback):
        for proxy in ("nginx", "traefik", "haproxy"):
            assert proxy not in blob.lower()


def test_runbook_and_evidence_exist_without_secrets_or_forbidden_tokens():
    assert RUNBOOK.is_file()
    assert EVIDENCE.is_file()
    docs = {
        "deploy.md": _read(RUNBOOK),
        "dwcs-503-evidence.md": _read(EVIDENCE),
        "Caddyfile.mma": _read(CADDY_PROD),
        "install.sh": _read(INSTALL_SH),
        "rollback.sh": _read(ROLLBACK_SH),
        "README.md": _read(REPO_ROOT / "deploy" / "README.md"),
    }
    for name, text in docs.items():
        lowered = text.lower()
        for token in FORBIDDEN_DOC_TOKENS:
            assert token.lower() not in lowered, f"{name} discloses {token}"
        for match in re.finditer(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
            ip = match.group(0)
            assert ip.startswith("127."), f"{name} leaked non-loopback IP {ip}"
        for pattern in SECRET_PATTERNS:
            for m in pattern.finditer(text):
                token = m.group(0)
                assert (
                    "PLACEHOLDER" in token.upper() or "REDACTED" in token.upper()
                ), f"{name} possible secret: {pattern.pattern}"

    runbook = docs["deploy.md"]
    assert "basicauth" in runbook.lower()
    assert "mma.shermandavison.com" in runbook
    assert "shermandavison.com" in runbook
    assert "rollback" in runbook.lower()
    assert "/etc/mma-model/dashboard.basicauth.password" in runbook
    assert PINNED_DIGEST in runbook


def test_examples_still_marked_not_installed():
    snippet = _read(REPO_ROOT / "deploy" / "examples" / "Caddyfile.mma.snippet")
    assert "EXAMPLE ONLY" in snippet.upper() or "NOT INSTALLED" in snippet.upper()
    assert "basicauth" in snippet.lower()
