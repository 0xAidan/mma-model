"""Link result versions to exact raw observations and clear invalid reversal proxies.

Revision ID: 0010_result_version_provenance
Revises: 0009_history_constraints
Create Date: 2026-08-12

Upgrade adds nullable exact provenance on ``bout_result_versions``, backfills
only unique matches, and removes fight-night publication proxies from current
reversal observations. Downgrade restores stashed proxy claims and drops the
new columns without deleting pre-ticket rows.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision: str = "0010_result_version_provenance"
down_revision: Union[str, Sequence[str], None] = "0009_history_constraints"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

AUDIT_UNKNOWN = "correction_publication_unknown"
AUDIT_PROXY = "cleared_proxy_published_at"
AUDIT_QUALITY = "cleared_timestamp_quality"
AUDIT_TIER = "cleared_quality_tier"


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    if table not in set(inspect(bind).get_table_names()):
        return set()
    return {col["name"] for col in inspect(bind).get_columns(table)}


def _parse_sql_dt(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text_value = str(value).strip()
        if not text_value:
            return None
        text_value = text_value.replace("Z", "+00:00")
        if "T" not in text_value and " " in text_value:
            text_value = text_value.replace(" ", "T", 1)
        dt = datetime.fromisoformat(text_value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_attrs(raw: object) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        loaded = json.loads(str(raw))
    except json.JSONDecodeError:
        return {"malformed_attributes": True, "malformed_original": str(raw)}
    return loaded if isinstance(loaded, dict) else {"malformed_attributes": True}


def _result_type(attrs: dict[str, Any], fallback: object) -> str:
    value = attrs.get("result_type")
    if value in (None, ""):
        value = fallback
    return str(value or "")


def _backfill_provenance() -> None:
    conn = op.get_bind()
    versions = list(
        conn.execute(
            text(
                "SELECT id, bout_id, version_kind, result_type, effective_at, observed_at "
                "FROM bout_result_versions"
            )
        )
    )
    observations = list(
        conn.execute(
            text(
                "SELECT id, subject_id, version_kind, observed_at, effective_at, "
                "attributes_json FROM raw_observations "
                "WHERE entity_kind = 'bout_result' OR entity_kind = '' OR entity_kind IS NULL"
            )
        )
    )
    obs_rows = []
    for row in observations:
        attrs = _load_attrs(row[5])
        obs_rows.append(
            {
                "id": int(row[0]),
                "subject_id": str(row[1] or ""),
                "version_kind": str(row[2] or ""),
                "observed_at": _parse_sql_dt(row[3]),
                "effective_at": _parse_sql_dt(row[4]),
                "result_type": _result_type(attrs, None),
            }
        )
    claimed: dict[int, list[int]] = {}
    assignments: dict[int, tuple[str, int | None]] = {}
    for version in versions:
        version_id = int(version[0])
        bout_id = str(version[1] or "")
        version_kind = str(version[2] or "")
        result_type = str(version[3] or "")
        effective_at = _parse_sql_dt(version[4])
        observed_at = _parse_sql_dt(version[5])
        matches = [
            item["id"]
            for item in obs_rows
            if item["subject_id"] == bout_id
            and item["version_kind"] == version_kind
            and item["observed_at"] == observed_at
            and item["effective_at"] == effective_at
            and item["result_type"] == result_type
        ]
        unique_matches = list(dict.fromkeys(matches))
        if len(unique_matches) == 1:
            obs_id = unique_matches[0]
            claimed.setdefault(obs_id, []).append(version_id)
            assignments[version_id] = ("linked", obs_id)
        elif len(unique_matches) > 1:
            assignments[version_id] = ("ambiguous", None)
        else:
            assignments[version_id] = ("unknown", None)
    for _obs_id, version_ids in claimed.items():
        if len(version_ids) == 1:
            continue
        for version_id in version_ids:
            assignments[version_id] = ("ambiguous", None)
    for version_id, (status, obs_id) in assignments.items():
        conn.execute(
            text(
                "UPDATE bout_result_versions "
                "SET provenance_status = :status, raw_observation_id = :obs_id "
                "WHERE id = :version_id"
            ),
            {"status": status, "obs_id": obs_id, "version_id": version_id},
        )


def _clear_invalid_reversal_proxies() -> None:
    conn = op.get_bind()
    night_by_bout: dict[str, str] = {}
    current_by_bout: dict[str, str] = {}
    for row in conn.execute(
        text(
            "SELECT bout_id, version_kind, revision, result_type "
            "FROM bout_result_versions ORDER BY revision ASC"
        )
    ):
        bout_id = str(row[0] or "")
        kind = str(row[1] or "")
        result_type = str(row[3] or "")
        if kind == "event_night":
            night_by_bout[bout_id] = result_type
        elif kind == "current":
            current_by_bout[bout_id] = result_type
    rows = list(
        conn.execute(
            text(
                "SELECT id, subject_id, version_kind, timestamp_quality, "
                "proxy_published_at, quality_tier, attributes_json "
                "FROM raw_observations"
            )
        )
    )
    for row in rows:
        obs_id = int(row[0])
        subject_id = str(row[1] or "")
        version_kind = str(row[2] or "")
        timestamp_quality = str(row[3] or "")
        proxy_published_at = row[4]
        quality_tier = row[5]
        attrs = _load_attrs(row[6])
        if version_kind != "current":
            continue
        if timestamp_quality != "publication_proxy" or proxy_published_at in (None, ""):
            continue
        reversed_state = attrs.get("version_state") == "reversed_to_no_contest"
        night_result = night_by_bout.get(subject_id)
        current_result = current_by_bout.get(subject_id)
        reversed_lanes = night_result in {"decisive", "draw"} and current_result == "no_contest"
        if not reversed_state and not reversed_lanes:
            continue
        if attrs.get(AUDIT_UNKNOWN) is True:
            continue
        attrs[AUDIT_UNKNOWN] = True
        attrs[AUDIT_PROXY] = str(proxy_published_at)
        attrs[AUDIT_QUALITY] = timestamp_quality
        if quality_tier not in (None, ""):
            attrs[AUDIT_TIER] = str(quality_tier)
        conn.execute(
            text(
                "UPDATE raw_observations SET proxy_published_at = NULL, "
                "timestamp_quality = 'unknown', quality_tier = 'bronze', "
                "attributes_json = :attrs WHERE id = :obs_id"
            ),
            {"attrs": json.dumps(attrs, sort_keys=True), "obs_id": obs_id},
        )


def _restore_cleared_proxies() -> None:
    conn = op.get_bind()
    rows = list(conn.execute(text("SELECT id, attributes_json FROM raw_observations")))
    for row in rows:
        attrs = _load_attrs(row[1])
        if attrs.get(AUDIT_UNKNOWN) is not True:
            continue
        proxy = attrs.pop(AUDIT_PROXY, None)
        quality = attrs.pop(AUDIT_QUALITY, None) or "publication_proxy"
        tier = attrs.pop(AUDIT_TIER, None) or "silver"
        attrs.pop(AUDIT_UNKNOWN, None)
        conn.execute(
            text(
                "UPDATE raw_observations SET proxy_published_at = :proxy, "
                "timestamp_quality = :quality, quality_tier = :tier, "
                "attributes_json = :attrs WHERE id = :obs_id"
            ),
            {
                "proxy": proxy,
                "quality": quality,
                "tier": tier,
                "attrs": json.dumps(attrs, sort_keys=True),
                "obs_id": int(row[0]),
            },
        )


def upgrade() -> None:
    existing = _tables()
    if "bout_result_versions" not in existing:
        return
    cols = _columns("bout_result_versions")
    with op.batch_alter_table("bout_result_versions") as batch:
        if "raw_observation_id" not in cols:
            batch.add_column(sa.Column("raw_observation_id", sa.Integer(), nullable=True))
        if "provenance_status" not in cols:
            batch.add_column(
                sa.Column(
                    "provenance_status",
                    sa.String(length=32),
                    nullable=False,
                    server_default="unknown",
                )
            )
        batch.create_foreign_key(
            "fk_bout_result_version_raw_observation",
            "raw_observations",
            ["raw_observation_id"],
            ["id"],
        )
        batch.create_unique_constraint(
            "uq_bout_result_version_raw_observation",
            ["raw_observation_id"],
        )
        batch.create_check_constraint(
            "ck_bout_result_provenance_status",
            "provenance_status IN ('linked', 'unknown', 'ambiguous')",
        )
        batch.create_check_constraint(
            "ck_bout_result_provenance_link",
            "(provenance_status = 'linked' AND raw_observation_id IS NOT NULL) "
            "OR (provenance_status IN ('unknown', 'ambiguous') "
            "AND raw_observation_id IS NULL)",
        )
    _backfill_provenance()
    _clear_invalid_reversal_proxies()


def downgrade() -> None:
    existing = _tables()
    if "bout_result_versions" not in existing:
        return
    _restore_cleared_proxies()
    cols = _columns("bout_result_versions")
    if "raw_observation_id" not in cols and "provenance_status" not in cols:
        return
    with op.batch_alter_table("bout_result_versions") as batch:
        batch.drop_constraint("ck_bout_result_provenance_link", type_="check")
        batch.drop_constraint("ck_bout_result_provenance_status", type_="check")
        batch.drop_constraint("uq_bout_result_version_raw_observation", type_="unique")
        batch.drop_constraint("fk_bout_result_version_raw_observation", type_="foreignkey")
        if "raw_observation_id" in cols:
            batch.drop_column("raw_observation_id")
        if "provenance_status" in cols:
            batch.drop_column("provenance_status")
