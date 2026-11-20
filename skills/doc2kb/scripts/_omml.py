"""
_omml.py — Office Math (OMML, `m:oMath` / `m:oMathPara`) → LaTeX.

PowerPoint (`a14:m`) and Word store equations as OMML. python-pptx exposes
none of it, so without this an equation's text vanished from the slide. The
mapping follows the OMML element set (ECMA-376 Part 1, §22.1); anything not
recognised is walked for its text, so content is never dropped.

    to_latex(element) -> str      # an m:oMath / m:oMathPara / any OMML subtree

Pure stdlib; works on lxml and xml.etree elements alike.
"""
from __future__ import annotations

import re
import unicodedata

M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"

_GREEK = {
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta", "ε": r"\varepsilon",
    "ϵ": r"\epsilon", "ζ": r"\zeta", "η": r"\eta", "θ": r"\theta", "ϑ": r"\vartheta",
    "ι": r"\iota", "κ": r"\kappa", "λ": r"\lambda", "μ": r"\mu", "ν": r"\nu", "ξ": r"\xi",
    "π": r"\pi", "ϖ": r"\varpi", "ρ": r"\rho", "ϱ": r"\varrho", "σ": r"\sigma",
    "ς": r"\varsigma", "τ": r"\tau", "υ": r"\upsilon", "φ": r"\varphi", "ϕ": r"\phi",
    "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega", "Γ": r"\Gamma", "Δ": r"\Delta",
    "∆": r"\Delta", "Θ": r"\Theta", "Λ": r"\Lambda", "Ξ": r"\Xi", "Π": r"\Pi",
    "Σ": r"\Sigma", "Υ": r"\Upsilon", "Φ": r"\Phi", "Ψ": r"\Psi", "Ω": r"\Omega",
}
_SYMBOLS = {
    "−": "-", "∓": r"\mp", "±": r"\pm", "×": r"\times", "÷": r"\div", "·": r"\cdot",
    "⋅": r"\cdot", "∗": "*", "≤": r"\le", "⩽": r"\le", "≥": r"\ge", "⩾": r"\ge",
    "≠": r"\ne", "≈": r"\approx", "≡": r"\equiv", "∼": r"\sim", "≃": r"\simeq",
    "≅": r"\cong", "∝": r"\propto", "≪": r"\ll", "≫": r"\gg", "∞": r"\infty",
    "∂": r"\partial", "∇": r"\nabla", "∈": r"\in", "∉": r"\notin", "∋": r"\ni",
    "⊂": r"\subset", "⊃": r"\supset", "⊆": r"\subseteq", "⊇": r"\supseteq",
    "∪": r"\cup", "∩": r"\cap", "∅": r"\emptyset", "∀": r"\forall", "∃": r"\exists",
    "¬": r"\neg", "∧": r"\wedge", "∨": r"\vee", "→": r"\to", "←": r"\leftarrow",
    "↔": r"\leftrightarrow", "⇒": r"\Rightarrow", "⇐": r"\Leftarrow",
    "⇔": r"\Leftrightarrow", "↦": r"\mapsto", "…": r"\dots", "⋯": r"\cdots",
    "⋮": r"\vdots", "⋱": r"\ddots", "′": "'", "″": "''", "∘": r"\circ", "°": r"^{\circ}",
    "⊕": r"\oplus", "⊗": r"\otimes", "∑": r"\sum", "∏": r"\prod", "∫": r"\int",
    "√": r"\surd", "ℝ": r"\mathbb{R}", "ℕ": r"\mathbb{N}", "ℤ": r"\mathbb{Z}",
    "ℚ": r"\mathbb{Q}", "ℂ": r"\mathbb{C}", "⟨": r"\langle", "⟩": r"\rangle",
    "‖": r"\|", "∣": "|", "⌊": r"\lfloor", "⌋": r"\rfloor", "⌈": r"\lceil",
    "⌉": r"\rceil", "ℓ": r"\ell", "ℏ": r"\hbar", "∠": r"\angle", "⊥": r"\perp",
    "∥": r"\parallel", "{": r"\{", "}": r"\}", "%": r"\%", "#": r"\#", "&": r"\&",
    "$": r"\$", "_": r"\_", "\\": r"\backslash",
}
_NARY = {
    "∑": r"\sum", "∏": r"\prod", "∐": r"\coprod", "∫": r"\int", "∬": r"\iint",
    "∭": r"\iiint", "∮": r"\oint", "⋃": r"\bigcup", "⋂": r"\bigcap",
    "⋁": r"\bigvee", "⋀": r"\bigwedge", "⨁": r"\bigoplus", "⨂": r"\bigotimes",
}
_ACCENTS = {
    "̂": r"\hat", "^": r"\hat", "̃": r"\tilde", "~": r"\tilde",
    "̇": r"\dot", "̈": r"\ddot", "⃛": r"\dddot", "̅": r"\bar",
    "¯": r"\bar", "̄": r"\bar", "⃗": r"\vec", "→": r"\vec",
    "̀": r"\grave", "́": r"\acute", "̆": r"\breve", "̌": r"\check",
}
_DELIMS = {
    "": ".", "{": r"\{", "}": r"\}", "⟨": r"\langle", "⟩": r"\rangle", "〈": r"\langle",
    "〉": r"\rangle", "‖": r"\|", "⌊": r"\lfloor", "⌋": r"\rfloor", "⌈": r"\lceil",
    "⌉": r"\rceil", "|": "|", "[": "[", "]": "]", "(": "(", ")": ")",
}
_FUNCS = {"sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan",
          "sinh", "cosh", "tanh", "coth", "log", "ln", "lg", "exp", "lim", "max", "min",
          "sup", "inf", "arg", "det", "dim", "ker", "deg", "gcd", "Pr"}
