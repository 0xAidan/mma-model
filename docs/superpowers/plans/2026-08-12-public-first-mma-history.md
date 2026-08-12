# Public-first MMA historical data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Phase 1 DWCS-102 through DWCS-106 under the approved public-first hybrid source policy without weakening identity, leakage, provenance, explicit-missingness, or coverage gates.

**Architecture:** Adapters emit DWCS-101 `SourceObservationRecord` rows into `IngestRepository` + content-addressed raw blobs. UFCStats direct snapshots are canonical for UFC/DWCS; mma-ai dumps are bootstrap-only after hash/count/schema reconciliation; Tapology/Sherdog/Combat Registry enrich regional history; identity is exact-ID/Wikidata-first with a reversible review queue; DWCS-106 publishes gold/silver/bronze/missing/conflict health and fail-closed strict exits.

**Tech Stack:** Python 3.11, SQLAlchemy, Alembic, pydantic, httpx, pytest, Ruff, SQLite WAL, existing `mma-model` CLI.

## Global Constraints

- Policy contract: `config/sources/source_policy_v1.json` (`policy_mode=public_first_hybrid_personal_project`).
- `decision.primary` stays `null` until a measured audit passes; do not invent licensed adoption.
- Universe: 89 cards / 440 bouts; every exclusion categorized.
- Reconciliation ≥0.98 where comparable; result agreement ≥0.99; zero unresolved evaluated/upcoming identity conflicts; zero future-row leakage failures; zero mutable-current historical feature leakage.
- Never bypass logins, paywalls, CAPTCHAs, robots/access controls, or technical restrictions.
- Never backdate `observed_at`. Separate acquisition, source-update, effective, and proxy clocks.
- Mappers populate first-class `quality_tier`, `timestamp_quality`, `timestamp_quality_source`, and `proxy_published_at` (plus the other contract clocks/hash/ref). `attributes` / `attributes_json` are source-specific non-contract metadata only and must never shadow reserved contract keys.
- Never train on opaque precomputed feature CSVs.
- No planned source file may exceed ~1000 lines; split client/parser/mapper/cli.
- CI: no live network; fixtures only. Live probes are operator-flagged only.
- Preserve existing CLI commands (`init-db`, `sync`, `odds`, `train`, `predict-fight`, `backtest`) unless a ticket adds tested wrappers.
- Do not commit `.env`, raw licensed payloads, DB files, or secrets.

### File map (create / modify)

| Path | Owner ticket | Responsibility |
|------|--------------|----------------|
| `config/sources/source_policy_v1.json` | prerequisite (already merged) | Canonical IDs + observation metadata + persistence contract |
| `src/mma_model/sources/policy.py` | prerequisite (already merged) | Deeply immutable fail-closed loader |
| `config/sources/pit_proxy_v1.json` | DWCS-102 Task 1 | Frozen publication-proxy rule |
| `config/sources/http_politeness_v1.json` | DWCS-102 Task 1 | Per-host rate/backoff/UA |
| `migrations/versions/0006_observation_pit_metadata.py` | DWCS-102 Task 2 | Persist four-clock PIT/quality/attributes |
| `src/mma_model/sources/http/polite_client.py` | DWCS-102 | Shared polite HTTP |
| `src/mma_model/sources/http/block_signals.py` | DWCS-102 | Detect/stop on blocks |
| `src/mma_model/sources/ufcstats_public/client.py` | DWCS-102 | Snapshot GET + cache |
| `src/mma_model/sources/ufcstats_public/parser.py` | DWCS-102 | HTML → typed dicts |
| `src/mma_model/sources/ufcstats_public/mapper.py` | DWCS-102 | dicts → `SourceObservationRecord` |
| `src/mma_model/sources/ufcstats_public/adapter.py` | DWCS-102 | Orchestrate fetch/parse/map |
| `src/mma_model/sources/mma_ai_bootstrap/reconcile.py` | DWCS-102 | Hash/count/schema gates |
| `src/mma_model/sources/mma_ai_bootstrap/importer.py` | DWCS-102 | Import only after pass |
| `src/mma_model/dwcs/manifest.py` | DWCS-103 | Load frozen manifests |
| `src/mma_model/dwcs/classification.py` | DWCS-103 | Series/result classes |
| `src/mma_model/dwcs/ingest.py` | DWCS-103 | Manifest-first sync |
| `src/mma_model/identity/normalize.py` | DWCS-104 | Unicode-safe normalize |
| `src/mma_model/identity/resolver.py` | DWCS-104 | Exact-ID rules |
| `src/mma_model/identity/review.py` | DWCS-104 | Reversible queue |
| `src/mma_model/db/tables/identity.py` | DWCS-104 | Queue + evidence tables |
| `src/mma_model/sources/tapology_public/` | DWCS-105 | Regional primary breadth |
| `src/mma_model/sources/sherdog_public/` | DWCS-105 | Secondary reconcile |
| `src/mma_model/sources/combat_registry/` | DWCS-105 | Authoritative validation |
| `src/mma_model/history/reconstruct.py` | DWCS-105 | Pre-fight from prior bouts |
| `src/mma_model/quality/coverage.py` | DWCS-106 | Tiered coverage |
| `src/mma_model/quality/gates.py` | DWCS-106 | Strict thresholds |
| `src/mma_model/quality/leakage.py` | DWCS-106 | Future-row invariance |
| `output/contracts/coverage.schema.json` | DWCS-106 | Coverage JSON schema |
| `src/mma_model/cli.py` | DWCS-102–106 | Subcommands |

