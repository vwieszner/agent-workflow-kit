---
name: preprod-schema
description: MANDATORY when config schema.preprod = true. Pre-production single-migration policy — no new migration files, edit the initial migration in place, no backward-compatibility tests or shims, and reset every database the edit makes stale.
---
# Pre-Production Schema Policy

Applies only when `config: schema.preprod` is `true` (no production database exists; test databases
are built from migrations).

## Rule
- **Never create new migration files.** When a model/schema changes, edit the relevant initial
  migration directly. Do not run the migration generator to produce a new file.
- **Never write backward-compatibility tests or shims:** no migration-preservation tests, no "existing
  rows survive" tests, no migration-executor fixtures, no "pre-existing data unaffected" assertions, no
  dual-read fallbacks. If a spec or AC asks for one, challenge it and remove the requirement before
  implementing.

## Reset every database the edit makes stale
Migration frameworks do not re-apply an already-applied migration, so every existing database keeps
the OLD schema after the initial migration is edited.

- **Reused test databases** (`--keepdb`-style, which the full gate uses) stay stale indefinitely and
  the gate fails en masse with "relation/column/constraint does not exist" in unrelated tests.
  Drop them **after the migration-editing branch merges** (`config: schema.test_db_reset_cmd`); they
  rebuild on the next run. Dropping them before the merge accomplishes nothing — they rebuild from the
  pre-merge schema.
- **Live databases** on every stack the edit reaches fail at write time. Pick the reset path:
  - **Additive change** (new nullable column or column with a default): apply the equivalent `ALTER`
    directly on each affected stack's live DB. The post-merge smoke applies it on the shared stack.
  - **Structural change** (rename, type change, FK change, dropped column, table rename): full stack
    reset (`config: schema.stack_reset_cmd`, i.e. down with volumes + up with rebuild) on each affected
    stack. Same on the shared stack immediately post-merge.
- After any migration edit, and before any test run against a live DB, invoke the `dev-preflight`
  skill to catch live-DB vs on-disk drift.
