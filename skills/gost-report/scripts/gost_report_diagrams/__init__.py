"""gost_report_diagrams — mermaid-диаграммы → PNG как подключаемый модуль.

Namespace `r.diagram` (callable): r.diagram(src, caption=...).
Рендер в PNG через каскад бэкендов: внешний `mmdc` (Node, точно) → `merm`
(pure-python, оффлайн) → mermaid.ink (сеть, opt-in). ГОСТ-тема (neutral +
grayscale + serif) инжектится автоматически.

Детерминизм — best-effort (random SVG id, метрики шрифта), см. research/19.
"""
