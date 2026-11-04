---
name: parallel-dev-wave
description: Orchestrate one ready-for-dev story through the full implementation lifecycle in an isolated slot — Phase 1 setup script (slot + worktree + optional isolated service stack), Phase 2 story-impl subagent (tests-first implementation + recorded scoped tests + impl commit), layered-review (review + verification workflow), Phase 3 story-finalize subagent (triaged patches + close-out commit), propagation, then the land window (merge train, land lease, final rebase, pre-merge gate) and land-story. Three human checkpoints: spec review, code-review triage, merge approval. Use whenever a ready-for-dev story needs implementing; multiple stories can run concurrently in separate slots.
---

Follow the instructions in [workflow.md](workflow.md).
