---
name: propagate-story-changes
description: 'Pre-merge propagation pass for an in-flight story, run by hand outside the parallel-dev-wave flow. Dispatches the story-propagate subagent to find every dependent doc (epics, other stories, audits, sprint status, deferred work, the active plan, AGENTS.md) and draft an edit per impact, walks the checklist with the owner, and has the subagent apply the approved rows after the merge. Use when the user says "propagate the story changes", "run pre-merge propagation", or "check downstream impacts before merge".'
---

Follow the instructions in ./workflow.md.
