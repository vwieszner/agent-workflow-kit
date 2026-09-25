---
name: browser-interaction
description: MANDATORY for any agent-driven browser session (browser MCP or subagent). Safe text-field input, the sanctioned UI entry URL, and credential verification.
---
# Browser Interaction Protocol

## 1. Safe text input
Every time you type into a field:
1. **Inspect first** — read the field's current value.
2. **Explicit clear** — if not empty, clear it (select-all + delete).
3. **Empty-verify** — confirm it is empty immediately before typing.

## 2. Environment
- Use `config: ui.base_url` (the proxy / public entry point) for every UI-driven task.
- ❌ Navigating directly to raw backend or API pages, unless explicitly doing API-only testing.
  Authenticate through the app's own login UI.

## 3. Credentials
- Read the exact credentials from `config: ui.credentials_source` (or the relevant test fixture)
  before typing them. Never guess a username or password.