---

### Prerequisite already merged (do not recreate)

These files are already present from the DWCS-003 policy PR and must be treated as
given inputs for every later task:

- `config/sources/source_policy_v1.json` (canonical source IDs + observation metadata + `dwcs_102_persistence`)
- `src/mma_model/sources/policy.py` (`load_source_policy`, frozen `SourcePolicy`, fail-closed nested validation)
- `tests/sources/test_source_policy.py`
- Design/plan/ADR/scorecard handoff updates

Canonical source IDs (exhaustive; no aliases): `ufcstats_public`, `mma_ai_bootstrap`,
`tapology_public`, `sherdog_public`, `combat_registry`, `wikidata`,
`bestfightodds_archive`, `the_odds_api`, `sportsdataio`, `balldontlie`,
`explicit_missing`.

---

### Task 1: PIT proxy + HTTP politeness contracts (DWCS-102 prelude)

**Files:**
- Already present: `config/sources/source_policy_v1.json`, `src/mma_model/sources/policy.py`, `tests/sources/test_source_policy.py`
- Create: `config/sources/pit_proxy_v1.json`
- Create: `config/sources/http_politeness_v1.json`
- Create: `src/mma_model/sources/pit_proxy.py`
- Create: `src/mma_model/sources/http_politeness.py`
- Create: `tests/sources/test_pit_proxy.py`
- Create: `tests/sources/test_http_politeness.py`
- Modify: `src/mma_model/sources/__init__.py` (export new loaders only; do not rewrite `load_source_policy`)

**Interfaces:**
- Consumes: `load_source_policy()` already merged; `observation_metadata.quality_tier_values` / `timestamp_quality_values`
- Produces:
  - `load_pit_proxy_rule(path: Path | None = None) -> PitProxyRule`
  - `PitProxyRule(rule_id: str, rule_version: str, delay_iso8601: str, applies_to: tuple[str, ...], forbidden_for: tuple[str, ...], max_quality_tier_when_proxy: Literal["silver"])`
  - `load_http_politeness(path: Path | None = None) -> HttpPolitenessConfig`
  - `HttpPolitenessConfig` frozen model with per-host `min_delay_sec`, `max_concurrency`, `max_retries`, `backoff_base_sec`, `backoff_cap_sec`, `user_agent`, `contact`, `stop_status_codes`

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path
import pytest
from mma_model.sources.policy import load_source_policy

ROOT = Path(__file__).resolve().parents[2]

def test_load_pit_proxy_rule_silver_ceiling():
    from mma_model.sources.pit_proxy import load_pit_proxy_rule
    policy = load_source_policy()
    rule = load_pit_proxy_rule(ROOT / "config/sources/pit_proxy_v1.json")
    assert rule.rule_id == "event_completion_plus_delay"
    assert rule.delay_iso8601 == "P1D"
    assert rule.max_quality_tier_when_proxy == "silver"
    assert rule.max_quality_tier_when_proxy in policy.observation_metadata.quality_tier_values

def test_load_http_politeness_requires_contact_and_stop_codes():
    from mma_model.sources.http_politeness import load_http_politeness
    cfg = load_http_politeness(ROOT / "config/sources/http_politeness_v1.json")
    assert cfg.hosts["ufcstats.com"].min_delay_sec >= 0.75
    assert 403 in cfg.hosts["ufcstats.com"].stop_status_codes
    assert cfg.user_agent
    assert cfg.contact
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/sources/test_pit_proxy.py::test_load_pit_proxy_rule_silver_ceiling -v`  
Expected: FAIL with `ModuleNotFoundError` for `mma_model.sources.pit_proxy` (policy loader already imports successfully)

- [ ] **Step 3: Write minimal implementation**

Create `pit_proxy_v1.json`:

```json
{
  "rule_id": "event_completion_plus_delay",
  "rule_version": "1",
  "delay_iso8601": "P1D",
  "applies_to": ["immutable_bout_result", "immutable_bout_stat"],
  "forbidden_for": ["mutable_profile_aggregate"],
  "max_quality_tier_when_proxy": "silver"
}
```

Create `http_politeness_v1.json` with hosts `ufcstats.com`, `tapology.com`, `sherdog.com`, `combatreg.com`, `bestfightodds.com` each defining `min_delay_sec`, `max_concurrency=1`, `max_retries`, `backoff_base_sec`, `backoff_cap_sec`, `stop_status_codes=[403,429,503]`, plus top-level `user_agent` and `contact`. Implement frozen loaders; do not recreate `policy.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/sources/test_pit_proxy.py tests/sources/test_http_politeness.py tests/sources/test_source_policy.py -q`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config/sources/pit_proxy_v1.json config/sources/http_politeness_v1.json \
  src/mma_model/sources/pit_proxy.py src/mma_model/sources/http_politeness.py \
  src/mma_model/sources/__init__.py \
  tests/sources/test_pit_proxy.py tests/sources/test_http_politeness.py
git commit -m "feat(sources): Add PIT proxy and HTTP politeness contracts"
```

---

### Task 2: Persist four-clock PIT + quality metadata (DWCS-102)

