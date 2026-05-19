# Changelog

All notable changes to this project will be documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`doc2kb` skill: автоматическое восстановление math и встроенных картинок без потерь.** Две независимые регрессии, замеченные на стандартной ИТМО-лабораторной (`МАИ.docx` + презентация-сопровождение `МАИ.pdf` для метода анализа иерархий), которые тихо съедали критичное содержимое:
  - **DOCX с OOXML-математикой (`<m:oMath>`).** `mammoth` дропает math элементы и emit'ит warning, но в knowledge base уходит body с подписями `"формулируется как , (1)"` — формулы просто отсутствуют, а текст вокруг на них ссылается ("формула 1", "формулы (3) или (4)"). На лабах с >2 формулами это делает kb бесполезным. Фикс в `extract_docx.py`: если scout пометил `has_equations: true` и `pandoc` есть на `PATH`, extractor автоматически переключается с mammoth на `pandoc -f docx -t markdown --wrap=none` (60s timeout, `--force-mammoth` escape-hatch для регрессии). Pandoc сохраняет OOXML math как `$...$` / `$$...$$` LaTeX. JSON output теперь содержит `extractor: "pandoc"` или `"mammoth+markdownify"`. Если pandoc недоступен — loud warning с инструкцией установить (`brew install pandoc` / `apt install pandoc`). Регрессия на `МАИ.docx`: 6 формул (`\max_{x \in X}`, `\sum w_i \cdot \mathrm{norm}(f_i)`, нормализация `\frac{x - x_{\min}}{x_{\max} - x_{\min}}` и др.) теперь survive, body растёт с 1947 → 2133 токенов.
  - **PDF с positioned-pictures (формулы и матрицы, нарисованные как embedded images).** `pymupdf4llm` уже emit'ил `dropped_pictures` warning со счётчиком, но remediation требовал ручной транскрипции через `Read` tool — на 6-страничной презентации с 8 матрицами это 30+ минут работы оператора. Фикс в `extract_pdf_pymupdf4llm.py`: новая функция `_replace_dropped_pictures_with_assets()` извлекает все embedded images через pymupdf (`page.get_images(full=True)` + `doc.extract_image()`), сохраняет под `<doc_id>-pageNN-imgM.<ext>` в `<kb_dir>/assets/` и заменяет каждый `==> picture [WxH] intentionally omitted <==` placeholder на `![page N, image M](../assets/<file>)`. Default `<kb_dir>` вычисляется как `<output_md>.parent.parent / "assets"` (соответствует стандартному `kb_dir/docs/*.md` ↔ `kb_dir/assets/` layout); override через `--assets-dir`/`--assets-rel`, отключить через `--no-extract-images`. `assets:` ключ в frontmatter перечисляет relative paths. `dropped_pictures` warning теперь supressed для замещённых placeholder'ов, и оставляется (с уточнением `N/M auto-recovered`) только для тех, что не удалось заменить. Регрессия на `МАИ.pdf`: 7 placeholder'ов заменены, 8 jpeg сохранены в `assets/` (один дополнительный — picture, которая в text-layer не имела omitted-placeholder), warnings=[].
  - Также обновил `SKILL.md` (Phase 1 — опциональная pandoc dependency; Phase 4 — описание авто-маршрутов; Phase 5 — рекомендация переместить sources в `<kb_dir>/raw/` для self-contained kb с проверкой `source_sha256`), `references/extraction-recipes.md` (новые CLI флаги), `references/format-spec.md` (новое опциональное поле `assets:` + полный список `extraction_method` значений включая `pandoc@<ver>` и `claude-pagewise-manual@1`). Старое поведение `--no-extract-images` / `--force-mammoth` гарантирует, что existing call-sites не сломаются — фиксы только дополняют дефолты.
- **`doc2kb` skill: Jupyter notebook (`.ipynb`) support.** Adds `extract_ipynb.py` to the lightweight tier — pure stdlib `json` parser, no new venv dependencies (would have cost `nbformat` + `jsonschema` + `traitlets` + `jupyter-core` for what is a fully JSON-defined format). Scout classifies `.ipynb` via extension + optional `application/x-ipynb+json` mime, populates `cells`/`code_cells`/`markdown_cells`/`raw_cells`/`has_outputs`, and emits `extraction_strategy: "ipynb"`. Extract walks every cell verbatim: markdown cells passthrough, code cells fenced (language sniffed from `kernelspec`/`language_info` against an allowlist, default `python`), text outputs (`stream`, `execute_result.text/plain`, `display_data.text/plain`, `error` with ANSI escapes stripped) preserved under `### Output`. Base64 `image/*` outputs collapsed into one `*(image output omitted: N)*` placeholder per cell with a warning — same never-embed-base64 rule already enforced on DOCX inline images. `huge_file` user-decision gate applies to notebooks too (>50 MB). Manifest, INDEX.md, llms.txt all gain a `Jupyter notebooks` group; per-doc bullets surface `<n> cells` and `with outputs`. Scout caps cell-scan at 4000 cells with a warning for ultra-large notebooks (true count still reported).
- **New skill: `doc2kb` — heterogeneous document corpus → LLM-optimized knowledge base.** Two-session workflow: extraction session converts a folder of mixed documents into per-source Markdown with YAML frontmatter + `manifest.json` + `INDEX.md` + `llms.txt` + `AGENTS.md`; a second Claude/Codex session ingests `<kb_dir>/` by reading `AGENTS.md` → `INDEX.md` → targeted `docs/*.md`. MVP supports PDF (text-layer), DOCX, PPTX (with speaker notes preserved — most converters drop them), MD, TXT, HTML. Local-first, no paid APIs: bootstraps its own `.venv` with `pymupdf4llm`, `pdfplumber`, `pikepdf`, `python-magic`, `mammoth`, `python-pptx`, `trafilatura`, `tiktoken`, `charset-normalizer` (~13 MB lightweight tier; deps pinned with upper bounds so qpdf/lxml CVE-prone majors are not silently pulled). 5-phase pipeline: `bootstrap` → `scout` (`_scout.json` with PDF text/image_only/encrypted classification, DOCX/PPTX richness, mime sniff via libmagic with extension fallback) → `decide` (batched single-message user prompt for encrypted/scanned/huge/unsupported files) → `extract` (per-format script, returns JSON to stdout) → `assemble` (manifest builder). Smoke-tested on a 22-file russian KIS corpus (5 PDF, 5 DOCX, 10 PPTX, .archimate + huge Radchenko skipped per scout): 20 docs extracted in 46 s, output 1.5 MB Markdown for 322k tokens (~70× compression vs. 100 MB source). Critical correctness invariants: never summarize (verbatim preservation, only structural cleanup via `normalize_md.py`), never embed images as base64 (placeholder `<img alt="image N">` instead — saved ~5× tokens on diagram-heavy DOCX), `[page N]` anchors for PDFs, speaker notes preserved per slide via `### Notes` sub-heading. Security hardening: scout refuses symlinks whose targets resolve outside the corpus root (skipped with reason `symlink escapes corpus root — refused (security)`), heading text is sanitized of control characters in every extract script, hand-rolled YAML emitter forces block style on lists containing control chars and escapes newlines in inline scalars (prevents frontmatter injection via crafted document headings), `--source-rel` is validated against absolute paths and `..` components, hash-like and other identifier-shaped strings are protected from accidental numeric coercion on round-trip, files >50 MB of any supported type — not just PDFs >500 pages — now gate on a user-confirmation `huge_file` decision. Includes a `Trust boundary` section in `SKILL.md` + `AGENTS.md` template explicitly flagging the second-session prompt-injection surface (verbatim corpus body is data, not instructions).

