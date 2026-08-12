# Regional / pre-UFC history coverage

Sanitized DWCS-105 evidence. No raw HTML or live payloads.

- Report hash: `8d1db62a16c96bc8099eed1dc253761ed1a45d20e16cf72c2963dbd35039a15d`
- Evidence class: `fixture_validation`
- Year-filtered professional sample: 6/6 (1.0000); source_failed=0; missing_unexplained=0
- Year-filtered regulated-US amateur sample: 0/0 (n/a); source_failed=0; missing_unexplained=0
- Unknown classification rows: 0
- Pre-fight agreement: 0/0 (blocker (insufficient_comparable_records); never a pass)
- Pre-fight exclusions: placeholder_replaced_in_tests
- Future-row invariance failures: 0
- Invariance hash: `3c8a328b20fd2c640d91a5f3203c8ae2eb3be2801e2944ddaa55170adac4c263`
- Conflicts: 1
- Left-truncated histories: 1
- Unresolved identities: 0
- PIT tiers: {'silver': 17}
- Identity exact links / queued / blocks / conflations: 17/0/0/0

## fixture_validation

Synthetic `data-schema` decoder counts. These are **not live coverage** and must not satisfy live 95%/80% gates.

- Professional decoder: 9/9 (synthetic fixtures; not live coverage)
- Amateur decoder: 2/2 (synthetic fixtures; not live coverage)
- `9/9` and `2/2` must not be treated as measured live coverage.

## live_source_coverage

- `tapology_public`: status=`source_killed` result=`BLOCKED` reason=`http_403` http=403 path=`/rankings/`
- `sherdog_public`: status=`accessibility_only` result=`OK` reason=`None` http=200 path=`/events/`
- `combat_registry`: status=`source_killed` result=`BLOCKED` reason=`login_wall` http=200 path=`/`

Sherdog hash-only 200 on `/events/` is accessibility only and is not measured fighter-history coverage. Tapology HTTP 403 and Combat Registry login wall kill those source roles. Unknown remains unknown, not zero.

## Source failures

- none

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
