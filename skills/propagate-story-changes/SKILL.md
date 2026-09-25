---
name: propagate-story-changes
description: 'Pre-merge propagation pass for an in-flight story. Compares the story spec to its base, identifies dependent docs (epics, other stories, audits, sprint status, deferred work, the active plan), and walks through each impact with the owner. Forcing function for the source-of-truth rule "in-flight spec change capture + end-of-impl propagation." Use when the user says "propagate the story changes", "run pre-merge propagation", or "check downstream impacts before merge".'
---

Follow the instructions in ./workflow.md.
