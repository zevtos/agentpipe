"""Валидация .docx, сгенерированного gost_report, на соответствие ГОСТ 7.32.

Два слоя потребляют этот модуль:
  L1: Report.save() импортирует validate_docx() и raise'ит GostValidationError
      на failures — модель видит traceback и сама исправляет, без какой-либо
      инструкции про валидацию в SKILL.md.
  L2: Stop-hook вызывает `python3 validate.py --hook`. Hook сканирует cwd на
      sentinel-файлы (.gost-meta.json), валидирует соответствующие .docx,
      возвращает {"decision": "block", "reason": "..."} JSON если есть
      нарушения. Hook никогда не валит Stop pipeline — на любой ошибке exit 0.

Sentinel format: <docx_path>.gost-meta.json — пишется Report.save() рядом с
docx, нужен чтобы L2-хук срабатывал ТОЛЬКО на gost-report-генерации, а не
на любой .docx в cwd (false-fires в чужих проектах).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

# python-docx импортируется ЛЕНИВО (~17ms + lxml): Stop-хук дёргает этот
# скрипт на каждом завершении хода Claude, и в проекте без gost-отчётов
# импорт до early-exit-гейта был чистым налогом (~45ms → ~10ms no-op).
Document = None
qn = None
HAS_DOCX: Optional[bool] = None  # None = ещё не пробовали импортировать


def _load_docx() -> bool:
    """Импортирует python-docx при первом обращении. True = доступен."""
    global Document, qn, HAS_DOCX
    if HAS_DOCX is None:
        try:
            from docx import Document as _Document
            from docx.oxml.ns import qn as _qn
        except ImportError:
            HAS_DOCX = False
        else:
            Document, qn = _Document, _qn
            HAS_DOCX = True
    return HAS_DOCX


def _venv_python() -> Optional[Path]:
    """Путь к скилл-venv питону, если он есть. Venv живёт в глобальном
    state-каталоге (ADR-008) — резолвим через ensure_env (единственный
    источник правды о layout'е); legacy in-skill .venv оставлен fallback'ом
    для не-мигрировавших установок."""
    try:
        import ensure_env
        candidate = ensure_env.venv_python()
        if candidate.exists():
            return candidate
    except Exception:
        pass
    skill_dir = Path(__file__).resolve().parent.parent
    if os.name == "nt":
        candidate = skill_dir / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = skill_dir / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else None


def _maybe_reexec_in_venv(argv: List[str]) -> None:
    """Если python-docx не доступен в текущем питоне, но скилл-venv существует —
    re-exec под venv-питоном. Используется только в CLI-режиме (--hook / --check):
    при `import validate` из gost_report этот код не выполняется (importer уже
    на правильном питоне).

    Если venv нет (юзер ещё ни разу не вызывал r.save()) — функция возвращает
    управление; CLI решит сам, что делать (--hook silently exit 0, --check
    прокинет missing-deps Violation).
    """
    if _load_docx():
        return
    venv_py = _venv_python()
    if venv_py is None:
        return
    try:
        same = Path(sys.executable).resolve() == venv_py.resolve()
    except OSError:
        same = False
    if same:
        return
    script = str(Path(__file__).resolve())
    if os.name == "nt":
        import subprocess
        rc = subprocess.run([str(venv_py), script, *argv]).returncode
        sys.exit(rc)
    os.execv(str(venv_py), [str(venv_py), script, *argv])


SENTINEL_VERSION = 1
SENTINEL_SUFFIX = ".gost-meta.json"
MAX_VIOLATIONS_IN_REPORT = 10
HOOK_GLOB_MAX_DEPTH = 6

# Каталоги, в которые Stop-хук не спускается при сканировании sentinel'ов:
# тяжёлые/чужие деревья, где gost-report-артефактов заведомо нет.
HOOK_SCAN_SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "site-packages", "dist", "build", ".next", ".cache",
    ".idea", ".vscode", "Library", "Applications",
})
# Маркеры корня проекта (тот же набор, что у gost_report.paths()). Хук
# сканирует только если cwd или предок (но не сам $HOME) — это корень проекта.
HOOK_ROOT_MARKERS = (".git", "Makefile", "pyproject.toml", ".gost-report", ".claude")

TIER_FAIL = "a"
TIER_WARN = "b"


@dataclass(frozen=True)
class Violation:
    tier: str        # "a" hard fail, "b" heuristic warn
    code: str        # short rule id
    message: str     # ru, one sentence
    location: str = ""

    def format(self) -> str:
        prefix = "[FAIL]" if self.tier == TIER_FAIL else "[WARN]"
        loc = f" ({self.location})" if self.location else ""
        return f"  {prefix} [{self.code}]{loc}: {self.message}"


# ---- Sentinel ---------------------------------------------------------------

def _sentinel_path(docx_path: Path) -> Path:
    return docx_path.parent / (docx_path.name + SENTINEL_SUFFIX)


def write_sentinel(docx_path: Path, *, profile_name: str = "",
                   generator_version: str = "",
                   allow_dashes: bool = False,
                   style: Optional[dict] = None) -> None:
    payload = {
        "version": SENTINEL_VERSION,
        "generator": "gost_report",
        "generator_version": generator_version,
        "profile": profile_name,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "docx": str(docx_path),
    }
    # Report(allow_dashes=True): тире требует само задание, Stop-хук это видит.
    if allow_dashes:
        payload["allow_dashes"] = True
    # Правила профиля (тире, подписи, доп. проверки): validate_docx берёт их
    # отсюда, чтобы документ по положению МБИ не падал на правилах ИТМО.
    if style:
        payload["style"] = style
    sentinel = _sentinel_path(docx_path)
    sentinel.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def read_sentinel(docx_path: Path) -> Optional[dict]:
    sentinel = _sentinel_path(docx_path)
    if not sentinel.exists():
        return None
    try:
        return json.loads(sentinel.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---- Rules: правила профиля из sentinel --------------------------------------

_NUM = r"(\d+(?:\.\d+)?|[А-ЯЁA-Z]\.\d+)"


@dataclass(frozen=True)
class Rules:
    """Что считать нарушением: правила профиля, с которым собран документ
    (sentinel → "style"). Дефолты = ИТМО/ГОСТ, как до появления профилей."""
    dash: str = "—"
    prose_dashes: str = "strip"          # strip | normalize | keep
    figure_label: str = "Рисунок"
    table_label: str = "Таблица"
    continuation_text: str = "Продолжение таблицы {n}"
    code_font: str = "Courier New"
    checks: Tuple[str, ...] = ()
    keywords_min: int = 5
    keywords_max: int = 15
    bib_min_sources: int = 0
    bib_title: str = ""
    appendix_label: str = "ПРИЛОЖЕНИЕ"
    title_page: bool = True

    @classmethod
    def from_style(cls, style: Optional[dict]) -> "Rules":
        if not style:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        kw = {k: v for k, v in style.items() if k in known}
        if "checks" in kw:
            kw["checks"] = tuple(kw["checks"])
        return cls(**kw)

    def enabled(self, name: str) -> bool:
        return "all" in self.checks or name in self.checks

    @property
    def figure_re(self):
        return _caption_re(self.figure_label, self.dash)

    @property
    def table_re(self):
        return _caption_re(self.table_label, self.dash)

    @property
    def continuation_re(self):
        head, _, tail = self.continuation_text.partition("{n}")
        return re.compile("^" + re.escape(head) + _NUM + re.escape(tail) + r"\s*$")


_CAPTION_CACHE: dict = {}


def _caption_re(label: str, dash: str):
    key = (label, dash)
    if key not in _CAPTION_CACHE:
        _CAPTION_CACHE[key] = re.compile(
            "^" + re.escape(label) + " " + _NUM + " " + re.escape(dash) + " ")
    return _CAPTION_CACHE[key]


DEFAULT_RULES = Rules()


# ---- Paragraph classification ----------------------------------------------

_FIGURE_CAPTION_RE = re.compile(r"^Рисунок (\d+) — ")
_TABLE_CAPTION_RE = re.compile(r"^Таблица (\d+) — ")


def _para_is_caption(text: str, rules: Rules = DEFAULT_RULES) -> bool:
    return bool(rules.figure_re.match(text) or rules.table_re.match(text))


def _number_key(n: str) -> Tuple[str, int]:
    """«3» → ("", 3); «1.2» → ("1", 2); «А.1» → ("А", 1)."""
    if "." in n:
        prefix, _, idx = n.rpartition(".")
        return prefix, int(idx)
    return "", int(n)


class _Sequence:
    """Нумерация без дыр: сквозная или внутри главы / приложения."""

    def __init__(self):
        self._last: dict = {}

    def expected(self, n: str) -> Optional[str]:
        prefix, idx = _number_key(n)
        want = self._last.get(prefix, 0) + 1
        self._last[prefix] = idx
        if idx != want:
            return f"{prefix}.{want}" if prefix else str(want)
        return None


_BIB_TITLE_RE = re.compile(
    r"^(СПИСОК\s+(ИСПОЛЬЗОВАННЫХ\s+)?(ИСТОЧНИКОВ|ЛИТЕРАТУРЫ)"
    r"|БИБЛИОГРАФИЧЕСКИЙ\s+СПИСОК|БИБЛИОГРАФИЯ)", re.IGNORECASE)
_BIB_ENTRY_RE = re.compile(r"^\s*\d+\.\s")


def _para_is_heading(paragraph) -> bool:
    try:
        name = (paragraph.style.name or "")
    except Exception:
        return False
    return name.startswith("Heading") or name.startswith("Заголовок")


def _para_is_code(paragraph, rules: Rules = DEFAULT_RULES) -> bool:
    for run in paragraph.runs:
        if run.font.name == rules.code_font:
            return True
    return False


def _title_section_end(doc, rules: Rules) -> int:
    """Индекс первого абзаца после титула (абзац с w:sectPr закрывает секцию
    титула). 0 — титула нет."""
    if not rules.title_page or qn is None:
        return 0
    for i, p in enumerate(doc.paragraphs):
        ppr = p._element.find(qn("w:pPr"))
        if ppr is not None and ppr.find(qn("w:sectPr")) is not None:
            return i + 1
    return 0


def _para_has_drawing(paragraph) -> bool:
    if qn is None:
        return False
    for _ in paragraph._element.iter(qn("w:drawing")):
        return True
    return False


def _para_text_from_xml(p_element) -> str:
    if qn is None:
        return ""
    parts = []
    for t in p_element.iter(qn("w:t")):
        if t.text:
            parts.append(t.text)
    return "".join(parts)


# ---- Tier (a) checks: hard fail --------------------------------------------

_PROHIBITED_DASH_RE = re.compile(r"[—–]")
_WHERE_LINE_RE = re.compile(r"^\s*(?:где\s*)?[—–]\s")
_M_OMATH = "{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath"
_SPACED_HYPHEN_RE = re.compile(r"(?<=[^\s\d])[ \u00a0]-[ \u00a0](?=[^\s\d])")


def _dash_violation(text: str, rules: Rules) -> Optional[Tuple[int, int, str]]:
    """(start, end, что не так) или None. strip — любое тире; normalize —
    «чужое» тире и дефис с пробелами вместо тире; keep — ничего."""
    if rules.prose_dashes == "keep":
        return None
    if rules.prose_dashes == "normalize":
        other = "—" if rules.dash != "—" else "–"
        rx = re.compile((r"[ \u00a0]" + other + r"[ \u00a0]") if other == "–" else other)
        m = rx.search(text)
        if m:
            name = "длинное «—»" if other == "—" else "короткое «–» с пробелами"
            return m.start(), m.end(), f"{name} вместо «{rules.dash}»"
        m = _SPACED_HYPHEN_RE.search(text)
        if m:
            return m.start(), m.end(), f"дефис с пробелами вместо тире «{rules.dash}»"
        return None
    m = _PROHIBITED_DASH_RE.search(text)
    if m:
        return m.start(), m.end(), "длинное/среднее тире"
    return None


def _check_dashes(doc, rules: Rules = DEFAULT_RULES) -> List[Violation]:
    violations: List[Violation] = []
    in_bibliography = False
    for i, p in enumerate(doc.paragraphs):
        text = p.text
        if not text:
            continue
        stripped = text.strip()
        # Вход/выход из секции списка литературы (там тире обязательно по ГОСТ
        # Р 7.0.5 / 7.0.100). Заголовок-список → вход; следующий структурный заголовок → выход.
        if _BIB_TITLE_RE.match(stripped):
            in_bibliography = True
            continue
        if in_bibliography and _para_is_heading(p) and not _BIB_TITLE_RE.match(stripped):
            in_bibliography = False
        if _para_is_code(p, rules):
            continue
        # Записи библиографии («N. Автор. — …») — тире легально по стандарту.
        if in_bibliography and _BIB_ENTRY_RE.match(stripped):
            continue
        cap = rules.figure_re.match(text) or rules.table_re.match(text)
        if cap:
            if rules.prose_dashes != "normalize":
                continue
            text = text[cap.end():]
        # «где m — масса, кг;» из r.formula(where=[...]): символ — формула (его
        # нет в p.text), тире после него ставит библиотека, как в подписях.
        where = _WHERE_LINE_RE.match(text)
        if where and p._element.find(".//" + _M_OMATH) is not None:
            text = text[where.end():]
        found = _dash_violation(text, rules)
        if found:
            a, b, what = found
            snippet = text[max(0, a - 20):min(len(text), b + 20)]
            violations.append(Violation(
                tier=TIER_FAIL, code="dashes",
                message=f"{what} в основном тексте: «...{snippet}...»",
                location=f"параграф {i + 1}",
            ))
    # doc.paragraphs не обходит таблицы → тире в ячейках остаётся незамеченным.
    # Бэкстоп: даже если какой-то будущий sink забудет санировать, запрещённое
    # тире, дошедшее до ячейки docx, всё равно жёстко падает. Подписи живут
    # ВНЕ ячеек (это обычные параграфы тела), так что caption/bibliography
    # исключения здесь не нужны.
    for ti, table in enumerate(getattr(doc, "tables", []) or []):
        for ri, row in enumerate(table.rows):
            for ci, cell in enumerate(row.cells):
                for cp in cell.paragraphs:
                    ctext = cp.text
                    if not ctext:
                        continue
                    found = _dash_violation(ctext, rules)
                    if found:
                        a, b, what = found
                        snippet = ctext[max(0, a - 20):min(len(ctext), b + 20)]
                        violations.append(Violation(
                            tier=TIER_FAIL, code="dashes",
                            message=f"{what} в ячейке таблицы: «...{snippet}...»",
                            location=f"таблица {ti + 1}, строка {ri + 1}, столбец {ci + 1}",
                        ))
    return violations


_BARE_LATEX_RE = re.compile(
    r"\\(?:frac|sum|int|prod|sqrt|text|begin|end|cdot|times|div|"
    r"alpha|beta|gamma|delta|epsilon|zeta|eta|theta|iota|kappa|lambda|"
    r"mu|nu|xi|omicron|pi|rho|sigma|tau|upsilon|phi|chi|psi|omega|"
    r"infty|partial|nabla|hat|bar|tilde|vec|dot|left|right|mathbb|mathcal)"
    r"\b"
)


def _check_bare_latex(doc, rules: Rules = DEFAULT_RULES) -> List[Violation]:
    violations: List[Violation] = []
    for i, p in enumerate(doc.paragraphs):
        text = p.text
        if not text or _para_is_code(p, rules):
            continue
        m = _BARE_LATEX_RE.search(text)
        if m:
            violations.append(Violation(
                tier=TIER_FAIL, code="bare-latex",
                message=(f"LaTeX-команда {m.group(0)!r} попала в текст. "
                         "Используй r.formula(...) для уравнений."),
                location=f"параграф {i + 1}",
            ))
    return violations


def _check_figure_captions(doc, rules: Rules = DEFAULT_RULES) -> List[Violation]:
    violations: List[Violation] = []
    paragraphs = doc.paragraphs
    seq = _Sequence()
    skip = _title_section_end(doc, rules)   # логотип бланка на титуле не рисунок
    label, dash = rules.figure_label, rules.dash
    for i, p in enumerate(paragraphs):
        if i < skip or not _para_has_drawing(p):
            continue
        # Подпись — в ближайших абзацах после изображения (между ними может
        # стоять экспликация рисунка).
        m = None
        for j in range(i + 1, min(i + 4, len(paragraphs))):
            t = paragraphs[j].text
            if not t.strip():
                continue
            m = rules.figure_re.match(t)
            if m or _para_has_drawing(paragraphs[j]):
                break
        if not m:
            violations.append(Violation(
                tier=TIER_FAIL, code="figure-no-caption",
                message=(f"у рисунка нет подписи «{label} N {dash} Описание» "
                         "сразу после изображения."),
                location=f"параграф {i + 1}",
            ))
            continue
        want = seq.expected(m.group(1))
        if want:
            violations.append(Violation(
                tier=TIER_FAIL, code="figure-numbering",
                message=(f"номер рисунка {m.group(1)}, ожидался {want}. "
                         "Нумерация не должна прыгать."),
                location=f"параграф {i + 1}",
            ))
    return violations


def _check_table_captions(doc, rules: Rules = DEFAULT_RULES) -> List[Violation]:
    """Каждая таблица должна иметь подпись «Таблица N — ...» прямо перед ней.

    Плавающие borderless-таблицы (городкод/год на титульнике, у них
    `<w:tblpPr>` внутри `<w:tblPr>`) пропускаются — у них нет подписи по
    дизайну. Часть таблицы после «Продолжение таблицы N» — продолжение, не
    новая таблица. Таблицы титула (бланк) не проверяются.
    """
    violations: List[Violation] = []
    if qn is None:
        return violations
    body = doc.element.body
    seq = _Sequence()
    last_nonempty_text = ""
    in_title = rules.title_page
    label, dash = rules.table_label, rules.dash
    for child in body:
        local = child.tag.split("}", 1)[-1]
        if local == "p":
            text = _para_text_from_xml(child)
            if text.strip():
                last_nonempty_text = text
            ppr = child.find(qn("w:pPr"))
            if in_title and ppr is not None and ppr.find(qn("w:sectPr")) is not None:
                in_title = False
                last_nonempty_text = ""
        elif local == "tbl":
            tblpr = child.find(qn("w:tblPr"))
            is_floating = (tblpr is not None
                           and tblpr.find(qn("w:tblpPr")) is not None)
            if is_floating or in_title:
                last_nonempty_text = ""
                continue
            if rules.continuation_re.match(last_nonempty_text.strip()):
                last_nonempty_text = ""
                continue
            m = rules.table_re.match(last_nonempty_text)
            if not m:
                violations.append(Violation(
                    tier=TIER_FAIL, code="table-no-caption",
                    message=(f"у таблицы нет подписи «{label} N {dash} Описание» "
                             "прямо перед ней."),
                ))
            else:
                want = seq.expected(m.group(1))
                if want:
                    violations.append(Violation(
                        tier=TIER_FAIL, code="table-numbering",
                        message=f"номер таблицы {m.group(1)}, ожидался {want}.",
                    ))
            last_nonempty_text = ""
    return violations


# ---- Tier (b) checks: heuristic warn ---------------------------------------

_PLACEHOLDER_NAME_RE = re.compile(r"Фамилия\s+И\.О\.")


def _check_placeholder_name(doc, rules: Rules = DEFAULT_RULES) -> List[Violation]:
    """Tier (b): шаблонное ФИО из примеров скилла. Warn, не fail —
    у юзера могут быть законные причины (тест, демо, etc.); ложный fail
    на example_lab.py хуже, чем пропущенный warn."""
    full_text = "\n".join(p.text for p in doc.paragraphs)
    if _PLACEHOLDER_NAME_RE.search(full_text):
        return [Violation(
            tier=TIER_WARN, code="placeholder-name",
            message=("«Фамилия И.О.» из примеров skill'а попало в документ. "
                     "Подставь реальное ФИО студента/преподавателя."),
        )]
    return []


_AI_TONE_PATTERNS = [
    re.compile(r"в\s+ходе\s+выполнения\s+работ\w*", re.IGNORECASE),
    re.compile(r"в\s+результате\s+проведённого\s+исследовани\w*", re.IGNORECASE),
    re.compile(r"\b(?:данн\w+|вышеуказанн\w+|нижеследующ\w+)\b", re.IGNORECASE),
    re.compile(r"\bпредставляет\s+собой\b", re.IGNORECASE),
    re.compile(r"\bосуществляется\b", re.IGNORECASE),
    re.compile(r"стоит\s+отметить,?\s+что", re.IGNORECASE),
    re.compile(r"необходимо\s+подчеркнуть,?\s+что", re.IGNORECASE),
    re.compile(r"таким\s+образом,?\s+можно\s+сделать\s+вывод", re.IGNORECASE),
]


def _check_ai_tone(doc, rules: Rules = DEFAULT_RULES) -> List[Violation]:
    violations: List[Violation] = []
    for i, p in enumerate(doc.paragraphs):
        text = p.text
        if not text or _para_is_caption(text, rules) or _para_is_code(p, rules):
            continue
        for rx in _AI_TONE_PATTERNS:
            m = rx.search(text)
            if m:
                violations.append(Violation(
                    tier=TIER_WARN, code="ai-tone",
                    message=(f"шаблонная фраза «{m.group(0)}» — "
                             "перепиши естественнее (см. writing-style в SKILL.md)."),
                    location=f"параграф {i + 1}",
                ))
                break
    return violations


def _check_caption_short(doc, rules: Rules = DEFAULT_RULES) -> List[Violation]:
    violations: List[Violation] = []
    for i, p in enumerate(doc.paragraphs):
        text = p.text
        m = rules.figure_re.match(text)
        if not m:
            continue
        after = text[m.end():].strip()
        if len(after.split()) < 2:
            violations.append(Violation(
                tier=TIER_WARN, code="caption-short",
                message=(f"подпись рисунка слишком короткая: «{after}». "
                         "Дай осмысленное описание."),
                location=f"параграф {i + 1}",
            ))
    return violations


# ---- Проверки по профилю (Rules.checks): все WARN ---------------------------

_M_T = "{http://schemas.openxmlformats.org/officeDocument/2006/math}t"


def _full_text(p_el) -> str:
    """Текст абзаца вместе с инлайн-формулами (m:t): иначе «где $m$ – …»
    выглядит как «где  – …» с двойным пробелом."""
    return "".join(el.text or "" for el in p_el.iter(qn("w:t"), _M_T))


def _check_double_space(doc, rules: Rules = DEFAULT_RULES) -> List[Violation]:
    out: List[Violation] = []
    for i, p in enumerate(doc.paragraphs):
        if "  " in _full_text(p._element) and not _para_is_code(p, rules):
            out.append(Violation(TIER_WARN, "double-space",
                                 "двойной пробел в тексте", f"параграф {i + 1}"))
    return out


def _check_heading_period(doc, rules: Rules = DEFAULT_RULES) -> List[Violation]:
    out: List[Violation] = []
    for i, p in enumerate(doc.paragraphs):
        if not _para_is_heading(p):
            continue
        text = p.text.strip()
        if text.endswith(".") and not text.endswith(("и др.", "т. д.", "т. п."))\
                and ". " not in text[:-1]:
            out.append(Violation(TIER_WARN, "heading-period",
                                 f"точка в конце заголовка: «{text[-40:]}»",
                                 f"параграф {i + 1}"))
    return out


def _spacing_pt(p_el) -> Tuple[float, float]:
    ppr = p_el.find(qn("w:pPr"))
    sp = ppr.find(qn("w:spacing")) if ppr is not None else None
    if sp is None:
        return 0.0, 0.0
    before = sp.get(qn("w:before")) or "0"
    after = sp.get(qn("w:after")) or "0"
    try:
        return int(before) / 20, int(after) / 20
    except ValueError:
        return 0.0, 0.0


def _check_paragraph_spacing(doc, rules: Rules = DEFAULT_RULES) -> List[Violation]:
    """Интервалы до/после у абзацев основного текста (по ширине), кроме
    формул и их «где …»: положения запрещают межабзацные интервалы."""
    out: List[Violation] = []
    if qn is None:
        return out
    skip = _title_section_end(doc, rules)
    for i, p in enumerate(doc.paragraphs):
        if i < skip or _para_is_heading(p) or not p.text.strip():
            continue
        el = p._element
        ppr = el.find(qn("w:pPr"))
        jc = ppr.find(qn("w:jc")) if ppr is not None else None
        if jc is None or jc.get(qn("w:val")) not in ("both", "distribute"):
            continue
        ind = ppr.find(qn("w:ind"))
        if (ind is not None and (ind.get(qn("w:left")) or "0") not in ("0", "")) \
                or p.text.lstrip().startswith("где ") \
                or el.find(".//{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath") is not None:
            continue
        before, after = _spacing_pt(el)
        if before > 0 or after > 0:
            out.append(Violation(TIER_WARN, "paragraph-spacing",
                                 f"интервал до/после абзаца ({before:g}/{after:g} пт)",
                                 f"параграф {i + 1}"))
    return out


def _mentioned_numbers(text: str, stem: str) -> set:
    """Номера из «таблице 2», «таблицах 1–3», «рисунках 1.1 и 1.2»."""
    found: set = set()
    rx = re.compile(stem + r"\w*\.?[\s\u00a0]+(" + _NUM + r"(?:\s*(?:[–—-]|,|и)\s*" + _NUM + r")*)",
                    re.IGNORECASE)
    for m in rx.finditer(text):
        chunk = m.group(1)
        nums = re.findall(_NUM, chunk)
        found.update(nums)
        for a, b in re.findall(_NUM + r"\s*[–—-]\s*" + _NUM, chunk):
            pa, ia = _number_key(a)
            pb, ib = _number_key(b)
            if pa == pb and ib > ia:
                found.update((f"{pa}.{k}" if pa else str(k)) for k in range(ia, ib + 1))
    return found


def _check_mentions(doc, rules: Rules, *, kind: str) -> List[Violation]:
    out: List[Violation] = []
    is_table = kind == "table"
    cap_re = rules.table_re if is_table else rules.figure_re
    stem = r"табл" if is_table else r"рис"
    paragraphs = [p.text for p in doc.paragraphs]
    for i, text in enumerate(paragraphs):
        m = cap_re.match(text)
        if not m:
            continue
        n = m.group(1)
        scope = paragraphs[:i] if is_table else paragraphs[:i] + paragraphs[i + 1:]
        scope = [t for t in scope if not cap_re.match(t)]
        if n not in _mentioned_numbers("\n".join(scope), stem):
            what = "таблицу" if is_table else "рисунок"
            where = " до неё" if is_table else ""
            out.append(Violation(TIER_WARN, f"{kind}-mention",
                                 f"нет ссылки на {what} {n} в тексте{where}",
                                 f"параграф {i + 1}"))
    return out


def _check_table_mention(doc, rules: Rules = DEFAULT_RULES) -> List[Violation]:
    return _check_mentions(doc, rules, kind="table")


def _check_figure_mention(doc, rules: Rules = DEFAULT_RULES) -> List[Violation]:
    return _check_mentions(doc, rules, kind="figure")


def _check_keywords_count(doc, rules: Rules = DEFAULT_RULES) -> List[Violation]:
    out: List[Violation] = []
    for i, p in enumerate(doc.paragraphs):
        try:
            name = p.style.name or ""
        except Exception:
            name = ""
        if name != "Keywords":
            continue
        n = len([w for w in p.text.split(",") if w.strip()])
        if not rules.keywords_min <= n <= rules.keywords_max:
            out.append(Violation(TIER_WARN, "keywords-count",
                                 f"ключевых слов {n}, нужно "
                                 f"{rules.keywords_min}–{rules.keywords_max}",
                                 f"параграф {i + 1}"))
    return out


def _check_bib_count(doc, rules: Rules = DEFAULT_RULES) -> List[Violation]:
    if not rules.bib_min_sources:
        return []
    count, inside, seen = 0, False, False
    for p in doc.paragraphs:
        text = p.text.strip()
        if _BIB_TITLE_RE.match(text):
            inside = seen = True
            continue
        if inside and _para_is_heading(p):
            inside = False
        if inside and _BIB_ENTRY_RE.match(text):
            count += 1
    # Без списка источников (наброски, глава отдельным файлом) — не о чем говорить.
    if seen and count < rules.bib_min_sources:
        return [Violation(TIER_WARN, "bib-count",
                          f"источников {count}, положение требует не менее "
                          f"{rules.bib_min_sources}")]
    return []


# ---- Orchestration ---------------------------------------------------------

_ALL_CHECKS = [
    _check_dashes,
    _check_bare_latex,
    _check_figure_captions,
    _check_table_captions,
    _check_placeholder_name,
    _check_ai_tone,
    _check_caption_short,
]

# Проверки по требованию профиля: имя в UniversityProfile.checks → функция.
_PROFILE_CHECKS = {
    "double-space": _check_double_space,
    "heading-period": _check_heading_period,
    "paragraph-spacing": _check_paragraph_spacing,
    "table-mention": _check_table_mention,
    "figure-mention": _check_figure_mention,
    "keywords-count": _check_keywords_count,
    "bib-count": _check_bib_count,
}


def _safe_run_check(check, doc, rules: Rules = DEFAULT_RULES) -> List[Violation]:
    """Один правило-чек, обёрнутый в try/except. Ошибки внутри правила
    становятся `rule-error` WARN'ом, чтобы не ронять остальные правила.
    Единая точка реакции на внутренние сбои валидатора."""
    try:
        return list(check(doc, rules))
    except Exception as e:
        return [Violation(
            tier=TIER_WARN, code="rule-error",
            message=f"внутренняя ошибка правила {check.__name__}: {e}",
        )]


def validate_docx(path: Path, *, style: Optional[dict] = None) -> List[Violation]:
    """Проверка .docx. Правила — style (если передан), иначе из sentinel
    рядом с файлом, иначе дефолты ИТМО/ГОСТ."""
    if not _load_docx():
        return [Violation(
            tier=TIER_FAIL, code="missing-deps",
            message=("python-docx не установлен; не могу прочитать .docx. "
                     "Запусти через scripts/ensure_env.py."),
        )]
    try:
        doc = Document(str(path))
    except Exception as e:
        return [Violation(
            tier=TIER_FAIL, code="read-failed",
            message=f"не смог открыть .docx: {e}",
        )]
    sentinel = read_sentinel(path) or {}
    rules = Rules.from_style(style if style is not None else sentinel.get("style"))
    allow_dashes = bool(sentinel.get("allow_dashes")) and style is None
    violations: List[Violation] = []
    for check in _ALL_CHECKS:
        if (allow_dashes or rules.prose_dashes == "keep") and check is _check_dashes:
            continue
        violations.extend(_safe_run_check(check, doc, rules))
    for name, check in _PROFILE_CHECKS.items():
        if rules.enabled(name):
            violations.extend(_safe_run_check(check, doc, rules))
    return violations


def has_failures(violations: Iterable[Violation]) -> bool:
    return any(v.tier == TIER_FAIL for v in violations)


def format_error(docx_path: Path, violations: List[Violation]) -> str:
    fails = [v for v in violations if v.tier == TIER_FAIL]
    warns = [v for v in violations if v.tier == TIER_WARN]
    ordered = fails + warns

    lines = [f"Файл {docx_path} не прошёл ГОСТ-проверку:"]
    shown = ordered[:MAX_VIOLATIONS_IN_REPORT]
    for v in shown:
        lines.append(v.format())
    omitted = len(ordered) - len(shown)
    if omitted > 0:
        lines.append(f"  ... и ещё {omitted} "
                     f"(показаны первые {MAX_VIOLATIONS_IN_REPORT}).")
    lines.append("")
    lines.append("Перегенерируй документ: исправь причины и снова вызови r.save().")
    lines.append("Это валидация ГОСТ, не баг библиотеки. Не подавляй это исключение")
    lines.append("и не используй python-docx напрямую — валидация выполняется "
                 "автоматически в r.save().")
    return "\n".join(lines)


# ---- CLI -------------------------------------------------------------------

def _under_project_root(cwd: Path) -> bool:
    """True, если cwd или один из предков (но НЕ сам $HOME) содержит маркер
    корня проекта. Гейт нужен, чтобы хук не сканировал $HOME / `/` / случайные
    каталоги: в $HOME всегда есть ~/.claude, поэтому его исключаем явно."""
    try:
        home = Path.home()
    except Exception:
        home = None
    for d in (cwd, *cwd.parents):
        if home is not None and d == home:
            break
        for marker in HOOK_ROOT_MARKERS:
            if (d / marker).exists():
                return True
    return False


def _scan_sentinels(cwd: Path, max_depth: int = HOOK_GLOB_MAX_DEPTH) -> List[Path]:
    """Ограниченный обход вниз от cwd. В отличие от `cwd.rglob(...)` (который
    спускается во ВСЁ дерево, а depth-cap лишь фильтрует результат), здесь обход
    обрезается на глубине и пропускает шумные каталоги — из большого корня хук
    не обходит весь поддерев.

    Семантика глубины совпадает с прежним `len(rel.parts) <= max_depth`:
    sentinel в каталоге глубины d имеет rel.parts == d + 1, поэтому в дерево
    спускаемся только пока (d + 1) дочернего каталога <= max_depth."""
    out: List[Path] = []
    max_dir_depth = max_depth - 1            # каталог сентинела: depth <= max-1
    stack: List[Tuple[Path, int]] = [(cwd, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    if (depth < max_dir_depth
                            and entry.name not in HOOK_SCAN_SKIP_DIRS
                            and not entry.is_symlink()):
                        stack.append((entry, depth + 1))
                elif entry.name.endswith(SENTINEL_SUFFIX):
                    out.append(entry)
            except OSError:
                continue
    return out


def _hook_main(cwd: Path, argv: Optional[List[str]] = None) -> int:
    """Stop-хук: сканирует cwd на sentinel'ы, валидирует docx, печатает
    decision-block JSON если есть FAIL'ы. Hook всегда exit 0 — лучше тихо
    промолчать, чем уронить Stop-pipeline.

    Порядок гейтов важен: дешёвые stdlib-проверки (project root, sentinel-скан)
    идут ПЕРВЫМИ; python-docx импортируется — и re-exec в venv происходит —
    только когда sentinel'ы реально найдены. No-op путь хука платит ~10ms
    вместо ~45ms, а re-exec не случается в проектах без отчётов вовсе."""
    if not _under_project_root(cwd):
        return 0
    try:
        sentinels = _scan_sentinels(cwd)
    except Exception:
        return 0

    if not sentinels:
        return 0

    if not _load_docx():
        # execv не возвращается, если venv найден; иначе тихо молчим (exit 0).
        _maybe_reexec_in_venv(list(argv) if argv is not None else ["--hook"])
        return 0

    by_doc: dict = {}
    for sentinel in sentinels:
        try:
            payload = json.loads(sentinel.read_text(encoding="utf-8"))
            docx_str = payload.get("docx") or ""
            docx = Path(docx_str)
            if not docx.is_absolute():
                docx = sentinel.parent / Path(docx_str).name
            if not docx.exists():
                # Sentinel остался от удалённого .docx — игнорируем.
                continue
            violations = validate_docx(docx)
            fails = [v for v in violations if v.tier == TIER_FAIL]
            if fails:
                by_doc[docx] = fails
        except Exception:
            continue

    if not by_doc:
        return 0

    parts = [format_error(d, vs) for d, vs in by_doc.items()]
    reason = "\n\n".join(parts)
    print(json.dumps({"decision": "block", "reason": reason},
                     ensure_ascii=False))
    return 0


def _profile_style(spec: str) -> dict:
    """--profile NAME|path.toml → правила проверки этого стиля."""
    import gost_report
    profile = gost_report.load_profile(spec)
    return gost_report.validation_style(profile)


def _check_main(path_str: str, profile: Optional[str] = None) -> int:
    """`--check <path>`: ручная проверка одного .docx, человеко-читаемый отчёт.
    `--profile mbi` — правила стиля для .docx без sentinel (чужой файл).

    Exit: 0 если passed (включая warnings-only), 1 если есть FAIL,
    2 при ошибке вызова.
    """
    path = Path(path_str)
    if not path.exists():
        print(f"validate.py: file not found: {path}", file=sys.stderr)
        return 2
    style = _profile_style(profile) if profile else None
    violations = validate_docx(path, style=style)
    if not violations:
        print(f"validate.py: {path} — passed (0 нарушений)")
        return 0
    print(format_error(path, violations))
    return 1 if has_failures(violations) else 0


def main(argv: Optional[List[str]] = None) -> int:
    parsed_argv = list(sys.argv[1:]) if argv is None else list(argv)

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--hook", action="store_true",
                        help="Stop-hook: сканирует cwd, JSON decision-block")
    parser.add_argument("--check", metavar="PATH",
                        help="ручная проверка одного .docx")
    parser.add_argument("--profile", metavar="STYLE",
                        help="стиль для --check (mbi, путь к .toml); без него — "
                             "правила из sentinel рядом с файлом или ИТМО/ГОСТ")
    args = parser.parse_args(parsed_argv)

    try:
        if args.hook:
            # re-exec отложен внутрь _hook_main — случается только при
            # найденных sentinel'ах, не на каждом Stop.
            return _hook_main(Path.cwd(), parsed_argv)
        if args.check:
            if not _load_docx():
                _maybe_reexec_in_venv(parsed_argv)
            return _check_main(args.check, args.profile)
        parser.print_help()
        return 2
    except Exception:
        if args.hook:
            return 0
        import traceback
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
