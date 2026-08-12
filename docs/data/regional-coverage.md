# Regional / pre-UFC history coverage

Sanitized DWCS-105 evidence. No raw HTML or live payloads.

- Report hash: `05b4ca89632ffd0e1eff89012bf1ed809cbf563595ba1478e957ddb6bb0e6318`
- Professional sample: 9/9 (1.0000); source_failed=0; missing_unexplained=0
- Regulated-US amateur sample: 2/2 (1.0000); source_failed=0; missing_unexplained=0
- Unknown classification rows: 1
- Pre-fight agreement: 0/0 (n/a)
- Pre-fight exclusions: placeholder_replaced_in_tests
- Future-row invariance failures: 0
- Invariance hash: `3c8a328b20fd2c640d91a5f3203c8ae2eb3be2801e2944ddaa55170adac4c263`
- Conflicts: 1
- Left-truncated bouts: 11
- Unresolved identities: 0
- PIT tiers: {'gold': 1, 'silver': 16}
- Identity exact links / queued / blocks / conflations: 17/0/0/0

## Source failures

- none

## Sources

- `tapology_public` (public extraction): bouts=13 killed=False reasons=none
- `sherdog_public` (public extraction): bouts=2 killed=False reasons=none
- `combat_registry` (official record): bouts=2 killed=False reasons=none

## Live probes

- `tapology_public`: result=`BLOCKED` reason=`http_403` http=403 path=`/rankings/` robots=`rfc9309_parsed_allow`
- `sherdog_public`: result=`OK` reason=`None` http=200 path=`/events/` robots=`rfc9309_parsed_allow`
- `combat_registry`: result=`BLOCKED` reason=`login_wall` http=200 path=`/` robots=`rfc9309_parsed_allow`

Licensed SportsDataIO / BALLDONTLIE validation remains `source_failed` under recorded limitations and is not a DWCS-105 stop.
Pre-fight agreement 0/0 is the committed sample placeholder exclusion; reconstruction agreement and future-row invariance are covered by fixture tests.
