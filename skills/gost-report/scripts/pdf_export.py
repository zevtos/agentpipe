"""docx → PDF через LibreOffice и проверка вёрстки готового PDF.

LibreOffice работает со своим профилем в state-dir скилла. Туда подключён русский
словарь переносов (в сборке для macOS его нет), и конвертация не конфликтует
с уже открытым у пользователя LibreOffice. Диалогов доступа, как у Word, здесь нет.

Stdlib-only. Внешние программы: soffice и unopkg, для проверки pdftotext и pdffonts
(poppler; без него проверка молча пропускается).
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from typing import List, Optional, Tuple, Union
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

from typography import SHORT_WORDS

WORD_JOINER = "⁠"

_HYPH_URL = ("https://raw.githubusercontent.com/LibreOffice/dictionaries/"
             "12e591abdbc5565e5018ed9b8fbe6d635ade87d7/ru_RU/hyph_ru_RU.dic")
_HYPH_SHA256 = "e9a6ae92765fc7e63d879f2ccf3f50f2b95bc8ed7618a6f31c34ce7d39844c83"

_MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="http://openoffice.org/2001/manifest">
  <manifest:file-entry manifest:full-path="dictionaries.xcu" manifest:media-type="application/vnd.sun.star.configuration-data"/>
  <manifest:file-entry manifest:full-path="compatibility.xcu" manifest:media-type="application/vnd.sun.star.configuration-data"/>
</manifest:manifest>
"""
# По умолчанию NBSP в LibreOffice фиксированной ширины (tdf#41652): при выключке по ширине
# рядом с широкими пробелами остаются плотные «в 2015». Word такие пробелы растягивает.
_COMPATIBILITY_XCU = """<?xml version="1.0" encoding="UTF-8"?>
<oor:component-data xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" oor:name="Compatibility" oor:package="org.openoffice.Office">
  <node oor:name="AllFileFormats">
    <node oor:name="_default"><prop oor:name="UseVariableWidthNBSP" oor:op="fuse"><value>true</value></prop></node>
    <node oor:name="_user"><prop oor:name="UseVariableWidthNBSP" oor:op="fuse"><value>true</value></prop></node>
  </node>
</oor:component-data>
"""
_DICTIONARIES_XCU = """<?xml version="1.0" encoding="UTF-8"?>
<oor:component-data xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" oor:name="Linguistic" oor:package="org.openoffice.Office">
  <node oor:name="ServiceManager">
    <node oor:name="Dictionaries">
      <node oor:name="HyphDic_ru_gost_report" oor:op="fuse">
        <prop oor:name="Locations" oor:type="oor:string-list"><value>%origin%/hyph_ru_RU.dic</value></prop>
        <prop oor:name="Format" oor:type="xs:string"><value>DICT_HYPH</value></prop>
        <prop oor:name="Locales" oor:type="oor:string-list"><value>ru-RU</value></prop>
      </node>
    </node>
  </node>
</oor:component-data>
"""
# Без своего конфига LibreOffice в режиме --headless на macOS не видит системные
# шрифты и подменяет Times New Roman на Liberation Serif.
_MACOS_FONTS_CONF = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <dir>/System/Library/Fonts</dir>
  <dir>/Library/Fonts</dir>
  <dir>~/Library/Fonts</dir>
