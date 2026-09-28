"""Tests for guards/graph_query.py, guards/mcp_readonly.py and guards/agent_source_read.py."""
from __future__ import annotations

import json
from pathlib import Path

from _hookenv import GUARDS, HookTestCase

GRAPH = GUARDS / "graph_query.py"
MCP_RO = GUARDS / "mcp_readonly.py"
SRC_READ = GUARDS / "agent_source_read.py"


class GraphQueryTests(HookTestCase):
    def setUp(self):
        super().setUp()
        self.cache = self.root / "graph-cache"
        self.cache.mkdir()
        (self.cache / "wt-7-1.db").write_text("", encoding="utf-8")
        (self.cache / "main-repo.db").write_text("", encoding="utf-8")
        self.write_config(
            f'[graph]\ntool = "codebase-memory-mcp"\nmcp_server = "codebase-memory-mcp"\n'
            f'main_project = "main-repo"\ncache_dir = {json.dumps(str(self.cache))}\n'
        )

    def call(self, tool, project, agent=None):
        p = {"tool_name": f"mcp__codebase-memory-mcp__{tool}", "tool_input": {"project": project}}
        if agent:
            p["agent_type"] = agent
        return self.json_denied(GRAPH, p)

    def write_config(self, extra):
        # [graph] replaces the base section: write the base without it
        from _hookenv import BASE_CONFIG
        base = BASE_CONFIG.replace('[graph]\ntool = "none"\nmcp_server = "codebase-memory-mcp"\nmain_project = ""\n', "")
        (self.root / ".workflow" / "config.toml").write_text(base + "\n" + extra, encoding="utf-8")

    def test_slot_agent_on_main_project_denied(self):
        self.assertTrue(self.call("search_graph", "main-repo", "story-impl"))

    def test_main_session_on_main_project_allowed(self):
        self.assertFalse(self.call("search_graph", "main-repo"))

    def test_slot_agent_on_own_project_allowed(self):
        self.assertFalse(self.call("search_graph", "wt-7-1", "story-finalize"))

    def test_unindexed_project_denied(self):
        self.assertTrue(self.call("trace_path", "guessed-slug"))

    def test_exempt_tool_allowed(self):
        self.assertFalse(self.call("index_repository", "guessed-slug"))

    def test_other_server_ignored(self):
        self.assertFalse(self.json_denied(GRAPH, {"tool_name": "mcp__other__x", "tool_input": {"project": "nope"}}))

    def test_g2_skipped_without_main_project_is_stated_once(self):
        self.write_config(
            f'[graph]\ntool = "codebase-memory-mcp"\nmcp_server = "codebase-memory-mcp"\n'
            f'main_project = ""\ncache_dir = {json.dumps(str(self.cache))}\n'
        )
        p = {"tool_name": "mcp__codebase-memory-mcp__search_graph", "session_id": "s1",
             "tool_input": {"project": "main-repo"}, "agent_type": "story-impl"}
        first, second = self.run_hook(GRAPH, p), self.run_hook(GRAPH, p)
        self.assertIn(b"SKIPPED", first.stderr)
        self.assertNotIn(b"SKIPPED", second.stderr)
        self.assertNotIn(b'"deny"', first.stdout)

    def test_graph_none_skips(self):
        from _hookenv import BASE_CONFIG
        (self.root / ".workflow" / "config.toml").write_text(BASE_CONFIG, encoding="utf-8")
        self.assertFalse(self.call("trace_path", "guessed-slug"))


READONLY_CFG = '''
[guards]
mcp_readonly_servers = [
  { server = "graphdb", dialect = "cypher" },
  { server = "pg", dialect = "sql", extra_deny = ["\\\\bnextval\\\\s*\\\\("] },
]
'''


