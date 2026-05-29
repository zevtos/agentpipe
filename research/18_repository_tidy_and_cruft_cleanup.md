# Repository Tidy & Cruft Cleanup — Reference

Grounding for the `/tidy` command. Synthesized from a verified deep-research sweep
(25 claims, all 3-0 adversarially confirmed against primary sources: git-scm.com,
GitHub Docs, github/gitignore, git-filter-repo, BFG, and a peer-reviewed Empirical
Software Engineering study). Reference material — not auto-imported by agents.

## Cruft taxonomy — what accumulates in repos

1. **Accidentally-committed generated/build artifacts** — output that should have been ignored.
2. **Stale documentation** — broken links and references to renamed/deleted code, files, flags.
   Empirically pervasive: across 3,000+ GitHub projects ~82% had ≥1 outdated code-element
   reference at some point, ~29% of the most-popular projects currently do (Tan/Wagner/Treude,
   EMSE 2024, DOI 10.1007/s10664-023-10397-6). Broken/404 links "give the impression that the
   project's repository isn't maintained" (Azure SDK engineering blog).
3. **Editor/OS files** — `.DS_Store`, `Thumbs.db`, IDE folders. Belong in the github/gitignore
   `Global/` tier, ideally a global gitignore rather than per-repo.
4. **Large binaries** committed to history.
5. **Secrets** — credentials/tokens/keys. Special-cased (see below).
6. **Dead code / orphaned config** — least tool-supported; treat conservatively (flag, don't auto-remove).

## The reversibility ladder — git as the safety net

Every cleanup operation sits on a reversibility ladder. Safe automated tidy acts only at the
recoverable end and escalates the rest.

| Operation | Recoverable? | How / why |
|---|---|---|
| Edit a tracked file | yes | it's in the commit — `git revert` / `reset` |
| `git rm --cached <f>` | yes | removes from the index only, **working tree untouched** — the safe primitive for de-tracking. `.gitignore` governs only *untracked* files, so a committed artifact MUST be `git rm --cached`-ed (in either order with the rule) before the rule has any effect. |
| `git rm <f>` | yes | removes from index **and** disk, but it's in history. Has an up-to-date safety check — refuses if there are staged/uncommitted changes unless `-f`. |
| `git stash push -u -m "<label>"` | yes | "stash-as-trash": moves untracked (`-u`) / + ignored (`--all`) files off the tree but keeps them recoverable. Pro Git recommends `git stash --all` as the safer alternative to `git clean`. |
| `git clean -f` | **NO** | irreversibly deletes untracked files — never in history, so reflog/restore cannot recover them. If ever previewed, dry-run (`-n`) flags **must match** the real flags (`-d`, `-x`) or the preview understates the deletion. **A tidy tool should never run this — stash instead.** |
| history rewrite (`git-filter-repo`, BFG) | rewrites history | large blobs / secrets only; force-push required; out of scope for routine tidy. |

## Tooling

- **`.gitignore` templates** — github/gitignore is the canonical source (powers GitHub's template
  chooser). Its `Global/` directory holds editor/OS/tool rules meant for a global gitignore across
  all repos.
- **History surgery** — `git-filter-repo` is now recommended by the Git project itself, which
  deprecated its own bundled `git filter-branch` ("safety and performance issues cannot be
  backward compatibly fixed"). BFG Repo-Cleaner is the faster purpose-built alternative
  (mirror-clone first as a backup; protects HEAD by default). GitHub's current sensitive-data
  guidance centers `git-filter-repo >= 2.47` (`--sensitive-data-removal`).
- **Doc revalidation** — remark-validate-links and Azure SDK's Verify-Links.ps1 both treat the
  live filesystem / git repo as ground truth, checking that links and references resolve to files
  and headings that actually exist. This is the model `/tidy` follows: derive reality, then check
  the docs against it.

## Secrets are not a cleanup task

Removing a secret from history is **not** sufficient. Rotate/revoke it FIRST — it persists in
clones, forks, cached SHA-1 views on the host, and referencing PRs even after a rewrite and
force-push (GitHub Docs). `/tidy` detects committed secrets, STOPS, and hands off; it never
rewrites history automatically.

## Design implications for `/tidy`

- **Prerequisite: clean tracked tree** so the whole run is one reviewable, revertible commit.
  (No source prescribes single-atomic-commit bundling — it's a deliberate engineering choice that
  makes tidy trivially undoable. This was the one assumption the research could not source-confirm.)
- **Untracked junk → labelled `git stash`, never `git clean`.** Report the stash ref; stashes are
  not auto-GC'd, so the user can restore or drop on their own schedule.
- **Doc fixes correct only provably-false claims.** Link/reference checkers carry false positives
  (intentional external links, templated/example references) → those need human review, not auto-fix.
- **History-level cruft (big blobs, secrets) is flagged, never auto-rewritten.**

## Sources

- git-scm: gitignore, git-rm, git-stash, Pro Git "Stashing and Cleaning"
- GitHub Docs: ignoring files; removing sensitive data from a repository
- github/gitignore (canonical templates, `Global/` tier)
- git-filter-repo (newren) · BFG Repo-Cleaner (rtyley) · `git filter-branch` deprecation notice
- remark-validate-links · Azure SDK broken-link detection (Verify-Links.ps1)
- Tan, Wagner, Treude, "Outdated documentation…", EMSE 2024, DOI 10.1007/s10664-023-10397-6
