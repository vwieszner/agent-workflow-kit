---
name: read-full-docs
description: MANDATORY. Read a documentation, spec, planning or reference file in full before drawing any conclusion about it. Targeted search locates; it never supports a verdict.
---
# Read Full Doc Files

## Trigger
Any task to read, check, review, audit, assess, summarize or report on a documentation file: docs,
story specs, epics, schema docs, design docs, READMEs, audits, plans. The rule applies the moment you
draw a conclusion about the file — a gap, a consistency finding, "it does / doesn't cover X", a check
or audit verdict, "I checked Y".

## Rule
- Read the **entire file** before that conclusion. Reads default to a line cap; page through with
  offset/limit until the whole file is read. Length is a reason to make several reads, not to read
  part of it.
- If you read only part of a file, say so, and draw no whole-file conclusion from it.

## Forbidden
- ❌ A heading outline plus sampled sections presented as a check.
- ❌ A "partial / targeted" read that yields findings.
- ❌ Sampling, then asserting completeness.
- ❌ Reporting a verdict and reading the rest only when challenged.

## Scope
- Targeted search or a section read stays correct for **locating** something or a narrow single-fact
  lookup.
- Source code is not read end to end: navigate it by symbol (code graph when configured, else grep).
