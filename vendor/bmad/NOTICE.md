# NOTICE — vendored BMad Method components

## Source

- Project: BMad Method (`bmad-method`), https://github.com/bmad-code-org/BMAD-METHOD
- Version vendored: 6.2.2 (installed `_bmad/` tree; modules `core` and `bmm`)
- Copyright (c) 2025 BMad Code, LLC
- License: MIT — full text in `LICENSE` (copied verbatim from the upstream repository;
  a copy also sits in each vendored skill directory so it travels with the installed skill).
  The upstream `LICENSE` refers to `CONTRIBUTORS.md` and `TRADEMARK.md`; those files are not
  vendored and are available in the upstream repository.

## Trademark

"BMad", "BMad Method" and "BMad Core" are trademarks of BMad Code, LLC and are not licensed
under the MIT License. They are used here only to identify the origin of the vendored files.
This kit is not an official or endorsed BMad product; the vendored skills are named
`create-story` and `advanced-elicitation`, not with a BMad name.

## Files and modifications

| Upstream file (6.2.2 install path) | Vendored as | Modification |
|---|---|---|
| `bmm/workflows/4-implementation/create-story/workflow.yaml` | `skills/create-story/workflow.yaml` | `{config_source}:<key>` resolution replaced by `config: <section>.<key>` keys read from `.workflow/config.toml` (`project.owner`, `project.communication_language`, `project.document_output_language`, `paths.planning_dir`, `paths.specs_dir`, `paths.epics_dir`, `paths.sprint_status`, `paths.project_context`); `{project-root}/_bmad/...` paths replaced by skill-relative `{skill-root}` paths; epics sharded pattern reads `paths.epics_dir`; caller output-path override documented. |
| `bmm/workflows/4-implementation/create-story/instructions.xml` | `skills/create-story/instructions.xml` | Engine/workflow.yaml paths made skill-relative; references to workflows not vendored (`sprint-planning`, `correct-course`, PM agent, `dev-story`, `code-review`, test-architect module) replaced by generic instructions or the kit's pipeline skill; literal `sprint-status.yaml` replaced by `{{sprint_status}}`; one profanity replaced; step 6 validation calls the `validate_against_checklist` protocol (upstream referenced `core/tasks/validate-workflow.xml`, which is absent from the 6.2.2 install); step 6 sprint-status update may be skipped when the caller owns it and uses the project's sprint-status tool when one is named; completion report lists unapplied checklist suggestions and saved questions. |
| `bmm/workflows/4-implementation/create-story/template.md` | `skills/create-story/template.md` | Header note no longer names `validate-create-story` / `dev-story`. |
| `bmm/workflows/4-implementation/create-story/checklist.md` | `skills/create-story/checklist.md` | Validation-framework references point to the `validate_against_checklist` protocol and `{skill-root}` paths; non-interactive/#yolo rule added to Step 6 (apply critical issues, report the rest); next step names the kit pipeline instead of `dev-story`. |
| `core/tasks/workflow.xml` | `skills/create-story/workflow-engine.xml` | Config resolution rewritten for `config:` keys via `.workflow/scripts/wfconfig.py`; caller-supplied values phase added; rule 4 (non-interactive caller → #yolo, questions saved and returned); `[p] Party-Mode` option removed (party-mode not vendored); `[a]` invokes the `advanced-elicitation` skill; `validate_against_checklist` protocol added; self-references made skill-relative. |
| `core/workflows/advanced-elicitation/workflow.xml` | `skills/advanced-elicitation/workflow.xml` | `methods` path made skill-relative; `agent-party` manifest and party-mode references removed; `communication_language` bound to `config: project.communication_language`; return target generalized from `create-doc.md` to the invoking workflow/skill; `<non-interactive-mode>` section added (one method per invocation, critique returned, target not edited). |
| `core/workflows/advanced-elicitation/methods.csv` | `skills/advanced-elicitation/methods.csv` | None (verbatim). |

`SKILL.md` files in both skill directories are new (kit-authored wrappers).
