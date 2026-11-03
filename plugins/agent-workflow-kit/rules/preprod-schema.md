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
  - **Additive change** (new nullable column or column with a default, or an added index or
    constraint): apply the equivalent `ALTER` (for an index or constraint, the statement the
    framework's SQL-preview command prints for it) directly on each affected stack's live DB. For an
    added unique index or constraint, first check that no existing row violates it; if one does, use
    the structural path. The post-merge smoke applies it on the shared stack.
  - **Structural change** (rename, type change, FK change, dropped column, table rename): full stack
    reset (`config: schema.stack_reset_cmd`, i.e. down with volumes + up with rebuild) on each affected
    stack — stop that stack's compose watch first (`${CLAUDE_PLUGIN_ROOT}/scripts/kill_owned.py --watch`, or the
    slot cleanup's watcher stop), and go through `${CLAUDE_PLUGIN_ROOT}/scripts/bring_up_story_stack.py`, never a
    bare compose `down -v`. Same on the shared stack immediately post-merge, after asking the owner.
  - **Rebase:** when a story rebase finds a migration path in the old-base..target diff, wiping that
    story's OWN stack as above is pre-authorised. The shared stack and other stories' stacks never are.
- After any migration edit, and before any test run against a live DB, invoke the `dev-preflight`
  skill (it runs `${CLAUDE_PLUGIN_ROOT}/scripts/stack_preflight.py`, the gate) to catch live-DB vs on-disk drift;
  dispatch the `dev-preflight` subagent only to diagnose a red preflight.
