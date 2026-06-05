"""gost_report_diagrams — диаграммы через Graphviz (DOT) → PNG.

Namespace r.diagram (callable): r.diagram(dot_source, caption=...).
Покрывает структурные схемы, блок-схемы алгоритмов (ГОСТ 19.701), деревья, ER,
графы зависимостей. Красивое ГОСТ-оформление инжектится из коробки.

Зависимость — только системный бинарь `dot` (brew/apt install graphviz):
без pip, без Node, PNG напрямую. Детерминизм — best-effort (версия dot).
"""