**Files:**
- Create: `migrations/versions/0006_observation_pit_metadata.py`
- Modify: `src/mma_model/db/tables/provenance.py` (`RawObservation` columns)
- Modify: `src/mma_model/sources/contracts.py` (`SourceObservationRecord` first-class PIT/quality fields)
- Modify: `src/mma_model/ingest/repository.py` (persist attributes + new columns; round-trip)
- Create: `tests/ingest/test_pit_metadata_roundtrip.py`
- Modify: `tests/ingest/test_repository.py` only if existing fixtures need new required fields

**Interfaces:**
- Consumes: `load_source_policy().observation_metadata` and `.dwcs_102_persistence`
- Produces updated `SourceObservationRecord` fields:
  - `observed_at: datetime`
  - `source_published_at: datetime | None`
  - `source_updated_at: datetime | None`
  - `effective_at: datetime`
  - `proxy_published_at: datetime | None`
  - `timestamp_quality: TimestampQualityId`
  - `timestamp_quality_source: str | None`
  - `quality_tier: QualityTierId`
  - `attributes: Mapping[str, object]` (frozen; source-specific only — never reserved contract keys)
  - `payload_hash: str`, `raw_ref: str | None`, `raw_blob_absent: bool`
- `RawObservation` gains matching nullable/typed columns plus `attributes_json: str` (JSON object of non-contract keys only)
- `IngestRepository.commit_batch` must persist all first-class PIT/quality columns and `attributes_json`; reading back must equal written silver and gold rows; reject commits whose `attributes` contain any `observation_metadata.reserved_attribute_keys`

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime, timezone
from mma_model.sources.contracts import DetailLevel, SourceObservationRecord
from mma_model.sources.policy import load_source_policy

def test_round_trip_silver_vs_gold_quality_tier(repo, raw_store):
    policy = load_source_policy()
    assert "round_trip_silver_vs_gold_quality_tier" in policy.dwcs_102_persistence.required_tests
    observed = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)
    gold = SourceObservationRecord(
        source="ufcstats_public",
        stream="fight_details",
        external_id="fight-gold",
        entity_kind="bout_stat",
        observed_at=observed,
        source_published_at=datetime(2019, 1, 2, tzinfo=timezone.utc),
        source_updated_at=datetime(2019, 1, 2, tzinfo=timezone.utc),
        effective_at=datetime(2019, 1, 1, tzinfo=timezone.utc),
        proxy_published_at=None,
        timestamp_quality="direct_source_timestamp",
        timestamp_quality_source="ufcstats_public",
        quality_tier="gold",
        payload_hash="a" * 64,
        raw_ref="a" * 64,
        detail_level=DetailLevel.VERIFIED,
        attributes={"significant_strikes_landed": 12},
    )
    silver = gold.model_copy(update={
        "external_id": "fight-silver",
        "source_published_at": None,
        "proxy_published_at": datetime(2019, 1, 2, tzinfo=timezone.utc),
        "timestamp_quality": "publication_proxy",
        "timestamp_quality_source": "event_completion_plus_delay@1",
        "quality_tier": "silver",
        "payload_hash": "b" * 64,
        "raw_ref": "b" * 64,
    })
    # commit both via repository; reload; assert quality_tier and proxy/publish clocks survive
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ingest/test_pit_metadata_roundtrip.py::test_round_trip_silver_vs_gold_quality_tier -v`  
Expected: FAIL because `SourceObservationRecord` lacks PIT/quality fields and/or repository drops attributes

- [ ] **Step 3: Write minimal implementation**

Add migration `0006_observation_pit_metadata` columns listed in `dwcs_102_persistence.table_columns.raw_observations`. Extend the contract and repository so commit/read round-trips preserve silver vs gold. Reject `detail_level=verified` when required metadata from `observation_metadata` is missing. Do not implement HTTP adapters in this task.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/ingest/test_pit_metadata_roundtrip.py tests/ingest/test_repository.py tests/db/test_migrations.py -q`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/0006_observation_pit_metadata.py \
  src/mma_model/db/tables/provenance.py src/mma_model/sources/contracts.py \
  src/mma_model/ingest/repository.py tests/ingest/test_pit_metadata_roundtrip.py
git commit -m "feat(ingest): Persist four-clock PIT and quality metadata"
```

---

### Task 3: Polite HTTP client with block-signal stop (DWCS-102)

**Files:**
- Create: `src/mma_model/sources/http/__init__.py`
- Create: `src/mma_model/sources/http/block_signals.py`
- Create: `src/mma_model/sources/http/polite_client.py`
- Create: `tests/sources/http/test_polite_client.py`
- Create: `tests/fixtures/sources/http/ok.html`
- Create: `tests/fixtures/sources/http/captcha_interstitial.html`

**Interfaces:**
- Produces:
  - `class SourceBlockedError(RuntimeError)` with `reason: str`, `host: str`, `status_code: int | None`
  - `detect_block_signal(status_code: int | None, body_text: str, robots_disallow: bool) -> str | None`
  - `class PoliteHttpClient:`
    - `__init__(self, *, host: str, politeness: HttpPolitenessConfig, cache_dir: Path)`
    - `get_text(self, url: str) -> tuple[str, str]`  # returns `(text, sha256_hex)`
    - stops and raises `SourceBlockedError` on block signals
    - exponential backoff on transient 5xx/429 then kill after `max_retries`

- [ ] **Step 1: Write the failing test**

```python
from mma_model.sources.http.block_signals import detect_block_signal