_CTRL_END = re.compile(r"\\[A-Za-z]+$")


def _tag(el) -> str:
    t = el.tag if isinstance(el.tag, str) else ""
    return t.rsplit("}", 1)[-1] if t.startswith("{" + M_NS + "}") else ""


def _child(el, name: str):
    for c in el:
        if _tag(c) == name:
            return c
    return None


def _children(el, name: str) -> list:
    return [c for c in el if _tag(c) == name]


def _prop(el, prop: str, name: str, default: str | None = None) -> str | None:
    """m:val of <m:{prop}><m:{name} m:val=…/></m:{prop}>; `default` when absent,
    "" when present without a value."""
    pr = _child(el, prop)
    node = _child(pr, name) if pr is not None else None
    if node is None:
        return default
    return node.get(f"{{{M_NS}}}val", node.get("val", ""))


def _on(value: str | None) -> bool:
    return (value or "").lower() in ("1", "on", "true")


def _join(parts: list[str]) -> str:
    out = ""
    for p in parts:
        if not p:
            continue
        if out and _CTRL_END.search(out) and p[0].isalpha():
            out += " "
        out += p
    return out


def _group(s: str) -> str:
    return s if len(s) == 1 else "{" + s + "}"


def _char(ch: str) -> str:
    if 0x1D400 <= ord(ch) <= 0x1D7FF or ch == "ℎ":  # math alphanumerics, ℎ
        ch = unicodedata.normalize("NFKC", ch)
    if ch in _GREEK:
        return _GREEK[ch]
    if ch in _SYMBOLS:
        return _SYMBOLS[ch]
    if ch in (" ", " ", " ", " ", " ", " ", " "):
        return " "
    return ch


def _run_text(r) -> str:
    text = "".join(t.text or "" for t in r if _tag(t) == "t")
    parts = [_char(c) for c in text]
    if len(text) > 1 and text.isalpha() and text in _FUNCS:
        return "\\" + text
    return _join(parts)


def _arg(el, name: str) -> str:
    c = _child(el, name)
    return _conv(c) if c is not None else ""


def _delim(ch: str | None) -> str:
    ch = "" if ch is None else ch
    return _DELIMS.get(ch, ch)