class McpReadonlyTests(HookTestCase):
    extra_config = READONLY_CFG

    def q(self, server, tool, query):
        return self.json_denied(MCP_RO, {"tool_name": f"mcp__{server}__{tool}", "tool_input": {"query": query}})

    def test_cypher(self):
        self.assertTrue(self.q("graphdb", "write-cypher", "MATCH (n) RETURN n"))
        self.assertTrue(self.q("graphdb", "read-cypher", "MATCH (n) DETACH DELETE n"))
        self.assertTrue(self.q("graphdb", "read-cypher", "MATCH (n) SET n.x = 1"))
        self.assertTrue(self.q("graphdb", "read-cypher", "LOAD CSV FROM 'x' AS row RETURN row"))
        self.assertTrue(self.q("graphdb", "read-cypher", "CALL apoc.create.node(['A'], {})"))
        self.assertTrue(self.q("graphdb", "read-cypher", "MATCH (a)--(b) DELETE a"))
        self.assertFalse(self.q("graphdb", "read-cypher", "MATCH (n) WHERE n.name = 'DELETE ME' RETURN n"))
        self.assertFalse(self.q("graphdb", "read-cypher", "MATCH (n:Set) RETURN n.create"))
        self.assertFalse(self.q("graphdb", "get-schema", "MATCH (n) RETURN count(n) // delete later"))

    def test_sql(self):
        self.assertTrue(self.q("pg", "query", "DELETE FROM t"))
        self.assertTrue(self.q("pg", "query", "update t set x = 1"))
        self.assertTrue(self.q("pg", "query", "SELECT nextval('s')"))
        self.assertFalse(self.q("pg", "query", "SELECT * FROM t WHERE note = 'drop table'"))
        self.assertFalse(self.q("pg", "query", "SELECT replace(a, 'x', 'y') FROM t -- delete"))

    def test_unlisted_server_ignored(self):
        self.assertFalse(self.q("other", "write", "DELETE FROM t"))


class AgentSourceReadTests(HookTestCase):
    extra_config = ""

    def setUp(self):
        super().setUp()
        from _hookenv import BASE_CONFIG
        base = BASE_CONFIG.replace('tool = "none"', 'tool = "codebase-memory-mcp"')
        (self.root / ".workflow" / "config.toml").write_text(base, encoding="utf-8")

    def decide(self, agent, command, tool="Bash"):
        p = {"tool_name": tool, "tool_input": {"command": command}}
        if agent:
            p["agent_type"] = agent
        return self.json_denied(SRC_READ, p)

    FORBIDDEN = [
        ("story-impl", "sed -n '1,100p' app/tasks.py"),
        ("story-impl", "head -50 web/src/components/Main.jsx"),
        ("story-impl", "cd \"/w/wt\" && sed -n '10,40p' services/foo.py"),
        ("story-finalize", "cat app/services/graph_queries.py"),
        ("story-finalize", "tail -n 80 app/models.py"),
    ]
    ALLOWED = [
        ("story-impl", "uv run --no-project .workflow/scripts/stack_preflight.py --project-name s | tail -40"),
        ("story-impl", "cat > app/new_module.py <<'EOF'\nprint(1)\nEOF"),
        ("story-finalize", "docker compose -p s logs --tail=30 backend 2>&1 | tail -40"),
        ("story-impl", "cat docs/spec.md"),
        ("story-impl", "cat pyproject.toml"),
        ("story-impl", "git log --oneline -5 | head -3"),
        ("edge-case-hunter", "sed -n '1,100p' app/tasks.py"),
        (None, "sed -n '1,100p' app/tasks.py"),
    ]

    def test_forbidden(self):
        for agent, cmd in self.FORBIDDEN:
            with self.subTest(cmd=cmd):
                self.assertTrue(self.decide(agent, cmd))

    def test_allowed(self):
        for agent, cmd in self.ALLOWED:
            with self.subTest(cmd=cmd):
                self.assertFalse(self.decide(agent, cmd))

    def test_read_tool_never_gated(self):
        p = {"tool_name": "Read", "tool_input": {"file_path": "a.py"}, "agent_type": "story-impl"}
        self.assertFalse(self.json_denied(SRC_READ, p))

    def test_no_graph_skips(self):
        from _hookenv import BASE_CONFIG
        (self.root / ".workflow" / "config.toml").write_text(BASE_CONFIG, encoding="utf-8")
        self.assertFalse(self.decide("story-impl", "cat app/tasks.py"))