def test_detect_captcha_interstitial():
    html = "<html><body>Please complete the CAPTCHA to continue</body></html>"
    assert detect_block_signal(200, html, False) == "captcha_interstitial"

def test_detect_robots_disallow():
    assert detect_block_signal(200, "ok", True) == "robots_disallow"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/sources/http/test_polite_client.py::test_detect_captcha_interstitial -v`  
Expected: FAIL import or assertion

- [ ] **Step 3: Write minimal implementation**

Implement detectors for: status in stop list, body markers (`captcha`, `cf-browser-verification`, `access denied`), and `robots_disallow`. `PoliteHttpClient.get_text` must: enforce min delay, send configured UA, hash body with SHA-256, write gzip cache under `cache_dir / host / <hash>.gz`, never follow auth walls.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/sources/http/test_polite_client.py -q`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mma_model/sources/http tests/sources/http tests/fixtures/sources/http
git commit -m "feat(sources): Add polite HTTP client with block stops"
```

---

### Task 4: UFCStats public parser + mapper (DWCS-102)

**Files:**
- Create: `src/mma_model/sources/ufcstats_public/__init__.py`
- Create: `src/mma_model/sources/ufcstats_public/errors.py`
- Create: `src/mma_model/sources/ufcstats_public/parser.py`
- Create: `src/mma_model/sources/ufcstats_public/mapper.py`
- Create: `tests/sources/ufcstats_public/test_parser.py`
- Create: `tests/sources/ufcstats_public/test_mapper.py`
- Create: `tests/fixtures/sources/ufcstats/event_details_sample.html`
- Create: `tests/fixtures/sources/ufcstats/fight_details_sample.html`
- Create: `tests/fixtures/sources/ufcstats/fight_details_schema_drift.html`

**Interfaces:**
- Consumes: `load_source_policy().observation_metadata.reserved_attribute_keys`, `PitProxyRule`
- Produces:
  - `class ParserSchemaDriftError(ValueError)`
  - `class ReservedAttributeKeyError(ValueError)`
  - `parse_event_details(html: str) -> dict[str, object]`
  - `parse_fight_details(html: str) -> dict[str, object]`
  - `map_fight_to_observations(*, parsed: dict[str, object], observed_at: datetime, effective_at: datetime, source_published_at: datetime | None, source_updated_at: datetime | None, proxy: PitProxyRule | None, payload_hash: str) -> list[SourceObservationRecord]`
  - First-class fields on every returned record: `quality_tier`, `timestamp_quality`, `timestamp_quality_source`, `proxy_published_at`, `source_published_at`, `source_updated_at`, `observed_at`, `effective_at`
  - `attributes` / later `attributes_json` hold **only** source-specific non-contract stats/payload keys (e.g. `significant_strikes_landed`); reserved contract keys are rejected
  - source name constant: `SOURCE_UFCSTATS_PUBLIC = "ufcstats_public"`

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime, timezone
from pathlib import Path
import pytest
from mma_model.sources.policy import load_source_policy
from mma_model.sources.ufcstats_public.parser import parse_fight_details, ParserSchemaDriftError
from mma_model.sources.ufcstats_public.mapper import (
    map_fight_to_observations,
    ReservedAttributeKeyError,
)
from mma_model.sources.pit_proxy import load_pit_proxy_rule

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/sources/ufcstats"

def test_parse_fight_details_sample():
    html = (FIXTURES / "fight_details_sample.html").read_text(encoding="utf-8")
    parsed = parse_fight_details(html)
    assert parsed["external_fight_id"]
    assert parsed["fighter_a"]["name"]
    assert parsed["fighter_b"]["name"]
    assert "significant_strikes_landed" in parsed["fighter_a"]["stats"]

def test_schema_drift_raises():
    html = (FIXTURES / "fight_details_schema_drift.html").read_text(encoding="utf-8")
    with pytest.raises(ParserSchemaDriftError):
        parse_fight_details(html)

def test_mapper_sets_first_class_pit_and_quality_fields():
    policy = load_source_policy()
    proxy = load_pit_proxy_rule()
    html = (FIXTURES / "fight_details_sample.html").read_text(encoding="utf-8")
    parsed = parse_fight_details(html)
    observed = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)
    rows = map_fight_to_observations(
        parsed=parsed,
        observed_at=observed,
        effective_at=datetime(2019, 1, 1, tzinfo=timezone.utc),
        source_published_at=None,
        source_updated_at=None,
        proxy=proxy,
        payload_hash="a" * 64,
    )
    assert rows
    for row in rows:
        assert row.quality_tier in policy.observation_metadata.quality_tier_values
        assert row.timestamp_quality in policy.observation_metadata.timestamp_quality_values
        assert row.timestamp_quality_source is not None
        assert row.observed_at == observed
        assert row.observed_at != row.effective_at
        if row.timestamp_quality == "publication_proxy":
            assert row.proxy_published_at is not None
            assert row.quality_tier == "silver"
        for reserved in policy.observation_metadata.reserved_attribute_keys:
            assert reserved not in row.attributes
        assert "significant_strikes_landed" in row.attributes

def test_mapper_rejects_reserved_attribute_key_collision():
    policy = load_source_policy()
    assert "quality_tier" in policy.observation_metadata.reserved_attribute_keys
    parsed = {
        "external_fight_id": "x",
        "fighter_a": {"name": "A", "stats": {"quality_tier": "gold"}},
        "fighter_b": {"name": "B", "stats": {}},
    }
    with pytest.raises(ReservedAttributeKeyError, match="quality_tier"):
        map_fight_to_observations(
            parsed=parsed,
            observed_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
            effective_at=datetime(2019, 1, 1, tzinfo=timezone.utc),
            source_published_at=None,
            source_updated_at=None,
            proxy=None,
            payload_hash="b" * 64,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/sources/ufcstats_public/test_mapper.py::test_mapper_sets_first_class_pit_and_quality_fields -v`  
