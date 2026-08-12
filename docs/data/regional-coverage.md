# Regional / pre-UFC history coverage

Sanitized DWCS-105 evidence. No raw HTML or live payloads.

- Report hash: `82908154bb048296ad3cba8524d7695957fefa5be96ec472916dca1b750ca5f8`
- Evidence class: `fixture_validation`
- Probe evidence source: `frozen/sanitized`
- Live professional sample: 0/0 (n/a); source_failed=0; missing_unexplained=0
- Live regulated-US amateur sample: 0/0 (n/a); source_failed=0; missing_unexplained=0
- Unknown classification rows: 0
- Pre-fight agreement: 0/0 (blocker (insufficient_comparable_records); never a pass)
- Pre-fight unknown/missing excluded from agreement: 0
- Pre-fight exclusions: placeholder_replaced_in_tests
- Future-row invariance failures: 0
- Invariance hash: `3c8a328b20fd2c640d91a5f3203c8ae2eb3be2801e2944ddaa55170adac4c263`
- Conflicts: 1
- Left-truncated histories: 1
- Unresolved identities: 0
- PIT tiers: {'silver': 17}
- Identity exact source-ID links / queued fighters / blocks / conflations: 6/0/0/0

## fixture_validation

Synthetic `data-schema` decoder counts. These are **not live coverage** and must not satisfy live 95%/80% gates.

- Professional decoder: 9/9 (synthetic fixtures; not live coverage)
- Amateur decoder: 2/2 (synthetic fixtures; not live coverage)
- Year-filtered eligible sample rows: 6 (not live coverage)
- `9/9` and `2/2` must not be treated as measured live coverage.

## live_source_coverage

- `tapology_public`: status=`source_killed` result=`BLOCKED` reason=`http_403` http=403 path=`/rankings/`
- `sherdog_public`: status=`accessibility_only` result=`OK` reason=`None` http=200 path=`/events/`
- `combat_registry`: status=`source_killed` result=`BLOCKED` reason=`login_wall` http=200 path=`/`

Sherdog hash-only 200 on `/events/` is accessibility only and is not measured fighter-history coverage. Tapology HTTP 403 and Combat Registry login wall kill those source roles. Unknown remains unknown, not zero.

## Persisted source failures

HistorySourceFailure rows only. Probe killed/accessibility statuses are listed in the next section and are not the same as an empty failure table.

- none recorded in HistorySourceFailure

## Probe source statuses (frozen/sanitized)

- `tapology_public`: status=`source_killed` result=`BLOCKED` reason=`http_403` http=403 path=`/rankings/`
- `sherdog_public`: status=`accessibility_only` result=`OK` reason=`None` http=200 path=`/events/`
- `combat_registry`: status=`source_killed` result=`BLOCKED` reason=`login_wall` http=200 path=`/`

## Sources

- `tapology_public` (public extraction): bouts=13 killed=True live_status=source_killed reasons=none
- `sherdog_public` (public extraction): bouts=2 killed=False live_status=accessibility_only reasons=none
- `combat_registry` (official record): bouts=2 killed=True live_status=source_killed reasons=none

## Live probes

- `tapology_public`: result=`BLOCKED` reason=`http_403` http=403 path=`/rankings/` robots=`rfc9309_parsed_allow`
- `sherdog_public`: result=`OK` reason=`None` http=200 path=`/events/` robots=`rfc9309_parsed_allow`
- `combat_registry`: result=`BLOCKED` reason=`login_wall` http=200 path=`/` robots=`rfc9309_parsed_allow`

Licensed SportsDataIO / BALLDONTLIE validation remains `source_failed` under recorded limitations and is not a DWCS-105 stop.
Pre-fight agreement 0/0 is a blocker (`insufficient_comparable_records`), never a passing coverage gate.
