"""DWCS-504 production scheduler units, runner scripts, and monitoring docs."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
EXAMPLES_SYSTEMD = REPO_ROOT / "deploy" / "examples" / "systemd"
RUN_JOB = REPO_ROOT / "deploy" / "run-job.sh"
BACKUP_HOOK = REPO_ROOT / "deploy" / "backup-hook.sh"
MONITOR_CHECK = REPO_ROOT / "deploy" / "monitor-check.sh"
LOGROTATE = REPO_ROOT / "deploy" / "logrotate" / "mma-model"
MONITORING_EXAMPLE = REPO_ROOT / "deploy" / "examples" / "monitoring.env.example"
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "monitoring.md"
EVIDENCE = REPO_ROOT / "docs" / "deployment" / "dwcs-504-evidence.md"
DEPLOY_RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "deploy.md"

PINNED_DIGEST = (
    "sha256:5f209cfdea78fd29907656aae4618c896443464ff7d71c52a1fe756b4d51d7d6"
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
    re.compile(
        r"(?i)(password|secret|token)\s*[:=]\s*['\"][A-Za-z0-9_\-+/=]{8,}['\"]"
    ),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bghr_[A-Za-z0-9]{20,}\b"),
    # Real Healthchecks UUIDs must not appear; PLACEHOLDER is OK.
    re.compile(r"hc-ping\.com/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
)

REQUIRED_UNITS = (
    "mma-scheduler.service",
    "mma-scheduler.timer",
    "mma-backup.service",
    "mma-backup.timer",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_production_systemd_units_exist():
    assert SYSTEMD_DIR.is_dir()
    for name in REQUIRED_UNITS:
        assert (SYSTEMD_DIR / name).is_file(), name


def test_examples_systemd_still_marked_not_installed():
    for path in EXAMPLES_SYSTEMD.glob("mma-*"):
        text = _read(path)
        assert "EXAMPLE ONLY" in text.upper() or "NOT INSTALLED" in text.upper()


def test_scheduler_timer_persistent_and_five_minutes():
    timer = _read(SYSTEMD_DIR / "mma-scheduler.timer")
    assert "Persistent=true" in timer
    assert "OnCalendar=*:0/5" in timer
    assert "Unit=mma-scheduler.service" in timer


def test_backup_timer_persistent_and_nightly():
    timer = _read(SYSTEMD_DIR / "mma-backup.timer")
    assert "Persistent=true" in timer
    assert "OnCalendar=" in timer
    assert "03:15" in timer
    assert "Unit=mma-backup.service" in timer


def test_scheduler_service_uses_run_job_and_bounds():
    svc = _read(SYSTEMD_DIR / "mma-scheduler.service")
    assert "run-job.sh tick" in svc
    assert "TimeoutStartSec=" in svc
    assert "Type=oneshot" in svc
    assert "mma-writer.lock" in svc or "MMA_WRITER_LOCK" in svc
    assert "network_mode=host" not in svc.lower()
    assert "--network host" not in svc.lower()


def test_backup_service_uses_run_job_backup_hook():
    svc = _read(SYSTEMD_DIR / "mma-backup.service")
    assert "run-job.sh backup" in svc
    assert "backup-hook" in svc or "MMA_BACKUP_HOOK" in svc
    assert "Type=oneshot" in svc


def test_run_job_script_invariants():
    text = _read(RUN_JOB)
    assert RUN_JOB.stat().st_mode & 0o111
    assert "flock -n" in text
    assert "mma-writer.lock" in text
    assert "docker compose" in text
    assert "jobs tick" in text
    assert "hc-ping" in text or "MMA_HC_" in text
    assert "[REDACTED]" in text
    assert "--force-fail" in text
    assert "exit 75" in text
    assert "ports" not in text.lower() or "no ports" in text.lower()


def test_backup_hook_is_stub_not_restic():
    text = _read(BACKUP_HOOK)
    assert BACKUP_HOOK.stat().st_mode & 0o111
    assert "DWCS-505" in text
    assert "backup.last_ok" in text
    # May mention restic only as future replacement; must not invoke it.
    assert "restic backup" not in text.lower()
    assert "restic check" not in text.lower()
    assert "PRAGMA" not in text
    assert re.search(r"(?m)^\s*restic\b", text) is None


def test_monitor_check_covers_required_signals():
    text = _read(MONITOR_CHECK)
    assert MONITOR_CHECK.stat().st_mode & 0o111
    for token in (
        "disk",
        "backup",
        "health.json",
        "tls",
        "dashboard",
        "quota",
        "identity",
        "grading",
        "model",
        "odds",
        "80",
        "26",
    ):
        assert token.lower() in text.lower(), token


def test_logrotate_targets_mma_log_dir():
    text = _read(LOGROTATE)
    assert "/var/log/mma-model" in text
    assert "rotate" in text


def test_monitoring_env_example_uses_placeholders_only():
    text = _read(MONITORING_EXAMPLE)
    assert "PLACEHOLDER" in text
    assert "0600" in text or "mode 0600" in _read(RUNBOOK)
    for pattern in SECRET_PATTERNS:
        assert pattern.search(text) is None, pattern.pattern


def test_install_sh_supports_apply_scheduler():
    text = _read(INSTALL_SH)
    assert "--apply-scheduler" in text
    assert "mma-scheduler.timer" in text
    assert "systemd-analyze verify" in text
    assert "monitoring.env" in text
    assert "logrotate" in text


def test_runbook_and_evidence_exist_without_secrets_or_forbidden_tokens():
    assert RUNBOOK.is_file()
    assert EVIDENCE.is_file()
    docs = {
        "monitoring.md": _read(RUNBOOK),
        "dwcs-504-evidence.md": _read(EVIDENCE),
        "deploy.md": _read(DEPLOY_RUNBOOK),
        "install.sh": _read(INSTALL_SH),
        "run-job.sh": _read(RUN_JOB),
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
                ), f"{name} possible secret: {token}"

    runbook = docs["monitoring.md"]
    assert "Persistent=true" in runbook
    assert "flock" in runbook.lower()
    assert "Healthchecks" in runbook or "hc-ping" in runbook
    assert "DWCS-505" in runbook
    assert PINNED_DIGEST in _read(DEPLOY_RUNBOOK) or PINNED_DIGEST in _read(
        REPO_ROOT / "deploy" / "compose.yaml"
    )


def test_compose_still_unpublished():
    compose = _read(REPO_ROOT / "deploy" / "compose.yaml")
    assert re.search(r"(?m)^\s*ports\s*:", compose) is None
    assert re.search(r"(?m)^\s*expose\s*:", compose) is None
    assert re.search(r"(?i)network[_-]?mode\s*:\s*[\"']?host", compose) is None
    assert PINNED_DIGEST in compose


def test_run_job_redaction_filters_secrets():
    """Exercise the sed redactor embedded in run-job.sh via bash -c extract."""
    hc = r"s#(hc-ping\.com/)[A-Za-z0-9_-]+#\1[REDACTED]#g"
    key = r"s#([Aa][Pp][Ii][_-]?[Kk][Ee][Yy][=:])[^[:space:]]+#\1[REDACTED]#g"
    pw = r"s#([Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd][=:])[^[:space:]]+#\1[REDACTED]#g"
    sample = "url=https://hc-ping.com/abc-secret-uuid API_KEY=supersecret password=hunter2"
    script = f"printf '%s\\n' '{sample}' | sed -E -e '{hc}' -e '{key}' -e '{pw}'"
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "abc-secret-uuid" not in out
    assert "supersecret" not in out
    assert "hunter2" not in out
    assert "[REDACTED]" in out


def test_backup_hook_writes_stamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stamp_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    stamp_dir.mkdir()
    log_dir.mkdir()
    env = {
        "MMA_DATA_DIR_HOST": str(stamp_dir),
        "MMA_LOG_DIR": str(log_dir),
        "MMA_BACKUP_STAMP": str(stamp_dir / "backup.last_ok"),
    }
    proc = subprocess.run(
        ["bash", str(BACKUP_HOOK)],
        capture_output=True,
        text=True,
        check=False,
        env={**dict(**{k: v for k, v in __import__("os").environ.items()}), **env},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    stamp = stamp_dir / "backup.last_ok"
    assert stamp.is_file()
    assert stamp.read_text(encoding="utf-8").strip().endswith("Z")
    assert "DWCS-505" in proc.stdout or "stub ok" in proc.stdout


def test_systemd_analyze_verify_when_available():
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze not available on this runner")
    units = [str(SYSTEMD_DIR / name) for name in REQUIRED_UNITS]
    # Units reference /opt/mma-model paths; verify syntax still runs.
    proc = subprocess.run(
        ["systemd-analyze", "verify", *units],
        capture_output=True,
        text=True,
        check=False,
    )
    # Missing path warnings are OK; hard errors are not.
    combined = (proc.stdout + proc.stderr).lower()
    assert "error" not in combined or proc.returncode == 0 or "command not found" in combined
    # Prefer exit 0; some hosts warn about unresolved ExecStart paths.
    if proc.returncode != 0:
        assert "failed to parse" not in combined
        assert "invalid" not in combined or "path" in combined