def _conv(el) -> str:
    name = _tag(el)
    if name == "r":
        return _run_text(el)
    if name in ("oMath", "e", "num", "den", "sub", "sup", "deg", "lim", "fName", "box",
                "phant", "argPr") or name == "":
        return _join([_conv(c) for c in el if not _tag(c).endswith("Pr")])
    if name == "oMathPara":
        return r" \\ ".join(_conv(c) for c in _children(el, "oMath"))
    if name == "f":
        num, den = _arg(el, "num"), _arg(el, "den")
        kind = _prop(el, "fPr", "type", "bar")
        if kind in ("lin", "skw"):
            return f"{_group(num)}/{_group(den)}"
        if kind == "noBar":
            return rf"\genfrac{{}}{{}}{{0pt}}{{}}{{{num}}}{{{den}}}"
        return rf"\frac{{{num}}}{{{den}}}"
    if name == "sSub":
        return f"{_arg(el, 'e')}_{_group(_arg(el, 'sub'))}"
    if name == "sSup":
        return f"{_arg(el, 'e')}^{_group(_arg(el, 'sup'))}"
    if name == "sSubSup":
        return f"{_arg(el, 'e')}_{_group(_arg(el, 'sub'))}^{_group(_arg(el, 'sup'))}"
    if name == "sPre":
        return f"{{}}_{_group(_arg(el, 'sub'))}^{_group(_arg(el, 'sup'))}{_arg(el, 'e')}"
    if name == "rad":
        deg = _arg(el, "deg")
        if _on(_prop(el, "radPr", "degHide")) or not deg:
            return rf"\sqrt{{{_arg(el, 'e')}}}"
        return rf"\sqrt[{deg}]{{{_arg(el, 'e')}}}"
    if name == "d":
        beg = _delim(_prop(el, "dPr", "begChr", "("))
        end = _delim(_prop(el, "dPr", "endChr", ")"))
        sep = _delim(_prop(el, "dPr", "sepChr", "|"))
        inner = sep.join(_conv(e) for e in _children(el, "e"))
        return rf"\left{beg}{inner}\right{end}"
    if name == "nary":
        op = _prop(el, "naryPr", "chr", "∫")
        op = _NARY.get(op, _char(op) if op else r"\int")
        sub = "" if _on(_prop(el, "naryPr", "subHide")) else _arg(el, "sub")
        sup = "" if _on(_prop(el, "naryPr", "supHide")) else _arg(el, "sup")
        lims = (f"_{_group(sub)}" if sub else "") + (f"^{_group(sup)}" if sup else "")
        return _join([op + lims, " ", _arg(el, "e")])
    if name == "func":
        fn = _arg(el, "fName")
        body = _arg(el, "e")
        return _join([fn, "" if body[:1] in ("\\", "(", "{") else " ", body])
    if name in ("limLow", "limUpp"):
        base, lim = _arg(el, "e"), _arg(el, "lim")
        if name == "limLow" and _CTRL_END.fullmatch(base or "") and base[1:] in _FUNCS:
            return f"{base}_{_group(lim)}"
        return (rf"\underset{{{lim}}}{{{base}}}" if name == "limLow"
                else rf"\overset{{{lim}}}{{{base}}}")
    if name == "acc":
        ch = _prop(el, "accPr", "chr", "̂") or "̂"
        cmd = _ACCENTS.get(ch)
        base = _arg(el, "e")
        return rf"{cmd}{{{base}}}" if cmd else rf"\overset{{{_char(ch)}}}{{{base}}}"
    if name == "bar":
        top = (_prop(el, "barPr", "pos", "bot") or "bot") == "top"
        return rf"\overline{{{_arg(el, 'e')}}}" if top else rf"\underline{{{_arg(el, 'e')}}}"
    if name == "groupChr":
        ch = _prop(el, "groupChrPr", "chr", "⏟") or "⏟"
        top = (_prop(el, "groupChrPr", "pos", "bot") or "bot") == "top"
        base = _arg(el, "e")
        if ch in ("⏟", "⏞"):
            return rf"\underbrace{{{base}}}" if ch == "⏟" else rf"\overbrace{{{base}}}"
        return (rf"\overset{{{_char(ch)}}}{{{base}}}" if top
                else rf"\underset{{{_char(ch)}}}{{{base}}}")
    if name == "borderBox":
        return rf"\boxed{{{_arg(el, 'e')}}}"
    if name == "m":
        rows = [" & ".join(_conv(e) for e in _children(mr, "e")) for mr in _children(el, "mr")]
        return r"\begin{matrix}" + r" \\ ".join(rows) + r"\end{matrix}"
    if name == "eqArr":
        rows = [_conv(e) for e in _children(el, "e")]
        return r"\begin{array}{l}" + r" \\ ".join(rows) + r"\end{array}"
    if name.endswith("Pr") or name == "ctrlPr":
        return ""
    return _join([_conv(c) for c in el])  # unknown: keep its text


def to_latex(el) -> str:
    """LaTeX for an OMML element (m:oMath, m:oMathPara or any m:* subtree)."""
    return re.sub(r"[ \t]{2,}", " ", _conv(el)).strip()


def math_elements(el) -> list:
    """Top-level m:oMathPara / m:oMath elements inside `el` (not nested ones)."""
    out = []

    def walk(node):
        for c in node:
            if _tag(c) in ("oMathPara", "oMath"):
                out.append(c)
            else:
                walk(c)
    walk(el)
    return out
