"""gost_report_viz — matplotlib-графики по ГОСТ как подключаемый модуль.

Namespace `r.plot`: line / scatter / bar / grouped_bar / histogram.
Каждый хелпер либо встраивает рисунок (если передан caption) и возвращает его
номер, либо возвращает Figure-объект для ручного r.figure(chart, caption).

Тяжёлые импорты (matplotlib/numpy) — лениво, внутри render()/check_available().
"""