### Fixed
- **`doc2kb` skill: вторая форма поломки на math-heavy PDF — dropped pictures.** Первая итерация фикса (`mangled_visual_layout`) ловила случай, когда `pymupdf4llm` рассыпает формулы по markdown-таблицам в цепочки одиночных символов. Но на 5-страничной методичке `Преобразование математических моделей динамической системы.pdf` всплыл второй вариант: pymupdf4llm не смог распарсить позиционно-набранные уравнения и просто выкинул их как `==> picture [WxH] intentionally omitted <==` плейсхолдеры (19 штук на 5 страниц = 3.8/page). Текст вокруг ссылается на "уравнение (1)", "формулу", "матрицу A" — а самих формул в kb нет; вторая сессия получает фактически обезматематиченный документ и галлюцинирует ответы. Добавил `_detect_dropped_pictures()`: считает `==> picture [WxH] intentionally omitted <==` regex matches, emit warning `dropped_pictures: ...` если ≥ 2/page или ≥ 5 абсолютно (тюнинг по тому же smoke-corpus). Heuristic параллельный mangled_visual_layout — может сработать оба сразу. SKILL.md, `pitfalls.md` item #13 и `agents_template.md` обновлены, чтобы перечислять обе сигнатуры как разные shapes одной и той же проблемы и направлять агента на один и тот же remediation path (read PDF через `Read` tool → переписать body вручную).
- **`doc2kb` skill: PDFs с визуальным layout-ом математики ломали extraction молча.** На лабораторных методичках и старых научных PDF (формулы набраны позиционно: дробная черта — отдельный прямоугольник, штрихи — отдельные glyph'ы рядом с буквой) `pymupdf4llm` рассыпает ODE/выражения по markdown-таблице в цепочки одиночных символов с `<br>` между ними: `)<br>(<br>2<br>)<br>...<br>_t_<br>_y_`. До фикса такой результат тихо ехал в knowledge base как успешный — во второй сессии агент получал семантический мусор вместо формул. Теперь `extract_pdf_pymupdf4llm.py` после извлечения прогоняет heuristic по markdown-таблицам: если ≥ 25% data-cells содержат ≥ 6 `<br>` разделителей с фрагментами ≤ 3 символа (тюнинг по smoke-corpus `Варианты задания_оценка параметров.pdf`, который ловится на 0.45) — emit warning `mangled_visual_layout: detected fragmented text in N/M table cells ...`. SKILL.md, `references/pitfalls.md` (новый item 13) и `assets/agents_template.md` теперь содержат инструкцию: прочитать исходный PDF напрямую через `Read` tool (Claude рендерит страницы и видит математику визуально), переписать body соответствующего `kb/docs/<id>-*.md` вручную, обновить `extraction_method: claude-pagewise-manual@1`, пересобрать manifest. Healthy PDF с обычными формулами (не позиционно-набранными) heuristic не false-positive'ит — проверено на 5-страничной `Преобразование математических моделей динамической системы.pdf` (0 warnings). Заодно расширил heading detection с `# ...` (h1-only) на `# ` + `## ` — `pymupdf4llm` чаще эмитит главные заголовки как `##`, и манифест ранее видел `headings: []` на документах с реальной структурой.
- **`doc2kb` skill: Cyrillic/accented filenames на macOS получали ложный `extraction missing` в `manifest.json`.** macOS HFS+/APFS экспонирует filename в Unicode NFD (decomposed: `й` = `и` + ` ̆`), а `_yaml_quote` записывает frontmatter `source:` в NFC — на глаз идентично, побайтово различается. `merge_scout` сравнивал строки через равенство и для каждой записи с не-ASCII именем добавлял в `skipped[]` запись `extraction missing (no kb file, no scout skip)`, хотя `docs/<id>-*.md` лежал рядом. Фикс трёхуровневый: `_common.nfc()` (новая функция), `validate_source_rel()` нормализует в NFC, `scout_corpus.scan_file()` пишет `source_path` сразу NFC (плюс в `walk_corpus` escape-branch и в `main()` scout-failed branch), и в `build_manifest.merge_scout()` оба сайта (индекс scout-файлов и `doc_sources` set) сравниваются через `_nfc()` как defensive belt — чтобы старые `_scout.json` от прошлых запусков всё ещё давали чистый manifest. Новый item 14 в pitfalls описывает invariant.
- **`gost-report` skill: растягивающиеся скобки в формулах с `\begin{cases}` и `\left( ... \right)`.** `r.formula(...)` для кусочно-заданных функций раньше рисовал крошечную `{` рядом с двухстрочной матрицей вместо высокой скобки на всю высоту cases-блока. Причина: `latex2mathml` выдаёт `<mrow><mo stretchy="true" fence="true" form="prefix">{</mo><mtable>…</mtable></mrow>`, а наш OMML-walker отдавал `<mo>` как обычный run без обёртки в делимитер — Word рендерил скобку нерастяжимо. Теперь walker детектирует пару `stretchy=true / fence=true` `<mo>` и оборачивает содержимое в `<m:d>` с правильными `begChr`/`endChr`; для одностороннего открытого fence (cases) `endChr` пустой — Word его скрывает. Парный `\left( ... \right)` тоже выигрывает: вокруг крупного содержимого (дробей, сумм) скобки теперь стянутся под высоту тела.

## [0.13.0] - 2026-05-11

### Added
- **`--skills-only` / `-SkillsOnly` flag.** Restricts the installer to copying `skills/*` only — agents, commands, and every `settings.json`/hook layer (attribution-fix, config-defaults, CLAUDE.md baseline, sound hooks, thinking summaries, gost-validation) are skipped. Composes with both `--target claude` (skills under `~/.claude/skills/`) and `--target codex` (skills under `~/.agents/skills/`), and with the existing actions: `--dry`, `--diff`, `--pull`, and `--uninstall` all scope to skills under the flag. Useful for fast skill iteration (e.g. updating `gost-report` after editing `scripts/validate.py`) without re-running the full claude-target settings merge. `--model-profile` is silently ignored under `--skills-only` — no agents are touched, so the profile is not persisted to `settings.json`.

## [0.12.0] - 2026-05-03

### Changed
- **`--with-sound-hooks` / `-WithSoundHooks` now installs only the `Stop` sound hook**, not `Stop` + `Notification` together. Previously the flag merged both events, which produced two beeps in sequence at the end of every chat (`Stop` fires when Claude finishes a turn, then `Notification` fires for "waiting for input"). Now installs only `Stop` — single, unambiguous "Claude finished" cue. **BC NOTE:** users on ≤0.11 with the old behavior already merged into `~/.claude/settings.json` will keep hearing both beeps until they run `--clean-sound-hooks` (see Added) — re-running `--with-sound-hooks` does **not** strip the previously-merged `Notification` entry.

### Added
- **`--with-notification-sound` / `-WithNotificationSound` opt-in flag.** Installs only the `Notification` sound hook (`afplay Glass.aiff` on macOS, `paplay` on Linux, `[console]::beep(660,250)` on Windows native, `powershell.exe ... beep` on WSL). Used alone, it fires when Claude is waiting for input or requests a permission. Used together with `--with-sound-hooks`, both install — the installer prints a warning that two beeps may fire in sequence on Stop. Independent flag, set-union with existing entries.
- **`--clean-sound-hooks` / `-CleanSoundHooks` action.** New install-script action (mutually exclusive with `--install` / `--update` / `--uninstall`) that strips every sound-hook entry from `hooks.Stop` and `hooks.Notification` in `~/.claude/settings.json`. Recognises `afplay`, `paplay`, `[console]::beep`, `powershell.exe ... beep` via case-insensitive regex. **Non-sound hooks are preserved** — `gost-report` validation, user-custom hooks, anything else stays put. Drops empty wrappers, drops empty event lists, drops the top-level `hooks` object if it ends up empty. Atomic rewrite via `tempfile` + `os.replace`. Bash side delegates to `scripts/clean-sound-hooks.py`; PowerShell side reimplements natively (no Python dependency on Windows).

## [0.11.0] - 2026-05-03

### Added
- **`gost-report` skill: invisible automatic validation.** New `skills/gost-report/scripts/validate.py` module deterministically checks the generated `.docx` against ГОСТ 7.32 rules — long/short dash leakage in body text outside auto-captions, missing or non-monotonic figure/table captions, bare LaTeX (`\frac`, `\sum`, `\sqrt`, etc.) in prose, placeholder name leaks (`Фамилия И.О.`), AI-tone phrase bank from `SKILL.md`'s writing-style block. Two delivery layers:
  - **L1 (always-on, every target).** `Report.save()` writes a `<docx>.gost-meta.json` sentinel next to the file, then runs the validator and raises `GostValidationError` on any tier-(a) violation. The model sees a Python traceback in the shell exactly as it would for any failing tool call — and the exception text is the only validation-related prose in the entire skill, visible only on failure. Tier-(b) heuristic warnings print to stderr without raising.
  - **L2 (default-on for `--target claude`, opt-out via `--no-gost-validation` / `-NoGostValidation`).** A `Stop` hook merged into `~/.claude/settings.json` runs `python validate.py --hook` on every model Stop. The script scans cwd (depth ≤ 6) for sentinel files, validates each described `.docx`, and emits `{"decision":"block","reason":"..."}` JSON on tier-(a) violations — Claude Code feeds the reason back to the model so it self-corrects on the next turn. Hook always exits 0, even on its own internal error; cannot break the Stop pipeline. Sentinel-only scoping means zero false fires in projects that don't use `gost-report`.
- **Skill body shrinks ~7.5 KB.** `SKILL.md`'s `## Checklist` section (11 lines) and `references/checklist.md` (76 lines) deleted — the validator is now the source of truth for everything that can be checked deterministically. The model's context never carries a manual checklist that could be forgotten in long generations; it thinks only about how to write the report. `grep -i 'валидаци|checklist|validate' SKILL.md` → 0 matches after this change.

### Changed
- **`Report.save()` now raises `GostValidationError(RuntimeError)` on validation failures.** Subclasses `RuntimeError` (not `Exception` directly) so existing user code with narrow `except IOError`/`except OSError` is unaffected. **BC NOTE:** code with broad `except Exception:` around `r.save()` will mask validation failures — review and let `GostValidationError` propagate, or catch specifically and re-raise after handling. The exception message explicitly tells the model not to suppress the error and not to switch to `python-docx` directly — those are the two failure modes the design defends against.
- **`validate.py` self-bootstraps via the skill venv when invoked as a CLI.** When the Stop hook fires `python validate.py --hook`, the system python likely lacks `python-docx`. The script detects this, locates `<skill_dir>/.venv/bin/python` (created by `r.save()`'s first invocation via `ensure_env.py`), and re-execs under that interpreter. Net effect: the hook command in `settings.json` stays a one-liner, and validation kicks in once the user has run the skill at least once.

### Notes
- **Codex compatibility.** Codex CLI has no hooks, so L2 is unavailable on `--target codex`. L1 (in-library raise) works identically — the model sees Python tracebacks regardless of target. The validator script ships in both targets' skill copies and remains usable in `--check <docx>` mode for manual debugging.
- **`--uninstall` does not auto-strip the Stop hook entry from `settings.json`** (same precedent as `--no-attribution-fix` / `--no-config-defaults` / `--with-sound-hooks` since v0.7.x). Re-running `bash install.sh --no-gost-validation` does not remove the previously-merged hook either; users who want to revert must edit `~/.claude/settings.json` manually.

## [0.10.0] - 2026-05-03

### Added
- **`--model-profile <preset>` / `-ModelProfile <preset>` flag** for per-agent model selection. Three presets: `mixed` (default, byte-identical to `agents/*.md` source — opus for `architect`+`security`, sonnet for the rest), `opus` (every agent rewritten to opus), `sonnet` (every agent downgraded to sonnet). Source files are **never modified** — the installer rewrites the `model:` line at copy time via sed (Bash) / regex (PowerShell), so re-running with the same profile produces byte-identical output. Codex target is unaffected (agents are not installed for `--target codex` regardless).
- **Profile is persisted** to `~/.claude/settings.json` under the key `agentpipeModelProfile`. Subsequent installs (including `update.sh`) reuse the persisted choice unless `--model-profile` is passed again. Persistence happens only when the user explicitly passes the flag — implicit `mixed` defaults don't pollute settings.json.
- **Profile-aware `--dry` and `--diff`** — both pipe source through the rewrite helper into a temp file before comparison, so a profile switch shows real drift, not phantom drift on every model line.
- **`--pull` always strips back to canonical mixed defaults** before writing into `agents/` — the repo source-of-truth is never contaminated by an installed `opus`-or-`sonnet` profile copy. A one-line info notice is printed when a non-mixed installed profile is stripped.

### Notes
- `--model-profile` is the granular escape hatch. The blunt-force alternative is the `CLAUDE_CODE_SUBAGENT_MODEL` env var, which overrides every subagent's model from outside — including Claude Code's built-in `Plan` and `Explore`. The installer's flag is documented as the primary mechanism; the env var is mentioned in `docs/installation.md` as a global override for users who want it.
- `--uninstall` does **not** strip `agentpipeModelProfile` from settings.json (same precedent as `includeCoAuthoredBy` / `permissions.deny` in v0.7.x — we don't track keys we added). Re-installing after uninstall will pick up the persisted profile from settings.json.
- A future `haiku` preset was considered and rejected for now — none of the agent prompts target haiku capabilities, and nobody asked for it. Will revisit if requested.

## [0.9.0] - 2026-05-03

### Added
- **Three new default keys in the config-defaults layer** (default-on for `--target claude`, opt-out via `--no-config-defaults`):
  - `autoUpdatesChannel: "stable"` — official default is `"latest"` (beta channel; that's the channel the Feb–Mar 2026 adaptive-thinking regression shipped on). Stable lags ~1 week and skips versions with major regressions.
  - `cleanupPeriodDays: 180` — official default is `30`, so old session files are purged at startup. 180 keeps history for users returning to projects after a month or two.
  - `spinnerTipsEnabled: false` — disables the in-spinner tips (noisy after the first session).
- **Destructive Bash patterns added to `permissions.deny`** (set-union with user entries via `--list-union`): `Bash(rm -rf /*)`, `Bash(rm -rf ~/*)`, `Bash(rm -rf $HOME/*)`, `Bash(mkfs *)`, `Bash(dd * of=/dev/*)`. These complement the existing secret-file deny list and are universally-unsafe regardless of stack. We chose `permissions.deny` (native Claude Code mechanism) over a custom `PreToolUse` hook with regex — simpler, no `jq` dependency, no false-positive risk.
- **Neutral CLAUDE.md baseline (install-if-missing).** New file `scripts/CLAUDE.md.example` ships stack-agnostic rules covering communication, honesty, scope, and workflow — no language-specific style opinions. The installer copies it to `~/.claude/CLAUDE.md` **only if no file exists there**, and **never overwrites** on subsequent installs once it exists. Opt-out: `--no-claude-md` / `-NoClaudeMd`.
- **`--with-sound-hooks` / `-WithSoundHooks` opt-in flag.** When passed, merges `Stop` + `Notification` hooks into `~/.claude/settings.json` with OS-auto-detected commands: `afplay` on macOS (Hero/Glass sounds), `paplay` on Linux (freedesktop complete.oga), `powershell.exe -c [console]::beep` on WSL, `[console]::beep` on Windows native. Set-union with user entries (your existing hooks are preserved). Off by default — personal preference.
- **`--with-thinking-summaries` / `-WithThinkingSummaries` opt-in flag.** When passed, sets `showThinkingSummaries: true`. Useful for debugging agent quality (otherwise thinking renders as a collapsed stub). Off by default — some users find the thinking output noisy.
- **`update.sh` / `update.ps1` canonical update entry point.** Thin wrappers that forward to `install.sh --update` / `install.ps1 -Update` (both still work for backward compat). Forward extra args, so `bash update.sh --target codex --no-claude-md` is valid. Promoted to canonical because "update agentpipe" is a discrete operation worth its own entry point.

### Notes
- Sandbox layer (the `sandbox.enabled` / `autoAllowBashIfSandboxed` config from the settings docs) was deliberately deferred to a future release. Its sub-keys (`filesystem.denyRead`, `network.allowedDomains`, `excludedCommands`) are user-specific (some users have `~/.aws`, some don't; allow-domains depend on stack) — shipping a single default would either be useless (empty lists) or invasive (our guesses clobbering user settings). Will likely arrive as `--with-sandbox` opt-in flag with minimal-neutral base config.
- Sound-hook / thinking-summaries opt-ins do **not** have a corresponding `--without-` to remove on uninstall. `--uninstall` doesn't auto-strip these settings.json keys (same precedent as `--no-attribution-fix`/`--no-config-defaults` in v0.7.x — we don't track which keys we added). Re-running with the flag again is idempotent (set-union for hooks, scalar overwrite for thinking-summaries).

## [0.8.0] - 2026-05-03

### Added
- **`gost-report`: project-paths convention.** New `_paths.py` module ships `ProjectPaths` (frozen dataclass: `root`, `docs`, `figures`, `tables`, `out`, `tex`) and `paths(start=None) → ProjectPaths` helper. Project root auto-detected by walking up from the caller's `__file__` (or CWD fallback) and matching the first marker among `.git/` → `Makefile` → `pyproject.toml` → `.claude/`. "Contains marker, not is marker" rule means a script in `<project>/.claude/gost-report/build.py` walks past `.claude/` and lands at `<project>/`. Both names re-exported from `gost_report` for `from gost_report import paths`.
- **`Report` auto-resolves figure and save paths.** `Report.figure(path, caption)` resolves relative paths against `<project>/docs/figures/`; absolute paths pass through. `Report.save(path=None)` defaults to `<project>/docs/report.docx`, treats relative paths as relative to `<project>/docs/`, and creates parent directories with `mkdir parents=True exist_ok=True`. `save()` now returns `Path` (was `str`). New keyword-only constructor parameter `Report(..., project_root=None)` lets callers override auto-detection. Result: agent-generated scripts drop the 5-line `Path(__file__).parent / "figures"` boilerplate and the build script can live anywhere — recommended location `<project>/.claude/gost-report/build.py` so it doesn't pollute the artefact directory.
- **`references/templates/build.py`** — copy-paste scaffold for a fresh project (see SKILL.md "Project layout" section for the full convention).

### Changed
- **BC NOTE**: `r.figure("foo.png", ...)` with a relative path previously resolved against `os.getcwd()`. It now resolves against `<project>/docs/figures/`. Real consumer scripts always built absolute paths via `Path(__file__).parent / "figures"`, so the in-the-wild break is near zero. If you hit this, either pass an absolute path (unchanged behavior) or accept the new convention. `FileNotFoundError` raised on miss now includes both input and resolved paths.
- `Report.save()` return type widened from `str` to `Path`. `print(f"Wrote {out}")` style still works.
- SKILL.md gains a "Project layout" section showing the recommended `<project>/.claude/gost-report/build.py` location and the auto-resolve rules. API table updated to reflect the new `figure`/`save` semantics and adds the `paths()` row.


## [0.7.1] - 2026-05-02

### Fixed
- **`includeCoAuthoredBy` is deprecated by Claude Code.** v0.7.0 wrote only the legacy key; the modern key is `attribution: { commit: "", pr: "" }`, which takes precedence. The installer now writes **both** — `attribution` for current Claude Code, `includeCoAuthoredBy` for backward compat with older versions. Verified against [official settings docs](https://code.claude.com/docs/en/settings).
- **Hook regex was too narrow.** v0.7.0 stripped only `Co-Authored-By: Claude <noreply@anthropic.com>`. Modern Claude Code emits model-named variants like `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>` (and presumably future model names). Regex broadened to `Claude[^<]*<noreply@anthropic\.com>` so any model-name variant is caught.

### Added
- **Safe-defaults layer (default-on for `--target claude`, opt-out via `--no-config-defaults` / `-NoConfigDefaults`).** Two universal defaults merged into `~/.claude/settings.json`:
  - `$schema: https://json.schemastore.org/claude-code-settings.json` — enables JSON autocomplete and inline validation in VS Code, Cursor, and other editors.
  - `permissions.deny` — set-union (preserves user entries) of universally-unsafe file-read patterns: `Read(./.env)`, `Read(./.env.*)`, `Read(./**/secrets/**)`, `Read(./**/*.pem)`, `Read(./**/*.key)`. We deliberately do not ship a `permissions.allow` list — it's stack-specific; use Claude Code's `fewer-permission-prompts` skill instead.
- **`scripts/json-merge.py`: `--list-union <dotted.path>`** repeatable flag. For the named JSON path, lists merge via set-union (overlay-first order, dedupe) instead of overwrite. Used to merge `permissions.deny` without clobbering user entries. Backward-compatible with v0.7.0 invocations (no flag → existing scalar/array overwrite behavior).
- **`scripts/agentpipe.env.example`** — curated reference for opt-in shell env vars (`CLAUDE_CODE_EFFORT_LEVEL`, `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`) with caveats: explicitly **not** auto-installed and **not** auto-sourced. Annotated trade-offs: `EFFORT_LEVEL=max` removes per-turn token cap (paid-plan quota burns faster), `DISABLE_ADAPTIVE_THINKING` is a no-op on Opus 4.7, `DISABLE_NONESSENTIAL_TRAFFIC` bundles autoupdater + telemetry + error-reporting + feedback-command.
- **`docs/installation.md`: "After Install: Terminal & Keybindings" section** — pointer to `/terminal-setup`, newline fallbacks (`Ctrl+J`), and a keybindings table covering `Esc Esc` rewind, image paste, permission-mode cycling, transcript toggle. Plus an "Optional Shell Environment Variables" section explaining how to use `agentpipe.env.example`.

### Notes
- `--uninstall` does **not** auto-remove the new `$schema` / `permissions.deny` keys (same precedent as `includeCoAuthoredBy` in v0.7.0): we don't track which keys we added, and removing user state on uninstall is not symmetric with adding by deep-merge. Edit `~/.claude/settings.json` manually to revert.
- The `agentpipe.env.example` file is shipped **only in the repo** — the installer does not copy it anywhere. Users who want it explicitly copy it to `~/.claude/agentpipe.env` and add a single source-line to their shell rc. This is intentional: shell rc is the user's territory.


## [0.7.0] - 2026-05-02

### Added
- **Installer suppresses Claude Code commit-message attribution by default (`--target claude`).** Two layers, both idempotent and reversible:
  1. **Settings flag.** `~/.claude/settings.json` gets `includeCoAuthoredBy: false` deep-merged in (existing keys preserved). Atomic write via temp file + rename through `scripts/json-merge.py` (Python 3 stdlib only); on parse error of the existing file, the original is left untouched. PowerShell uses native `ConvertFrom-Json` / `ConvertTo-Json` and skips the Python dependency.
  2. **Global `commit-msg` hook.** `scripts/git-hooks/commit-msg` is copied into `~/.git-templates/hooks/`, and `git config --global init.templateDir` is pointed at `~/.git-templates` (only if currently unset or already ours — never overrides a user-set path). The hook strips `🤖 Generated with [Claude Code]` and `Co-Authored-By: Claude <noreply@anthropic.com>` lines plus trailing blank lines via portable BSD/GNU sed.
- **`--no-attribution-fix` (Bash) / `-NoAttributionFix` (PowerShell) opt-out flag.** Skips both layers. Always implicitly off for `--target codex`.
- `--uninstall` reverses the layer: removes our hook (only if byte-identical to ours), unsets `init.templateDir` (only if it points to our path). `settings.json/includeCoAuthoredBy` is left as-is — uninstall scope stays narrow; the installer prints a one-line reminder so users can revert manually.
- New files: `scripts/git-hooks/commit-msg`, `scripts/json-merge.py`. Existing user hooks are backed up to `commit-msg.agentpipe.bak.<epoch>` before overwrite, with a loud warning.

### Changed
- `install.sh` and `install.ps1` gain three internal helpers (`do_attribution_fix` / `do_attribution_unfix` / `do_attribution_dry`) wired into install, uninstall, dry, and diff flows. Codex target unchanged.
- `docs/installation.md` adds a "Removing Claude Attribution from Commits" section with troubleshooting (existing template dir conflict, existing hook backup) and a `git filter-repo --message-callback` recipe for cleaning already-committed trailers (with explicit force-push warning — destructive, not automated).
- README "Customization" mentions the new default and how to opt out.

### Notes
- Existing repos are not retroactively modified — `git init` only seeds hooks at repo creation. To apply the hook to an existing repo, run `git init` inside it (no-op except for hooks; safe) or copy the hook into `.git/hooks/` manually. The installer surfaces this as a one-line note on every install.
- History rewrites for already-pushed commits with the trailers are documented but explicitly not automated by the installer (force-push hazard — must be coordinated with collaborators).

## [0.6.4] - 2026-05-02

### Fixed
- **`gost-report`: consumer scripts no longer need `sys.path.insert(...)` boilerplate before `from gost_report import ...`.** Previously every agent-generated script (e.g. lab `build_report.py`) had to start with `sys.path.insert(0, "/Users/.../skills/gost-report/scripts")` to make the import resolve, baking a hardcoded absolute path into every output and burning tokens on identical preamble each regeneration. `scripts/ensure_env.py` now writes a `gost_report.pth` file into the venv's `site-packages` during bootstrap (purelib path queried from the venv's own `sysconfig`, not hardcoded — works on macOS, Linux, Windows, conda). The `.pth` is rewritten if missing on every `main()` call (self-heal for venvs predating this feature or for users who manually deleted it). As belt-and-suspenders, `ensure_env.py` also prepends `<skill_dir>/scripts/` to `PYTHONPATH` before `os.execv` / Windows `subprocess.run` of the user script. Result: agents/users write `from gost_report import Report, TitleConfig` directly. SKILL.md Dependencies section documents the mechanism in one paragraph.

## [0.6.3] - 2026-05-02

### Fixed
- **`gost-report`: city and year no longer overflow to page 2 of the title page.** The previous implementation in `_build_title_page` placed `Санкт-Петербург` and `2026` as the last two paragraphs of a stream-of-paragraphs builder, padded above by 14 fixed blank-paragraph spacers (6 + 4 + 4). Page-1 budget left only ~99pt of slack, so any single extra wrap of `university_full`, `topic`, the teacher line, or `student_name` pushed the year onto a near-empty page 2 (and the body section break shifted body content to page 3). New approach: emit city + year as a borderless single-column floating table anchored to the bottom margin via OOXML `w:tblpPr` (`vertAnchor=margin`, `tblpYSpec=bottom`, `horzAnchor=margin`, `tblpXSpec=center`). The footer is now pinned to the bottom of page 1 regardless of how the rest of the title page lays out — robust against long topics, multi-line ministry/university strings, long teacher titles, and custom `UniversityProfile` margins.

## [0.6.2] - 2026-04-27

### Fixed
- **`gost-report`: nested N-ary operators (∑∑, ∫∑, ∑∏, …) no longer leave a placeholder square between the inner operator and its body.** v0.5.0 collected the body of an N-ary operator at the `mrow` level via lookahead, but only one level deep — the inner operator's `<m:e>` stayed empty because its body siblings were consumed at the outer level instead. Refactored the lookahead into a recursive walker (`_walk_with_nary`) that, when it encounters a nested N-ary inside an outer N-ary's body, recurses to collect the inner one's body first. Result: `J = \sum_{c=1}^{k} \sum_{x \in C_c} \|x - \mu_c\|^2` now produces a properly nested `<m:nary><m:e><m:nary><m:e>‖x−μ_c‖²` structure instead of two siblings with the norm-squared dangling outside. Verified end-to-end on `\sum\sum`, `\sum\prod`, and the original 8-formula regression suite — 0 empty `<m:e>` left.

## [0.6.1] - 2026-04-27

### Changed
- **`gost-report` SKILL.md surfaces `TitleConfig` configurable fields directly.** A real-world session showed the agent assuming `teacher_label="Проверил"` was a hard-coded title-page string, when it's actually a TitleConfig field that takes any value (`"Проверила"` for a female teacher, `"Руководитель"` for theses/term papers). Two small additions to plug this blind spot:
  1. The minimal-workflow example now passes `teacher_label="Проверил"` explicitly with an inline comment showing the alternatives, so the agent sees the option as part of the canonical template.
  2. A compact `TitleConfig — часто переопределяемые поля` table after the methods table covers the five fields agents most often miss (`teacher_label`, `work_number`, `variant`, `teacher_degree`, `teacher_position`), with a pointer to `references/api.md` for the full list. ~10 extra lines in SKILL.md to prevent a recurring class of mistake.

## [0.6.0] - 2026-04-27

### Added
- **Local eval framework for agent prompts: `scripts/eval.sh` + `tests/` + `docs/eval.md`.** Runs agent under test in `claude -p` headless mode (using the user's Claude Code OAuth — no API key required), then runs an LLM-as-judge step against a written rubric. Costs ~2 subscription messages per scenario. Designed for hand-iteration on agent prompts; **not wired to CI** by design, to avoid burning quota on every PR.
- Layout: `tests/<agent>/<scenario>/input.md` (what's sent to the agent) and `rubric.md` (what the judge checks against). After a run, the same folder gets `last_output.md` and `last_verdict.json` (gitignored — local debug artifacts).
- Runner CLI: `bash scripts/eval.sh --list` discovers scenarios and prints a cost estimate without making any calls; `bash scripts/eval.sh <agent>` runs all scenarios for one agent; `bash scripts/eval.sh <agent> <scenario>` runs exactly one. Returns non-zero exit code if any scenario fails (so it's still scriptable manually).
- `tests/` ships empty on purpose — real scenarios should be sourced from open-source datasets (OWASP/CWE, real PR comments, postmortems) and thoughtfully designed per agent, not bulk-generated. `tests/README.md` documents the format; `docs/eval.md` is the full guide with a worked SQL-injection example end-to-end.

### Changed
- README structure block now lists `tests/` and `scripts/eval.sh`; the Documentation section links to `docs/eval.md`.
- CLAUDE.md commands list and structure block updated with the new eval entries.
- `.gitignore` adds `tests/*/*/last_output.md` and `tests/*/*/last_verdict.json` so local eval artifacts don't pollute git status.

## [0.5.2] - 2026-04-27

### Added
- **One-shot installer upgrade: `bash install.sh --update` (Bash) / `.\install.ps1 -Update` (PowerShell).** Runs `git pull --ff-only` against the cloned repo and then re-installs. Refuses to run if the working tree has uncommitted changes (lists them and exits) or if the remote has diverged (asks the user to resolve manually). Equivalent to the previous two-step `git pull && bash install.sh` but in one command. Help text and `docs/installation.md` updated accordingly.

## [0.5.1] - 2026-04-27

### Changed
- **`gost-report` SKILL.md compacted from ~270 to ~140 lines.** Detail-heavy sections (alternate university profiles, full LaTeX subset, typical content patterns by work type, behaviour notes, extended TitleConfig docs) moved into `references/*.md` — loaded on demand by the agent rather than always-resident. The `⛔ Правило №1` (no em-dashes) and `Writing style` blocks stay in SKILL.md verbatim because they govern every line of generated prose. Net effect: agent invocations carry ~50% less skill context, with the same enforcement on writing rules.
- New reference docs: `references/profiles.md` (UniversityProfile fields, GOST/ITMO presets, custom profile examples), `references/formulas.md` (full LaTeX subset, edge cases, sanitizer vs LaTeX, debugging), `references/patterns.md` (content structure for лаб/ВКР/курсовых/практик/ДЗ), `references/api.md` (TitleConfig fields, behaviour notes — TOC field, list restart, no quotes around topic, image clamping, sanitizer semantics).

## [0.5.0] - 2026-04-27

### Added
- **`gost-report`: native LaTeX equations.** New API method `r.formula(latex, where=None) → int` inserts a LaTeX formula as a real Word equation (OMML), not a rendered image — the formula opens editable in Word, scales cleanly, and looks native. Supports the full `latex2mathml` subset: powers/subscripts, `\frac`, `\sqrt[n]`, Greek letters, `\sum/\int/\prod` with limits, `\bar/\hat/\vec/\tilde/\dot` accents, `pmatrix/bmatrix/vmatrix`, `\text{...}`, fenced groups. Auto-numbered «(N)» pinned to the right edge per GOST 7.32, optional `where=...` block for variable explanations renders below the formula. Returns the formula's number so prose can reference it: `f1 = r.formula(...)` → `r.text(f"По формуле ({f1}) ...")`. The LaTeX string itself bypasses `_sanitize_prose` (so `\text{1—5}` doesn't get mangled); `where` text is sanitized as normal user prose.
- **`gost-report`: isolated Python environment.** New `scripts/ensure_env.py` bootstrap manages the skill's deps in a dedicated venv at `<skill_dir>/.venv/` — fixed location, single per installation, never global. Manager preference: `uv` → `conda --prefix` → stdlib `python -m venv`. Idempotent: hashes `requirements.txt` and skips work when nothing changed (millisecond no-op on warm runs). When the skill ships a new `requirements.txt`, deps auto-update on next invocation. Two modes: `python ensure_env.py` (prints venv python path) and `python ensure_env.py <script>` (bootstraps then `os.execv`s the script with venv python). Cross-platform (POSIX + Windows), stdlib only, with best-effort flock to prevent concurrent-bootstrap races.
- **`gost-report`: `scripts/requirements.txt`** — `python-docx>=1.1.0`, `latex2mathml>=3.77.0`. Single source of truth for both pip and the bootstrap.

### Changed
- **`gost-report` SKILL.md** — `## Dependencies` rewritten: agents are now told to **never** `pip install` globally, always go through `ensure_env.py`. New `### Формулы (LaTeX)` section with examples and supported subset. New `### Запуск через ensure_env.py` block under `## How to use` documenting the bootstrap workflow. Checklist gains two items (formulas render as equations, single venv per skill).
- **Installers** (`install.sh`, `install.ps1`) and **build script** (`scripts/build-skills.sh`) — strip `.venv/` and `.venv.lock` on copy/zip in both directions (install + pull). Prevents a developer's local venv from leaking into system installations or release archives, where the absolute paths in `pyvenv.cfg` would be wrong anyway.
- **`.gitignore`** — `skills/*/.venv/` and `skills/*/.venv.lock` so `git status` stays clean during local skill development.

## [0.4.4] - 2026-04-27

### Added
- **`gost-report`: hard enforcement against em-dashes.** The soft writing-style rule from v0.4.3 was still being ignored — agents kept putting `—` into report prose. Now two layers of protection:
  1. **Prompt.** `SKILL.md` opens with a top-level "⛔ Правило №1 — приоритет выше всего остального" block right after the H1, listing exactly where em/en-dashes are forbidden (`r.text`, `r.task`, headings, list items, captions, table cells) and what to write instead (comma between words, hyphen in ranges). The old writing-style section now defers to this rule.
  2. **Code.** `gost_report.py` adds `_sanitize_prose()` — every content method (`text`, `task`, `h1/h2/h3`, `numbered`, `bullet`, table cells, and the user `caption` argument in `figure`/`table`) runs input through regexes: ` — ` / ` – ` → `, ` and bare `—`/`–` → `-`. The library-generated prefixes «Рисунок N — Описание» and «Таблица N — Описание» are untouched because the dash there is mandated by GOST and inserted by the library itself.
- The sanitizer is a safety net, not a license to relax — `SKILL.md` explicitly tells the model not to rely on it and to write without em-dashes from the start.

## [0.4.3] - 2026-04-26

### Added
- **`gost-report`: hard writing-style rule for prose.** New `## Writing style — обязательно соблюдать` section in `SKILL.md` makes the model write report body text in a natural Russian voice instead of the typical AI register: no em-dashes, no «ёлочки», no канцеляризмы («в ходе выполнения работы», «является», «данный», etc.), varied sentence rhythm. Scoped to prose only — auto-generated captions (`Рисунок N — Описание`) and canonical structural headings (`Введение`, `Заключение`) are explicitly exempt because they're library-formatted per GOST.

## [0.4.2] - 2026-04-26

### Fixed
- **`gost-report`: large screenshots no longer overflow page margins.** `figure()` now clamps the image width to the printable area (A4 width minus left+right body margins from the active profile — typically ~17 cm for ITMO, ~16.5 cm for GOST). Previously, calling `figure(path, caption)` without `width_cm` used the image's native dimensions, which for typical screenshots (1300-2500 px @ 144 DPI = 23-43 cm) extended past the right margin and into the page edge in Word. Verified end-to-end on real lab screenshots (Cisco Packet Tracer captures); images now fit cleanly. Explicitly set `width_cm` is also clamped as a safety net.
- No PIL/Pillow dependency added — image natural size is read via `docx.image.image.Image.from_file`, which python-docx already ships.

## [0.4.1] - 2026-04-26

### Changed
- **Repo renamed** `claude-agents` → `agentpipe`. New URL: https://github.com/zevtos/agentpipe. GitHub redirects the old URL automatically, so existing clones / links / `gh` commands keep working; for cleanliness run `git remote set-url origin git@github.com:zevtos/agentpipe.git`.
- README repositioned: "gated pipeline orchestration for Claude Code and Codex CLI" instead of generic "config" — the orchestration angle (`/feature` runs `pm → architect → reviewer → security → tester → docs`) is what differentiates this project from the saturated "AI config sync" namespace.
- Project identity strings in installers, build scripts, and `CLAUDE.md` updated from `claude-agents` to `agentpipe`. Past CHANGELOG entries kept verbatim for historical accuracy.

## [0.4.0] - 2026-04-26

### Added
- **Codex CLI support** — installer accepts `--target codex` (Bash) / `-Target codex` (PowerShell). Skills go to `~/.agents/skills/` (open-agent-skills standard, NOT `~/.codex/skills/`). Default target is still `claude` so existing workflows are unaffected.
- All installer actions (`install`, `--dry`, `--diff`, `--pull`, `--uninstall`) respect `--target`.
- README has a new "Codex Support" section with the per-target install matrix and rationale for skipping agents/commands in Codex mode.
- `CLAUDE.md` documents the multi-target architecture and the rule to keep both installers in sync.

### Changed
- `install.sh` rewritten with proper argument-loop parsing instead of single-flag dispatch.
- `install.ps1` rewritten with `-Target` parameter, `[ValidateSet("claude","codex")]` validation, and full skills support (the latter was missing in v0.3.x — bug fix).

### Notes
- Codex agents (TOML format in `~/.codex/agents/`) and Codex's lack of custom slash commands mean those Claude assets are intentionally skipped under `--target codex`. Auto-translating agents to TOML is a planned future feature.

## [0.3.1] - 2026-04-26

### Added
- **`skills/gost-report/LICENSE`** (MIT) — every skill now ships its own license so the standalone `.zip` is legally distributable when uploaded to Claude Chat or extracted into `~/.claude/skills/`. Same MIT terms as the repo top-level.
- `CLAUDE.md` updated: skill folders must include `LICENSE`; documented in `Skill Format` and `Adding a New Skill`.
- `SKILL.md` for `gost-report` now has a `## License` section.

## [0.3.0] - 2026-04-26

### Added
- **Skills support** — installer now copies `skills/<name>/` directories to `~/.claude/skills/<name>/` alongside agents and commands
- **`gost-report` skill** — generates Russian academic reports (`.docx`) formatted to GOST 7.32 for any vuz: лабораторные, отчёты по практике, курсовые, ВКР. Ships two profiles (`ITMO_PROFILE` as default, `GOST_PROFILE` as universal baseline) and a `UniversityProfile` dataclass for arbitrary universities (МГУ, СПбГУ, МФТИ, Бауманка, etc.)
- **`scripts/build-skills.sh`** (and `.ps1`) — packages every `skills/<name>/` into `dist/<name>.zip`
- **Release workflow** (`.github/workflows/release.yml`) — on `vX.Y.Z` tag push, builds skill zips and attaches them to the GitHub release; verifies tag matches `VERSION`
- `dist/`, `__pycache__/`, `*.pyc` added to `.gitignore`

### Changed
- Project framing: from "multi-agent orchestration" to "comprehensive Claude Code configuration" (agents + commands + skills)
- `install.sh` and `install.ps1` now also handle skills and report a unified item count
- `CLAUDE.md` documents skill format, the release flow, and the `Adding a New Skill` checklist
- All examples use placeholder names («Фамилия И.О.») — never real ones

## [0.2.0] - 2026-04-13

### Added
- **refactorer agent** — 9th specialist agent for code smell detection, duplication elimination, dead code removal, and test suite refactoring (language-agnostic)
- 4 new research docs: code smells catalog, test patterns, AI-assisted refactoring, architecture debt
- `/refactor` command now runs refactorer as mandatory first step before architect
- `CLAUDE.md` with project conventions, agent/command format specs, and contributor guide — the repo now dogfoods its own pattern
- MIT LICENSE file

### Fixed
- `/sprint` no longer hardcodes `develop` as the base branch; auto-detects the repository's default branch with fallback to `main`
- Repo URLs in README replaced with actual GitHub URL

## [0.1.0] - 2026-04-04

Initial release of claude-agents — multi-agent orchestration for Claude Code.

### Added
- 8 specialist agents: architect, pm, dba, devops, reviewer, security, tester, docs
- 15 orchestration commands: /next, /kickoff, /plan, /onboard, /feature, /fix, /refactor, /sprint, /review, /test, /audit, /deploy, /release, /db, /docs
- 10 research reference documents (security, testing, DevOps, API design, databases, mobile, AI docs)
- Bash installer for macOS, Linux, WSL, Git Bash (`install.sh`)
- PowerShell installer for Windows (`install.ps1`)
- Installer modes: `--dry`, `--diff`, `--pull`, `--uninstall`, `--version`
- Complete documentation: README, commands guide, agents guide, installation guide

### Fixed
- Shell arithmetic compatibility with `set -e` in installer (`((count++))` → `$((count + 1))`)
- Architect agent made mandatory in /plan command pipeline