Expected: FAIL (module missing or first-class fields absent)

- [ ] **Step 3: Write minimal implementation**

Reuse patterns from `src/mma_model/ufcstats/parsers.py` but isolate under `ufcstats_public/` with explicit required column labels. On missing required labels raise `ParserSchemaDriftError` (do not return partial as `verified`). Mapper sets `detail_level=VERIFIED` only when all required fields present. Populate first-class `quality_tier`, `timestamp_quality`, `timestamp_quality_source`, and `proxy_published_at` on `SourceObservationRecord` (`gold` + `direct_source_timestamp` when `source_published_at` or revision timestamp exists; else apply `PitProxyRule` → `silver` + `publication_proxy` + `proxy_published_at`; else `bronze` + `unknown`). Never put those contract fields in `attributes`. Never set `observed_at` from event date. If a source-specific key collides with `reserved_attribute_keys`, raise `ReservedAttributeKeyError` (do not silently rename into attributes).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/sources/ufcstats_public -q`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mma_model/sources/ufcstats_public tests/sources/ufcstats_public tests/fixtures/sources/ufcstats
git commit -m "feat(ufcstats): Add public snapshot parser and mapper"
```

---

### Task 5: UFCStats adapter + CLI audit + repository integration (DWCS-102)

**Files:**
- Create: `src/mma_model/sources/ufcstats_public/client.py`
- Create: `src/mma_model/sources/ufcstats_public/adapter.py`
- Modify: `src/mma_model/cli.py` (add `source audit ufcstats-public`)
- Create: `tests/sources/ufcstats_public/test_adapter.py`

**Interfaces:**
- Produces:
  - `class UfcstatsPublicAdapter:`
    - `audit_manifest_scope(self, *, years: range) -> dict[str, object]`
    - `iter_observations(self, *, event_external_ids: list[str], observed_at: datetime) -> Iterator[SourceObservationRecord]`
  - CLI: `mma-model source audit ufcstats-public --series dwcs --years 2017:2025 --json`

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime, timezone
from mma_model.sources.ufcstats_public.adapter import UfcstatsPublicAdapter

def test_adapter_maps_fixture_without_network(tmp_path):
    adapter = UfcstatsPublicAdapter.for_fixtures(fixture_root=tmp_path)
    # copy sample html into tmp_path layout in test setup
    rows = list(adapter.iter_observations(event_external_ids=["evt1"], observed_at=datetime(2026, 8, 12, tzinfo=timezone.utc)))
    assert rows
    assert all(r.source == "ufcstats_public" for r in rows)
    assert all(r.observed_at.year == 2026 for r in rows)
    assert all(r.quality_tier in {"gold", "silver", "bronze"} for r in rows)
    assert all(r.timestamp_quality_source is not None for r in rows)
    assert all("quality_tier" not in r.attributes for r in rows)
```


- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/sources/ufcstats_public/test_adapter.py -v`  
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

`for_fixtures` bypasses HTTP and reads local HTML. Live client uses `PoliteHttpClient` for `ufcstats.com`. Persist raw via `ContentAddressedRawStore.put`. Wire CLI subcommand that prints counts of mapped/missing/blocked and exits `2` on `SourceBlockedError`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/sources/ufcstats_public -q`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mma_model/sources/ufcstats_public src/mma_model/cli.py tests/sources/ufcstats_public
git commit -m "feat(ufcstats): Wire public adapter and source audit CLI"
```

---

### Task 6: mma-ai bootstrap reconciliation (DWCS-102)

**Files:**
- Create: `src/mma_model/sources/mma_ai_bootstrap/__init__.py`
- Create: `src/mma_model/sources/mma_ai_bootstrap/reconcile.py`
- Create: `src/mma_model/sources/mma_ai_bootstrap/importer.py`
- Create: `tests/sources/mma_ai_bootstrap/test_reconcile.py`
- Create: `tests/fixtures/sources/mma_ai/normalized_fights_sample.jsonl`
- Create: `tests/fixtures/sources/mma_ai/opaque_features.csv`

**Interfaces:**
- Produces:
  - `class BootstrapReject(ValueError)`
  - `reconcile_mma_ai_dump(*, normalized_path: Path, ufcstats_sample_hashes: dict[str, str], expected_counts: dict[str, int]) -> ReconcileReport`
  - `import_reconciled_observations(report: ReconcileReport) -> list[SourceObservationRecord]`
  - Reject if path suffix/name indicates feature matrix (`*feature*.csv`) or columns match known opaque feature headers

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path
import pytest
from mma_model.sources.mma_ai_bootstrap.reconcile import reconcile_mma_ai_dump, BootstrapReject

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/sources/mma_ai"