</fontconfig>
"""


def _warn(message: str) -> None:
    sys.stderr.write(f"gost-report pdf warn: {message}\n")


def _find_soffice() -> str:
    candidates = (
        os.environ.get("GOST_REPORT_SOFFICE"),
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        r"C:\Program Files\LibreOffice\program\soffice.exe",
    )
    for cand in candidates:
        if cand and Path(cand).exists():
            return cand
    raise RuntimeError(
        "LibreOffice не найден, PDF собрать нечем. Поставь его: "
        "brew install --cask libreoffice / sudo apt install libreoffice-writer "
        "или укажи путь в GOST_REPORT_SOFFICE."
    )


def _hyph_dict(state: Path) -> Optional[Path]:
    """Словарь переносов из репозитория LibreOffice, закреплён коммитом и sha256."""
    target = state / "hyph_ru_RU.dic"
    if target.is_file():
        return target
    try:
        with urllib.request.urlopen(_HYPH_URL, timeout=30) as resp:
            data = resp.read()
    except OSError as e:
        _warn(f"не скачал словарь переносов ({e}); PDF будет без русских переносов.")
        return None
    if hashlib.sha256(data).hexdigest() != _HYPH_SHA256:
        _warn("словарь переносов не совпал по sha256; PDF будет без русских переносов.")
        return None
    state.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def _ensure_profile(soffice: str, state: Path) -> Path:
    """Профиль LibreOffice с русскими переносами и NBSP как у Word. Готовится один раз."""
    profile = state / "lo-profile"
    marker = profile / f".ready-{hashlib.sha256((_HYPH_SHA256 + _COMPATIBILITY_XCU + _MANIFEST).encode()).hexdigest()[:12]}"
    if marker.exists():
        return profile
    dic = _hyph_dict(state)
    if dic is None:
        return profile
    real = Path(soffice).resolve()
    unopkg = next((p for p in (real.with_name("unopkg"), real.with_name("unopkg.com"))
                   if p.exists()), None) or shutil.which("unopkg")
    if unopkg is None:
        _warn("unopkg не найден; PDF будет без русских переносов.")
        return profile
    with tempfile.TemporaryDirectory() as tmp:
        oxt = Path(tmp) / "gost-report-hyph-ru.oxt"
        with zipfile.ZipFile(oxt, "w") as z:
            z.writestr("META-INF/manifest.xml", _MANIFEST)
            z.writestr("dictionaries.xcu", _DICTIONARIES_XCU)
            z.writestr("compatibility.xcu", _COMPATIBILITY_XCU)
            z.write(dic, "hyph_ru_RU.dic")
        res = subprocess.run(
            [str(unopkg), "add", "-f", f"-env:UserInstallation={profile.as_uri()}", str(oxt)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
        )
    if res.returncode != 0:
        _warn(f"unopkg не подключил словарь переносов: {res.stderr.strip()[-200:]}")
        return profile
    profile.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return profile


def _prepare_for_libreoffice(docx: Path, tmp: Path) -> Path:
    """Копия .docx только для конвертации, сам файл остаётся как есть.

    WORD JOINER вокруг знака запрещает перенос внутри «400–430» и «АИ-98». В Word
    у этих знаков такого средства нет, а в .docx эти символы только мешали бы.
    """
    def fix(m: "re.Match[str]") -> str:
        text = re.sub(r"(?<=\w)–(?=\w)", f"{WORD_JOINER}–{WORD_JOINER}", m[2])
        text = re.sub(r"(?<=\d)-(?=\w)|(?<=\w)-(?=\d)", f"{WORD_JOINER}-{WORD_JOINER}", text)
        return m[1] + text + m[3]

    out = tmp / docx.name
    with zipfile.ZipFile(docx) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                xml = data.decode("utf-8")
                data = re.sub(r"(<w:t(?: [^>]*)?>)([^<]*)(</w:t>)", fix, xml).encode("utf-8")
            zout.writestr(item, data)
    return out


def export_pdf(docx: Union[str, Path], pdf: Optional[Union[str, Path]] = None) -> Path:
    """Конвертирует .docx в PDF. Без pdf кладёт файл рядом с .docx. Возвращает путь."""
    docx = Path(docx).resolve()
    target = Path(pdf).resolve() if pdf else docx.with_suffix(".pdf")
    soffice = _find_soffice()
    from ensure_env import _skill_state_dir
    state = _skill_state_dir()
    state.mkdir(parents=True, exist_ok=True)
    profile = _ensure_profile(soffice, state)
    env = dict(os.environ)
    if sys.platform == "darwin":
        conf = state / "fonts.conf"
        conf.write_text(_MACOS_FONTS_CONF, encoding="utf-8")
        env["FONTCONFIG_FILE"] = str(conf)
    with tempfile.TemporaryDirectory() as tmp:
        prepared = _prepare_for_libreoffice(docx, Path(tmp))
        res = subprocess.run(
            [soffice, f"-env:UserInstallation={profile.as_uri()}", "--headless",
             "--convert-to", "pdf", "--outdir", tmp, str(prepared)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, env=env,
        )
        produced = Path(tmp) / f"{docx.stem}.pdf"
        if not produced.exists():
            raise RuntimeError(f"LibreOffice не создал PDF: {res.stderr.strip()[-300:]}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(target))
    return target


# ---- Проверка вёрстки ------------------------------------------------------

# Пробел шире трёх нормальных (0,25 кегля у Times) уже читается как дыра в строке.
_WIDE_GAP_EM = 0.75

@dataclass(frozen=True)
class Issue:
    code: str
    page: int
    message: str

    def __str__(self) -> str:
        return f"{self.code} (стр. {self.page}): {self.message}"


@dataclass(frozen=True)
class Line:
    page: int
    y0: float
    y1: float
    words: Tuple[Tuple[str, float, float], ...]  # (текст, x0, x1)

    @property
    def x0(self) -> float:
        return self.words[0][1]

    @property
    def x1(self) -> float:
        return self.words[-1][2]

    @property
    def tokens(self) -> List[str]:
        return " ".join(w[0] for w in self.words).split()  # split() режет и по NBSP


def _read_lines(pdf: Path) -> List[Line]:
    res = subprocess.run(["pdftotext", "-bbox-layout", str(pdf), "-"],
                         capture_output=True, text=True, timeout=60)
    lines: List[Line] = []
    # Разбираем вывод pdftotext, а не сторонний XML: текст PDF в нём экранирован,
    # своего DTD быть не может, поэтому defusedxml здесь лишняя зависимость.
    root = ET.fromstring(res.stdout)
    page_no = 0
    for page in root.iter():
        if not page.tag.endswith("}page"):
            continue
        page_no += 1
        height = float(page.get("height"))
        page_lines = []
        for el in page.iter():
            if not el.tag.endswith("}line"):
                continue
            words = tuple(((w.text or "").replace(WORD_JOINER, ""),
                           float(w.get("xMin")), float(w.get("xMax")))
                          for w in el if w.tag.endswith("}word"))
            y0, y1 = float(el.get("yMin")), float(el.get("yMax"))
            is_page_number = (len(words) == 1 and words[0][0].isdigit() and y0 > height * 0.9)
            if words and not is_page_number:
                page_lines.append(Line(page_no, y0, y1, words))
        lines.extend(sorted(page_lines, key=lambda l: l.y0))
    return lines


def check_lines(lines: List[Line]) -> List[Issue]:
    """Дефекты набора: висячие предлоги, дыры, короткие концовки, висячие строки."""
    if not lines:
        return []
    left = min(l.x0 for l in lines)
    right = max(l.x1 for l in lines)
    em = statistics.median(l.y1 - l.y0 for l in lines) / 1.1
    measure = right - left
    issues: List[Issue] = []
    wide = []
    hyphens = 0

    def full(line: Line) -> bool:
        return line.x1 >= right - 1.5

    def indented(line: Line) -> bool:
        return line.x0 > left + 0.5 * em

    def paragraph_end(line: Line) -> bool:
        return not full(line) and not indented(line)

    for i, line in enumerate(lines):
        tokens = line.tokens
        prev = lines[i - 1] if i else None
        if full(line):
            if tokens[-1].lower() in SHORT_WORDS:
                issues.append(Issue("hanging-word", line.page, f"строка кончается на «{tokens[-1]}»: «…{' '.join(tokens[-3:])}»"))
            if tokens[-1].endswith("–") or re.search(r"\d-$", tokens[-1]):
                issues.append(Issue("broken-range", line.page, f"перенос внутри диапазона: «{tokens[-1]}»"))
            gaps = [b[1] - a[2] for a, b in zip(line.words, line.words[1:])]
            if gaps and max(gaps) > _WIDE_GAP_EM * em:
                wide.append((max(gaps) / em, line))
        if tokens[0] in ("—", "–") and not indented(line):
            issues.append(Issue("dash-at-start", line.page, f"строка начинается с тире: «{' '.join(tokens[:3])}»"))
        hyphens = hyphens + 1 if re.search(r"[А-Яа-яЁё]-$", tokens[-1]) else 0
        if hyphens == 3:
            issues.append(Issue("hyphen-run", line.page, f"три переноса подряд, последний: «{tokens[-1]}»"))
        if prev is not None and paragraph_end(line) and full(prev):
            if len(tokens) == 1 or line.x1 - line.x0 < 0.1 * measure:
                issues.append(Issue("runt", line.page, f"слишком короткая концовка абзаца: «{' '.join(tokens)}»"))
        if prev is not None and prev.page != line.page:
            if paragraph_end(line):
                issues.append(Issue("widow", line.page, f"страница начинается с концовки абзаца: «{' '.join(tokens[:4])}…»"))
            if indented(prev) and full(prev):
                issues.append(Issue("orphan", prev.page, f"страница кончается первой строкой абзаца: «{' '.join(prev.tokens[:4])}…»"))
    for ratio, line in sorted(wide, key=lambda w: -w[0])[:3]:
        issues.append(Issue("wide-gap", line.page, f"пробел {ratio:.2f} em в строке «{' '.join(line.tokens[:5])}…»"))
    return issues


def _check_fonts(pdf: Path, font: Optional[str]) -> List[Issue]:
    res = subprocess.run(["pdffonts", str(pdf)], capture_output=True, text=True, timeout=30)
    expected = (font or "").replace(" ", "").lower()
    allowed = ("courier", "cambriamath", "symbol")
    bad, not_embedded = set(), set()
    for row in res.stdout.splitlines()[2:]:
        parts = row.split()
        if len(parts) < 6:
            continue
        name = parts[0].split("+")[-1]
        if parts[-5] != "yes":
            not_embedded.add(name)
        low = name.lower()
        if expected and expected not in low and not any(a in low for a in allowed):
            bad.add(name)
    issues = []
    if bad:
        issues.append(Issue("font", 0, f"вместо {font}: {', '.join(sorted(bad))}"))
    if not_embedded:
        issues.append(Issue("font-not-embedded", 0, ", ".join(sorted(not_embedded))))
    return issues


def check_pdf(pdf: Union[str, Path], *, font: Optional[str] = None) -> List[Issue]:
    """Предупреждения по готовому PDF. Без poppler возвращает пустой список."""
    if not (shutil.which("pdftotext") and shutil.which("pdffonts")):
        return []
    pdf = Path(pdf)
    return _check_fonts(pdf, font) + check_lines(_read_lines(pdf))
