"""
synthesize_v2.py — template-driven report writer for non-academic profiles.

For the academic profile, ultrasearch.py keeps invoking the v1 synthesize.py
to preserve byte-identical output. For dev/docs/etc., orchestrate.py calls
this module instead.

Input: ProfileConfig + list[Candidate] (scored, sorted, deduplicated).
Output: rendered markdown to either stdout or `--out` path.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from _common import iso_now, log
from templates import render


def _entry_from_candidate(c: dict[str, Any], idx: int) -> dict[str, Any]:
    """Flatten a Candidate dict to the variables our templates expect."""
    extras = c.get("extras") or {}
    return {
        "title": c.get("title") or c.get("external_id") or "(no title)",
        "url": c.get("url") or "",
        "external_id": c.get("external_id") or "",
        "doi": c.get("doi"),
        "summary": c.get("abstract") or c.get("title") or "",
        "year": c.get("year"),
        "authors": c.get("authors") or [],
        "venue": extras.get("venue"),
        "stars": extras.get("stars"),
        "last_commit": (
            extras.get("pushed_at") or extras.get("last_commit") or
            (f"{extras.get('last_commit_days')}d ago" if extras.get("last_commit_days") is not None else None)
        ),
        "language": extras.get("language"),
        "license": extras.get("license"),
        "score": c.get("quality_score"),
        "extras": extras,
        "_idx": idx,
    }


def synthesize(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    template: str,
    out_path: Path | None = None,
    extra_context: dict[str, Any] | None = None,
) -> str:
    """Render `template` from skill_dir()/templates/ with the given candidates.

    Returns the rendered markdown string. If `out_path` is given, also
    writes it to disk.
    """
    entries = [_entry_from_candidate(c, i) for i, c in enumerate(candidates)]
    # extra_context first, then built-ins override — caller cannot accidentally
    # clobber `query`/`entries`/`generated_at`/`n_entries`.
    context: dict[str, Any] = dict(extra_context or {})
    context.update({
        "query": query,
        "entries": entries,
        "generated_at": iso_now(),
        "n_entries": len(entries),
    })
    md = render(template, context)

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md, encoding="utf-8")
        log(f"wrote {len(md)} chars to {out_path}")
    return md


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="ultrasearch v2 template-driven synth (debug helper)")
    p.add_argument("--query", required=True)
    p.add_argument("--template", default="library_matrix.md.j2")
    p.add_argument("--in", dest="input_json", required=True,
                   help="path to JSON file with a list[Candidate dict]")
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)

    data = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("input JSON must be a list of candidates")
    out_path = Path(args.out) if args.out else None
    md = synthesize(args.query, data, template=args.template, out_path=out_path)
    if out_path is None:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