def test_reject_opaque_feature_csv():
    with pytest.raises(BootstrapReject, match="opaque_precomputed_feature"):
        reconcile_mma_ai_dump(
            normalized_path=FIXTURES / "opaque_features.csv",
            ufcstats_sample_hashes={},
            expected_counts={},
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/sources/mma_ai_bootstrap/test_reconcile.py::test_reject_opaque_feature_csv -v`  
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Require ≥99% hash agreement on sampled fight pages when hashes provided; row counts must match `expected_counts` exactly for keys present; schema must include fight/event/fighter ids and result fields. `import_reconciled_observations` must emit `SourceObservationRecord` rows with first-class PIT/quality fields (same rules as Task 4); source-specific dump columns go in `attributes` only and must not collide with reserved contract keys. On failure return/raise kill reason `mma_ai_bootstrap` per policy.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/sources/mma_ai_bootstrap -q`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mma_model/sources/mma_ai_bootstrap tests/sources/mma_ai_bootstrap tests/fixtures/sources/mma_ai
git commit -m "feat(sources): Gate mma-ai bootstrap behind reconciliation"
```

---

### Task 7: DWCS manifest-first history ingest (DWCS-103)

**Files:**
- Create: `src/mma_model/dwcs/__init__.py`
- Create: `src/mma_model/dwcs/manifest.py`
- Create: `src/mma_model/dwcs/classification.py`
- Create: `src/mma_model/dwcs/ingest.py`
- Modify: `src/mma_model/cli.py` (`dwcs sync-history`, `coverage` stub ok if DWCS-106 owns full coverage)
- Create: `tests/dwcs/test_ingest_history.py`

**Interfaces:**
- Produces:
  - `load_dwcs_bout_manifest(path: Path) -> list[DwcsBoutManifestRow]`
  - `classify_bout(row: DwcsBoutManifestRow) -> BoutClassification`
  - `sync_dwcs_history(*, through_year: int, adapter: UfcstatsPublicAdapter, repo: IngestRepository) -> SyncHistoryReport`
  - CLI: `mma-model dwcs sync-history --through 2025`

- [ ] **Step 1: Write the failing test**

```python
from mma_model.dwcs.manifest import load_dwcs_bout_manifest
from pathlib import Path

def test_manifest_has_440_bouts():
    rows = load_dwcs_bout_manifest(Path("data/manifests/dwcs_bouts_v1.jsonl"))
    assert len(rows) == 440
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/dwcs/test_ingest_history.py::test_manifest_has_440_bouts -v`  
Expected: FAIL until loader exists (or PASS if loader only — next tests must fail for sync)

- [ ] **Step 3: Write minimal implementation**

Ingest order: (1) manifest rows as observations with source `dwcs_manifest`, (2) UFCStats mapped facts, (3) fail closed on participant/result disagreement (first-class `quality_tier=conflict`, do not overwrite). Distinguish event-night vs current result versions using existing `BoutResultVersion` patterns. Derive elapsed seconds with validation against scheduled rounds.

- [ ] **Step 4: Run tests**

Run: `pytest tests/dwcs -q`  
Expected: PASS; CLI dry-run on fixtures prints `cards=89 bouts=440`

- [ ] **Step 5: Commit**

```bash
git add src/mma_model/dwcs src/mma_model/cli.py tests/dwcs
git commit -m "feat(dwcs): Ingest frozen manifest before provider facts"
```

---

### Task 8: Deterministic identity resolution + review queue (DWCS-104)

**Files:**
- Create: `src/mma_model/identity/__init__.py`
- Create: `src/mma_model/identity/normalize.py`
- Create: `src/mma_model/identity/resolver.py`
- Create: `src/mma_model/identity/review.py`
- Create: `src/mma_model/db/tables/identity.py`
- Create: `migrations/versions/0007_identity_review_queue.py`
- Create: `tests/identity/test_resolver.py`
- Create: `tests/identity/test_review_queue.py`
- Modify: `src/mma_model/cli.py` (`identity audit|approve|reject`)

**Interfaces:**
- Produces:
  - `normalize_person_name(name: str) -> str` (Unicode-preserving NFKC + casefold; keep distinguishing tokens)
  - `resolve_fighter(*, source: str, external_id: str, display_name: str, wikidata_id: str | None, dob: date | None) -> ResolveResult`
  - `ResolveResult.kind in {"linked", "created", "queued", "blocked"}`
  - `enqueue_review(candidate: ReviewCandidate) -> str`  # review id
  - `apply_review_decision(*, review_id: str, decision: Literal["approve","reject"], canonical_id: str | None, actor: str) -> None`
  - Same-name without exact ID ⇒ `queued`, never `linked`

- [ ] **Step 1: Write the failing test**

```python
from mma_model.identity.resolver import resolve_fighter

def test_same_name_without_external_id_queues():
    a = resolve_fighter(source="tapology_public", external_id="1", display_name="John Smith", wikidata_id=None, dob=None)
    b = resolve_fighter(source="sherdog_public", external_id="9", display_name="John Smith", wikidata_id=None, dob=None)
    assert a.kind in {"created", "linked"}
    assert b.kind == "queued"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/identity/test_resolver.py::test_same_name_without_external_id_queues -v`  
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Migration adds `identity_review_queue` and `identity_match_evidence` tables. Resolver checks `fighter_source_ids` exact match first, then Wikidata crosswalk table, then queues fuzzy candidates with evidence JSON. CLI `identity approve` requires `--canonical-id`. Upcoming unresolved identities set `blocked` for scoring consumers.

- [ ] **Step 4: Run tests**

Run: `pytest tests/identity tests/db/test_migrations.py -q`  
Expected: PASS; adjudicated fixture precision/recall documented in test (≥99.5% / ≥98% on fixed sample)

- [ ] **Step 5: Commit**

```bash
git add src/mma_model/identity src/mma_model/db/tables/identity.py migrations/versions/0007_identity_review_queue.py tests/identity src/mma_model/cli.py
git commit -m "feat(identity): Exact-ID resolver with reversible review queue"
```

---

### Task 9: Regional enrichment chain (DWCS-105)

**Files:**
- Create: `src/mma_model/sources/tapology_public/{client,parser,mapper,adapter,errors}.py`
- Create: `src/mma_model/sources/sherdog_public/{client,parser,mapper,adapter,errors}.py`
- Create: `src/mma_model/sources/combat_registry/{client,parser,mapper,adapter}.py`
- Create: `src/mma_model/history/reconstruct.py`
- Create: `src/mma_model/history/frontier.py`
- Create: `docs/data/regional-coverage.md`
- Create: `tests/history/test_reconstruct.py`
- Create: `tests/sources/tapology_public/test_parser.py`
- Create: `tests/sources/sherdog_public/test_parser.py`
- Create: `tests/fixtures/sources/tapology/fighter_public_sample.html`
- Create: `tests/fixtures/sources/sherdog/fighter_public_sample.html`
- Create: `tests/fixtures/sources/combat_registry/results_sample.html`

**Interfaces:**
- Produces:
  - `class RegionalFrontier:` stores pending fighter external URLs with depth/budget
  - `reconstruct_pre_fight_record(*, fighter_id: str, cutoff: datetime, session: Session) -> PreFightRecord`
  - Adapters honor kill criteria; on block raise `SourceBlockedError` and mark source killed in report
  - Fallback order hard-coded from `policy.deterministic_fallback_order`

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime, timezone
from mma_model.history.reconstruct import reconstruct_pre_fight_record

def test_future_bout_does_not_change_prior_record(db_session, fighter_with_two_bouts):
    cutoff = datetime(2020, 1, 1, tzinfo=timezone.utc)
    before = reconstruct_pre_fight_record(fighter_id=fighter_with_two_bouts, cutoff=cutoff, session=db_session)
    # insert 2021 bout via fixture helper
    after = reconstruct_pre_fight_record(fighter_id=fighter_with_two_bouts, cutoff=cutoff, session=db_session)
    assert before == after
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/history/test_reconstruct.py::test_future_bout_does_not_change_prior_record -v`  
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Tapology adapter is primary regional; Sherdog only for conflicts/misses; Combat Registry overrides on official disagreement. Each regional mapper populates first-class `quality_tier`, `timestamp_quality`, `timestamp_quality_source`, and `proxy_published_at` exactly like Task 4; `attributes` stay source-specific and reject reserved-key collisions. Frontier crawl is budgeted (`max_pages_per_run`, `max_depth`). Professional/amateur classification stored explicitly; unknown stays unknown (not zero). Coverage acceptance on sampled sets: ≥95% pro, ≥80% regulated-US amateur found or source failure declared; pre-fight agreement ≥98% where comparable.

- [ ] **Step 4: Run tests**

Run: `pytest tests/history tests/sources/tapology_public tests/sources/sherdog_public tests/sources/combat_registry -q`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mma_model/sources/tapology_public src/mma_model/sources/sherdog_public \
  src/mma_model/sources/combat_registry src/mma_model/history docs/data/regional-coverage.md \
  tests/history tests/sources/tapology_public tests/sources/sherdog_public \
  tests/sources/combat_registry tests/fixtures/sources
git commit -m "feat(history): Add regional public enrichment and PIT reconstruct"
```

---

### Task 10: Coverage tiers + strict health + leakage gates (DWCS-106)

**Files:**
- Create: `src/mma_model/quality/__init__.py`
- Create: `src/mma_model/quality/coverage.py`
- Create: `src/mma_model/quality/gates.py`
- Create: `src/mma_model/quality/leakage.py`
- Create: `output/contracts/coverage.schema.json`
- Create: `tests/quality/test_coverage.py`
- Create: `tests/quality/test_leakage.py`
- Create: `tests/quality/test_strict_gates.py`
- Modify: `src/mma_model/cli.py` (`mma-model coverage --series dwcs --strict --json`)

**Interfaces:**
- Produces:
  - `compute_coverage_report(*, series: str, db_url: str, policy: SourcePolicy) -> CoverageReport`
  - `CoverageReport.bouts: list[BoutCoverageRow]` length **440** for full DWCS
  - `BoutCoverageRow.tier: Literal["gold","silver","bronze","missing","conflict"]`
  - `evaluate_strict_gates(report: CoverageReport, policy: SourcePolicy) -> GateResult` exit code 0 pass / 2 fail
  - `assert_future_row_invariance(feature_fn, earlier_cutoff, later_mutation) -> None`

- [ ] **Step 1: Write the failing test**

```python
from mma_model.quality.gates import evaluate_strict_gates
from mma_model.sources.policy import load_source_policy

def test_strict_gate_fails_on_unresolved_identity(coverage_report_with_conflict):
    policy = load_source_policy()
    result = evaluate_strict_gates(coverage_report_with_conflict, policy)
    assert result.ok is False
    assert result.exit_code == 2
    assert "identity_conflict" in result.blocker_codes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/quality/test_strict_gates.py::test_strict_gate_fails_on_unresolved_identity -v`  
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Encode thresholds from policy `gates_retained`. Report must be deterministic for fixed DB + config hashes (`report_hash`). Human and JSON CLI output required. Blockers: missing uncategorized, reconciliation below 0.98, result agreement below 0.99, unresolved identity, future-row leakage, mutable-current leakage. Phase 3 consumers must refuse train/eval when strict health fails (document hook in `docs/data-contracts.md`).

- [ ] **Step 4: Run tests**

Run: `pytest tests/quality -q`  
Expected: PASS  
Run: `mma-model coverage --series dwcs --strict --json` on fixture DB  
Expected: exit 2 with structured blockers OR exit 0 when fixture is clean

- [ ] **Step 5: Commit**

```bash
git add src/mma_model/quality output/contracts/coverage.schema.json tests/quality src/mma_model/cli.py docs/data-contracts.md
git commit -m "feat(quality): Publish tiered coverage and strict health gates"
```

---

### Task 11: Odds lane seam notes (non-feature default)

**Files:**
- Create: `docs/data/odds-lane.md`
- Create: `tests/docs/test_public_first_docs.py` (link/path existence checks)

**Interfaces:**
- Produces documentation locking BestFightOdds archive + optional Odds API as separate lane; default training excludes odds features unless evaluation contract gains an explicit challenger flag in a later ticket.

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]

def test_design_and_plan_and_policy_exist():
    assert (ROOT / "docs/superpowers/specs/2026-08-12-public-first-mma-history-design.md").is_file()
    assert (ROOT / "docs/superpowers/plans/2026-08-12-public-first-mma-history.md").is_file()
    assert (ROOT / "config/sources/source_policy_v1.json").is_file()
```

- [ ] **Step 2: Run test to verify it fails** if docs missing; otherwise proceed to add `odds-lane.md` and assert no TBD/TODO in plan/spec.

- [ ] **Step 3: Write `docs/data/odds-lane.md`** describing archive-first reconciliation, optional Odds API from 2020, and separation from outcome features.

- [ ] **Step 4: Run** `pytest tests/docs/test_public_first_docs.py -q`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add docs/data/odds-lane.md tests/docs/test_public_first_docs.py
git commit -m "docs(odds): Lock odds as separate lane under public-first policy"
```

---

## Coverage thresholds (DWCS-106 machine gates)

| Gate | Threshold | Fail action |
|------|-----------|-------------|
| Manifest representation | 440/440 categorized | exit 2 |
| Cross-source reconciliation | ≥0.98 comparable | exit 2 |
| Result agreement | ≥0.99 comparable | exit 2 |
| Identity unresolved (evaluated/upcoming) | 0 | exit 2 |
| Future-row leakage failures | 0 | exit 2 |
| Mutable-current leakage | 0 | exit 2 |
| Regional pro sample (DWCS-105) | ≥0.95 found or source_failed | exit 2 |
| Regional regulated-US amateur sample | ≥0.80 found or source_failed | exit 2 |
| Pre-fight record agreement | ≥0.98 comparable | exit 2 |

## Source rate / backoff behavior

| Host class | min_delay_sec | max_retries | backoff | On persistent block |
|------------|---------------|-------------|---------|---------------------|
| ufcstats.com | 0.75 | 5 | exp base 1.0s cap 60s | kill `ufcstats_public` |
| tapology.com | 1.5 | 4 | exp base 2.0s cap 120s | kill `tapology_public` |
| sherdog.com | 1.5 | 4 | exp base 2.0s cap 120s | kill `sherdog_public` |
| combatreg.com | 1.0 | 4 | exp base 1.5s cap 90s | kill `combat_registry` |
| bestfightodds.com | 1.0 | 4 | exp base 1.5s cap 90s | kill `bestfightodds_archive` |

UA format: `mma-model/<version> (+https://github.com/0xAidan/mma-model; contact:<operator-email>)`.

## Raw-page hashing / storage

- SHA-256 of uncompressed bytes → `payload_hash`
- Store gzip at `data/raw/<source>/<hash[:2]>/<hash>.gz` via `ContentAddressedRawStore` (gitignored)
- `RawObservation.raw_ref == payload_hash` unless `raw_blob_absent=True`

## Parser schema-drift handling

- Required labels listed per parser module as frozensets
- Missing/renamed → `ParserSchemaDriftError`
- Adapter records `schema_drift` kill reason; does not mark partial rows `verified`

## Self-review checklist (plan author)

1. Spec §2 gates each appear in Task 10 thresholds.  
2. No TBD/TODO/placeholder steps.  
3. Interfaces named consistently (`UfcstatsPublicAdapter`, `SourceBlockedError`, `evaluate_strict_gates`).  
4. DWCS-102–106 each have failing-test → implement → pass → commit cycles.  
5. Odds default-off for outcome features documented in Task 11.

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-12-public-first-mma-history.md`. Implement via subagent-driven development (one PR per task/ticket group) starting at Task 1 after this policy prerequisite merges. Do not implement adapters in the prerequisite policy PR.
