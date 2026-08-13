# Odds manual-price DB recovery (development)

Production path: upgrade a fresh database through Alembic to
`0013_odds_manual_prices`, which creates the final
`odds_manual_price_observations` schema.

Draft/local databases that were stamped against removed or partial manual-price
revisions cannot be healed by re-running `0013` (Alembic will not re-execute an
already-stamped revision). Recreate the database, or for disposable local SQLite
only:

```bash
# Destructive local recovery — loses data in the target DB.
alembic -c alembic.ini stamp --purge base
alembic -c alembic.ini upgrade head
```

Prefer deleting the local SQLite file and running `upgrade head` when the DB has
no data you need to keep.
