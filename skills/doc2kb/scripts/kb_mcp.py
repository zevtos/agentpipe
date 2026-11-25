#!/usr/bin/env python3
"""
kb_mcp.py — MCP server (stdio) that puts doc2kb knowledge bases one tool call
away from an agent.

A KB already answers `./query.sh "<question>"`, but an agent has to know the KB
is there, `cd` into it and quote the query through a shell. Registered as an MCP
server, the same engine (`query_kb.py`) becomes a native tool:

    search(queries=[...], kb=?)   ranked passages in full, with file:line refs,
                                  from every KB under the session's folder —
                                  or from the one KB / folder named in `kb`
    list(path=?)                  which KBs are there and what is in them

and the server's `instructions` tell the agent, at session start, which KBs sit
under its working directory — so it searches them without being told to.

Register it once (Claude Code, user scope):

    dkb mcp install
    # = claude mcp add --scope user kb -- python3 <skill>/scripts/kb_mcp.py

Environment:
    DOC2KB_ROOT   folder to look for KBs in (default: the directory the client
                  started the server in — the session's working directory)
    DOC2KB_KBS    extra KBs that are searched from every session
                  (os.pathsep-separated paths, e.g. a personal notes KB)

Pure stdlib, no venv: the protocol is newline-delimited JSON-RPC 2.0 over
stdin/stdout. Both MCP styles are answered — the `initialize` handshake
(2024-11-05 … 2025-11-25) and the stateless one (2026-07-28: `server/discover`,
protocol version in each request's `_meta`).

Nothing but protocol messages may reach stdout; the engine's notes (index
refreshed, …) go to stderr.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import query_kb as qk  # noqa: E402

SERVER_NAME = "doc2kb"
STATEFUL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
STATELESS_VERSIONS = ["2026-07-28"]
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_SERVER = "io.modelcontextprotocol/serverInfo"
MAX_QUERIES = 8
DISCOVERY_TTL = 60.0

SEARCH_DESCRIPTION = (
    "Search doc2kb knowledge bases (folders of documents converted to Markdown: lecture "
    "slides, books, manuals, lab assignments, notes, transcripts) and get the best passages "
    "IN FULL, each headed by `docs/file.md:first-last` — pass that file and line range to "
    "Read when you need the text around it. Use this before grep or reading whole documents: "
    "it ranks by relevance, matches Russian and English word forms, and finds exact tokens "
    "(10.0.2.15, --mac-address, setBlocked) as typed.\n"
    "Send several short keyword queries in ONE call — different wordings, the terms the "
    "document itself would use (API names, option names), and each language the corpus "
    "has: matching is by words, not meaning, so a Russian query cannot reach an English "
    "book. Passages are never repeated across the queries of a call.\n"
    "By default every KB under the session's working directory is searched; `kb` narrows "
    "it. For EVERY occurrence of an exact string, grep the KB's docs/ instead."
)

TOOLS = [
    {
        "name": "search",
        "title": "Search knowledge bases",
        "description": SEARCH_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array", "items": {"type": "string"}, "minItems": 1,
                    "maxItems": MAX_QUERIES,
                    "description": "1-8 keyword queries, e.g. [\"NAT guest address "
                                   "10.0.2.15 gateway\", \"адрес гостя в режиме NAT\"]. "
                                   "Put \"double quotes\" inside a query for an exact phrase.",
                },
                "kb": {
                    "type": "string",
                    "description": "Limit the search: an absolute path to a KB or to a folder "
                                   "holding KBs, or a part of a KB's path (\"kb-course\"). "
                                   "Default: all KBs under the working directory.",
                },
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20,
                          "description": "Passages per query (default 6)."},
                "detail": {
                    "type": "string", "enum": ["auto", "full", "brief"],
                    "description": "auto (default): the strongest passages in full, about "
                                   "1500 tokens per query, the rest as one-line pointers. "
                                   "full: every passage in full. brief: pointers and "
                                   "snippets only — for scanning where a topic is covered.",
                },
                "doc": {"type": "string",
                        "description": "Only this document id (e.g. doc-007), as shown in "
                                       "a hit's path."},
                "all_terms": {"type": "boolean",
                              "description": "Require every term of a query to be present "
                                             "(default: any term, ranked)."},
            },
            "required": ["queries"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "list",
        "title": "List knowledge bases",
        "description": (
            "List the doc2kb knowledge bases under the session's working directory (or under "
            "`path`): where each one is, its size, languages and source documents. Use it to "
            "see what is searchable or to pick a `kb` for `search`."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Folder to look in (default: the working directory)."},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


# ---------- knowledge-base scope ----------

def _root() -> Path:
    return Path(os.environ.get("DOC2KB_ROOT") or os.getcwd()).expanduser().resolve()


def _extra_kbs() -> list[Path]:
    out = []
    for raw in (os.environ.get("DOC2KB_KBS") or "").split(os.pathsep):
        p = Path(raw).expanduser() if raw.strip() else None
        if p is not None and qk.is_kb(p):
            out.append(p.resolve())
    return out


_cache: dict[str, tuple[float, list[Path]]] = {}


def _kbs_under(root: Path) -> list[Path]:
    hit = _cache.get(str(root))
    if hit and time.monotonic() - hit[0] < DISCOVERY_TTL:
        return hit[1]
    kbs = qk.discover_kbs(root)
    _cache[str(root)] = (time.monotonic(), kbs)
    return kbs


def _scope(select: Optional[str]) -> tuple[Path, list[Path]]:
    """(root, KBs to search). No selector: every KB under the root plus the
    always-on extras. A selector: that KB / folder / path fragment."""
    root = _root()
    if not select:
        kbs = list(_kbs_under(root))
        kbs += [k for k in _extra_kbs() if k not in kbs]
        return root, kbs
    p = Path(select).expanduser()
    if p.is_absolute() and p.is_dir():
        return p.resolve(), qk.discover_kbs(p.resolve())
    pool = list(_kbs_under(root))
    pool += [k for k in _extra_kbs() if k not in pool]
    cand = root / p
    if qk.is_kb(cand):
        return root, [cand.resolve()]
    needle = select.strip("/").casefold()
    return root, [k for k in pool if needle in str(k).casefold()]


def instructions() -> str:
    root = _root()
    try:
        kbs = _kbs_under(root) + [k for k in _extra_kbs()]
    except Exception:
        kbs = []
    if not kbs:
        return ("doc2kb: no knowledge base under this session's directory. `search` and "
                "`list` take a path (`kb` / `path`) to one elsewhere.")
    lines = [f"doc2kb knowledge bases (document corpora with a search index) under {root}:"]
    for d in kbs[:16]:
        try:
            rel = str(d.relative_to(root)) if d != root else "."
        except ValueError:
            rel = str(d)
        s = qk.kb_summary(d)
        if s.get("indexed"):
            lines.append(f"- {rel} — {s['documents']} docs, ~{max(1, s['tokens'] // 1000)}k tokens")
        else:
            lines.append(f"- {rel}")
    if len(kbs) > 16:
        lines.append(f"- … and {len(kbs) - 16} more (`list` shows all)")
    lines.append(
        "For a fact, a definition or a how-to from these documents call the `search` tool "
        "first, before grep or reading files: one call takes several keyword queries and "
        "returns the best passages in full with `file:first-last` refs for Read.")
    return "\n".join(lines)


# ---------- tools ----------

def _as_list(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    return []


def call_search(args: dict[str, Any]) -> tuple[str, bool]:
    queries = _as_list(args.get("queries") or args.get("query"))[:MAX_QUERIES]
    if not queries:
        return "search needs `queries`: a list of 1-8 keyword queries.", True
    root, kbs = _scope(args.get("kb"))
    if not kbs:
        where = f"matching {args['kb']!r}" if args.get("kb") else f"under {root}"
        return (f"no doc2kb knowledge base {where}. `list` shows what is there; `kb` "
                f"takes an absolute path to a KB or to a folder of KBs."), True
    detail = args.get("detail") if args.get("detail") in ("auto", "full", "brief") else "auto"
    try:
        top_k = max(1, min(20, int(args.get("top_k") or 6)))
    except (TypeError, ValueError):
        top_k = 6
    text, _ = qk.run_search(
        kbs, queries, top_k=top_k, mode=detail, doc=args.get("doc") or None,
        require_all=bool(args.get("all_terms")), show_kb=True,
        keep_weak=bool(args.get("top_k")) or detail != "auto")
    return text, False


def call_list(args: dict[str, Any]) -> tuple[str, bool]:
    if args.get("path"):
        root = Path(str(args["path"])).expanduser().resolve()
        if not root.is_dir():
            return f"not a folder: {root}", True
        kbs = qk.discover_kbs(root)
    else:
        root, kbs = _scope(None)
    return qk.render_list(root, kbs), False


# ---------- protocol ----------

_out = sys.stdout


def _send(msg: dict[str, Any]) -> None:
    _out.write(json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n")
    _out.flush()


def _server_info() -> dict[str, str]:
    return {"name": SERVER_NAME, "version": f"engine-{qk.ENGINE_REV}"}


def handle(msg: dict[str, Any]) -> None:
    mid = msg.get("id")
    method = msg.get("method")
    if method is None or mid is None:           # a response or a notification: nothing to say
        return
    params = msg.get("params") or {}
    stateless = isinstance(params.get("_meta"), dict) and META_VERSION in params["_meta"]

    def ok(result: dict[str, Any]) -> None:
        if stateless:
            result.setdefault("resultType", "complete")
            result.setdefault("_meta", {})[META_SERVER] = _server_info()
        _send({"jsonrpc": "2.0", "id": mid, "result": result})

    def err(code: int, message: str, data: Any = None) -> None:
        e: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            e["data"] = data
        _send({"jsonrpc": "2.0", "id": mid, "error": e})

    if stateless and params["_meta"][META_VERSION] not in STATELESS_VERSIONS + STATEFUL_VERSIONS:
        err(-32022, "Unsupported protocol version",
            {"supported": STATELESS_VERSIONS + STATEFUL_VERSIONS,
             "requested": params["_meta"][META_VERSION]})
        return

    if method == "initialize":
        want = params.get("protocolVersion")
        ok({"protocolVersion": want if want in STATEFUL_VERSIONS else STATEFUL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": _server_info(),
            "instructions": instructions()})
    elif method == "server/discover":
        _send({"jsonrpc": "2.0", "id": mid, "result": {
            "resultType": "complete",
            "supportedVersions": STATELESS_VERSIONS + STATEFUL_VERSIONS,
            "capabilities": {"tools": {}},
            "_meta": {META_SERVER: _server_info()},
            "instructions": instructions()}})
    elif method == "ping":
        ok({})
    elif method == "tools/list":
        ok({"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = {"search": call_search, "list": call_list}.get(name)
        if fn is None:
            err(-32602, f"Unknown tool: {name}")
            return
        try:
            text, is_error = fn(args if isinstance(args, dict) else {})
        except Exception as e:                   # the model can read this and retry
            text, is_error = f"{name} failed: {type(e).__name__}: {e}", True
        ok({"content": [{"type": "text", "text": text}], "isError": is_error})
    else:
        err(-32601, f"Method not found: {method}")


def main() -> int:
    global _out
    # Protocol messages own stdout; anything else that prints goes to stderr.
    _out = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n")
    sys.stdout = sys.stderr
    stdin = os.fdopen(os.dup(sys.stdin.fileno()), "r", encoding="utf-8", newline="\n")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "Parse error"}})
            continue
        for m in (msg if isinstance(msg, list) else [msg]):
            if isinstance(m, dict):
                try:
                    handle(m)
                except Exception as e:
                    if m.get("id") is not None:
                        _send({"jsonrpc": "2.0", "id": m["id"],
                               "error": {"code": -32603, "message": f"Internal error: {e}"}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
