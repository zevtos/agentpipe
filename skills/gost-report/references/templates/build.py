"""Скелет build-скрипта для отчёта по ГОСТ. Положи в <project>/.claude/gost-report/build.py.

Запуск:
    python3 ~/.claude/skills/gost-report/scripts/ensure_env.py .claude/gost-report/build.py

Конвенции (резолвятся автоматически из <project>/.claude/gost-report/build.py):
    figures      — <project>/docs/figures/
    tables       — <project>/docs/tables/
    output .docx — <project>/docs/report.docx

Project root детектится обходом вверх до первого маркера: .git → Makefile → pyproject.toml → .claude.
"""
from gost_report import Report, TitleConfig, paths

p = paths()  # доступно если нужны явные пути; для базовых сценариев не требуется

r = Report(TitleConfig(
    work_type="Лабораторная работа",
    work_number="№N",
    topic="Тема работы",
    student_name="Фамилия И.О.",
    student_group="P3XXX",
    teacher_name="Фамилия И.О.",
    teacher_label="Проверил",  # женщине: "Проверила"; ВКР/курсовая: "Руководитель"
    teacher_degree="к.т.н.",
    teacher_position="доцент",
    year="2026",
))

r.toc()

r.h1("Введение")
r.text("Цель работы.")

r.h1("Выполнение работы")
r.task("Задание 1.")
r.code("команда")
r.figure("schema.png", "Схема, относительный путь резолвится от docs/figures/")

r.h1("Заключение")
r.numbered(["Результат 1.", "Результат 2."])

out = r.save()  # без аргумента → <project>/docs/report.docx; mkdir parents автоматический
print(f"Wrote {out}")
