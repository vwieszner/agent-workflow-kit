---
name: layered-review
description: 'Adversarial code review with typed review layers (blind-hunter, edge-case-hunter, verification-gap, acceptance-auditor), execution stages (ui-verifier, pbt-writer, test-adequacy), code dedupe, adversarial findings verification, majority votes in auto mode and repro tests, rendered into the story''s findings file. Pipeline mode is invoked by parallel-dev-wave and auto-dev-loop with explicit arguments and returns the triage table; direct mode runs when the owner says "run code review" or "review this code". Never applies patches; never triages in pipeline mode.'
---

Follow the instructions in ./workflow.md.
