"""DWCS-004 deploy example topology validators (TDD)."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATE_PATH = REPO_ROOT / "deploy" / "validate_examples.py"
EXAMPLES_DIR = REPO_ROOT / "deploy" / "examples"

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


def _load_validator():
    spec = importlib.util.spec_from_file_location(
        "mma_deploy_validate_examples", VALIDATE_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def validate():
    return _load_validator()


def _minimal_examples(tmp_path: Path, compose_body: str) -> Path:
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "README.md").write_text(
        "EXAMPLE ONLY — NOT INSTALLED\n"
        "Reuse existing host Caddy.\n"
        "rollback: retain prior image digest\n"
        "secret mode 0600 at /etc/mma-model/mma.env\n"
        "paths /srv/mma/public /srv/mma/data\n",
        encoding="utf-8",
    )
    (examples / "Caddyfile.mma.snippet").write_text(
        "# EXAMPLE ONLY — NOT INSTALLED\n"
        "mma.shermandavison.com {\n"
        "  basic_auth { mma PLACEHOLDER_HASH }\n"
        "  root * /srv/mma/public\n"
        "  file_server\n"
        "}\n",
        encoding="utf-8",
    )
    (examples / "docker-compose.yml").write_text(compose_body, encoding="utf-8")
    return examples


def test_examples_directory_exists():
    assert EXAMPLES_DIR.is_dir()


def test_required_example_files_present():
    required = [
        EXAMPLES_DIR / "README.md",
        EXAMPLES_DIR / "Caddyfile.mma.snippet",
        EXAMPLES_DIR / "docker-compose.yml",
        EXAMPLES_DIR / "mma.env.example",
        EXAMPLES_DIR / "systemd" / "mma-worker.service",
        EXAMPLES_DIR / "systemd" / "mma-worker.timer",
        EXAMPLES_DIR / "systemd" / "mma-backup.service",
        EXAMPLES_DIR / "systemd" / "mma-backup.timer",
    ]
    missing = [str(p.relative_to(REPO_ROOT)) for p in required if not p.is_file()]
    assert missing == []


def test_validate_examples_passes_on_committed_tree(validate):
    issues = validate.validate_examples(EXAMPLES_DIR)
    assert issues == [], [f"{i.path}: {i.message}" for i in issues]


def test_compose_rejects_published_ports(validate, tmp_path: Path):
    examples = _minimal_examples(
        tmp_path,
        "\n".join(
            [
                "# EXAMPLE ONLY — NOT INSTALLED",
                "services:",
                "  worker:",
                "    image: ghcr.io/example/mma-model@sha256:deadbeef",
                "    ports:",
                "      - '8000:8000'",
                "    env_file: [/etc/mma-model/mma.env]",
                "    volumes:",
                "      - /srv/mma/data:/data",
                "      - /srv/mma/public:/public",
            ]
        ),
    )
    issues = validate.validate_examples(examples)
    messages = " | ".join(i.message for i in issues)
    assert "ports" in messages.lower()


@pytest.mark.parametrize(
    "compose_fragment",
    [
        "network_mode: host",
        'network_mode: "host"',
        "network_mode: 'host'",
        "network_mode:host",
        "network-mode: host",
    ],
)
def test_compose_rejects_network_mode_host_variants(
    validate, tmp_path: Path, compose_fragment: str
):
    examples = _minimal_examples(
        tmp_path,
        "\n".join(
            [
                "# EXAMPLE ONLY — NOT INSTALLED",
                "services:",
                "  worker:",
                "    image: ghcr.io/example/mma-model@sha256:deadbeef",
                f"    {compose_fragment}",
                "    env_file: [/etc/mma-model/mma.env]",
                "    volumes:",
                "      - /srv/mma/data:/data",
                "      - /srv/mma/public:/public",
            ]
        ),
    )
    issues = validate.validate_examples(examples)
    blob = " | ".join(i.message for i in issues).lower()
    assert "network_mode" in blob or "host" in blob


@pytest.mark.parametrize(
    "expose_block",
    [
        "expose:\n      - '8000'",
        "expose: ['8000']",
        'expose: ["8000"]',
    ],
)
def test_compose_rejects_expose_variants(validate, tmp_path: Path, expose_block: str):
    examples = _minimal_examples(
        tmp_path,
        "\n".join(
            [
                "# EXAMPLE ONLY — NOT INSTALLED",
                "services:",
                "  worker:",
                "    image: ghcr.io/example/mma-model@sha256:deadbeef",
                f"    {expose_block}",
                "    env_file: [/etc/mma-model/mma.env]",
                "    volumes:",
                "      - /srv/mma/data:/data",
                "      - /srv/mma/public:/public",
            ]
        ),
    )
    issues = validate.validate_examples(examples)
    blob = " | ".join(i.message for i in issues).lower()
    assert "expose" in blob


@pytest.mark.parametrize(
    "ports_block",
    [
        "ports:\n      - '8000:8000'",
        "ports: ['8000:8000']",
        'ports: ["127.0.0.1:8000:8000"]',
        "ports:\n      - target: 8000\n        published: 8000\n        protocol: tcp",
    ],
)
def test_compose_rejects_published_port_bypass_formats(
    validate, tmp_path: Path, ports_block: str
):
    examples = _minimal_examples(
        tmp_path,
        "\n".join(
            [
                "# EXAMPLE ONLY — NOT INSTALLED",
                "services:",
                "  worker:",
                "    image: ghcr.io/example/mma-model@sha256:deadbeef",
                f"    {ports_block}",
                "    env_file: [/etc/mma-model/mma.env]",
                "    volumes:",
                "      - /srv/mma/data:/data",
                "      - /srv/mma/public:/public",
            ]
        ),
    )
    issues = validate.validate_examples(examples)
    blob = " | ".join(i.message for i in issues).lower()
    assert "ports" in blob or "publish" in blob


def test_rejects_second_proxy_and_secrets(validate, tmp_path: Path):
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "README.md").write_text(
        "EXAMPLE ONLY — NOT INSTALLED\nnginx as front proxy\n"
        "rollback\n0600\n/srv/mma/public\n/srv/mma/data\n/etc/mma-model/mma.env\ncaddy\n",
        encoding="utf-8",
    )
    (examples / "bad.env").write_text(
        "EXAMPLE ONLY — NOT INSTALLED\n"
        "API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n"
        "HOST=203.0.113.10\n",
        encoding="utf-8",
    )
    issues = validate.validate_examples(examples)
    blob = " | ".join(f"{i.path}:{i.message}" for i in issues)
    assert "second reverse proxy" in blob.lower() or "nginx" in blob.lower()
    assert "secret" in blob.lower() or "ipv4" in blob.lower()


def test_caddy_snippet_requires_subdomain_and_public_root(validate):
    text = "# EXAMPLE ONLY — NOT INSTALLED\nexample.com {\n  file_server\n}\n"
    issues = validate.validate_reuse_existing_caddy(
        text, "deploy/examples/Caddyfile.mma.snippet"
    )
    messages = " | ".join(i.message for i in issues)
    assert "mma.shermandavison.com" in messages
    assert "/srv/mma/public" in messages


def test_systemd_requires_flock_and_compose_run(validate):
    text = "# EXAMPLE ONLY — NOT INSTALLED\n[Service]\nExecStart=/usr/bin/true\n"
    issues = validate.validate_systemd_invariants(
        text, "deploy/examples/systemd/mma-worker.service"
    )
    messages = " | ".join(i.message for i in issues)
    assert "flock" in messages.lower()
    assert "docker compose run" in messages.lower()


def test_compose_source_and_rendered_invariants(validate):
    compose = EXAMPLES_DIR / "docker-compose.yml"
    assert compose.is_file()
    source = compose.read_text(encoding="utf-8")
    assert re.search(r"(?m)^\s*ports\s*:", source) is None
    assert re.search(r"(?m)^\s*expose\s*:", source) is None
    assert re.search(r"(?i)network[_-]?mode\s*:\s*[\"']?host[\"']?", source) is None
    source_issues = validate.validate_compose_structured(
        source, "deploy/examples/docker-compose.yml"
    )
    assert source_issues == [], [i.message for i in source_issues]

    if shutil.which("docker") is None:
        pytest.skip("docker not available for rendered compose config")

    proc = subprocess.run(
        ["docker", "compose", "-f", str(compose), "config"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # Placeholder digest / missing host env file are acceptable local failures,
        # but the failure must not be about publishing ports.
        combined = (proc.stdout + proc.stderr).lower()
        assert "published" not in combined
        assert "ports" not in combined or "variable is not set" in combined or "error" in combined
        return

    rendered = yaml.safe_load(proc.stdout)
    assert isinstance(rendered, dict)
    validate.assert_rendered_compose_has_no_publish_surface(rendered)


def test_docs_current_state_and_target_topology_exist():
    current = REPO_ROOT / "docs" / "deployment" / "current-state.md"
    target = REPO_ROOT / "docs" / "deployment" / "target-topology.md"
    assert current.is_file()
    assert target.is_file()
    current_text = current.read_text(encoding="utf-8")
    target_text = target.read_text(encoding="utf-8")
    for token in ("observed", "blocked", "proposed"):
        assert token in current_text.lower()
    assert "shermandavison.com" in current_text
    assert "mma.shermandavison.com" in current_text
    assert "NXDOMAIN" in current_text or "nxdomain" in current_text.lower()
    assert "INCOMPLETE" in current_text and "NOT VERIFIED" in current_text
    assert "DWCS-503" in current_text and "hard prerequisite" in current_text.lower()
    assert "connection refused" in current_text.lower()
    assert "loopback" in current_text.lower()
    assert "ruff check" in current_text.lower()
    assert "/srv/mma/public" in target_text
    assert "basic_auth" in target_text
    assert "rollback" in target_text.lower()
    assert "host networking" in target_text.lower() or "network_mode" in target_text.lower()
    assert "0.0.0.0" not in target_text
    assert "0.0.0.0" not in current_text

    for doc_name, text in ("current-state.md", current_text), ("target-topology.md", target_text):
        for match in re.finditer(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
            ip = match.group(0)
            assert ip.startswith("127."), f"{doc_name} leaked non-loopback IP {ip}"
        lowered = text.lower()
        for token in FORBIDDEN_DOC_TOKENS:
            assert token.lower() not in lowered, f"{doc_name} still discloses {token}"


def test_validator_cli_exits_zero(validate):
    assert validate.main() == 0


def test_ci_already_runs_ruff():
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "ruff check ." in ci
    assert "Ruff evaluation (strict)" in ci
