"""Скелет build-скрипта для отчёта по ГОСТ. Положи в <project>/.gost-report/build.py.

Запуск (проще всего — через лаунчер `gr` из cwd проекта):
    gr                       # найдёт .gost-report/build.py вверх по дереву

Конвенции (резолвятся автоматически из <project>/.gost-report/build.py):
    figures      — <project>/docs/figures/
    tables       — <project>/docs/tables/
    output .docx — <project>/docs/report.docx

Project root детектится обходом вверх до первого маркера:
.git → Makefile → pyproject.toml → .gost-report → .claude.

ФИО/группа/преподаватель/год берутся из ~/.config/gost-report/config (либо
из переменных окружения GOST_REPORT_*). Если хочется захардкодить — передай
явно в TitleConfig(...); env побеждает только когда значение в env непустое.
"""
from gost_report import Report, TitleConfig, paths

p = paths()  # доступно если нужны явные пути; для базовых сценариев не требуется

# Минимальный вариант: всё про работу — здесь, всё про автора — в ~/.config/gost-report/config.
r = Report(TitleConfig(
    work_type="Лабораторная работа",
    work_number="№N",
    topic="Тема работы",
    # student_name / student_group / teacher_* / year — из env
))

# Командная работа: укажи всех участников.
# r = Report(TitleConfig(
#     work_type="Лабораторная работа",
#     work_number="№N",
#     topic="Тема работы",
#     student_names=["Иванов И.И.", "Петров П.П.", "Сидоров С.С."],
#     # student_group берётся из env (общая группа)
# ))

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
