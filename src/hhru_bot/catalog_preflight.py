"""Pre-flight сверка профессии с live-каталогом ДО мутирующего клика (#950).

Боевой кейс 2026-09-03 («Хирург»): имена автоподсказок title-поля визарда и
имена листов дерева — разные каталоги, и CLI узнавал об этом только после
сожжённого боевого прогона. Модуль держит чистую сверку «запрос -> листы» и
read-only browser-обёртку для create-resume: фильтр каталога поиска вакансий
открывается и читается ДО входа в визард (id-пространство дерева модалки
совпадает с каталогом поиска вакансий, #913), поэтому отказ «листа нет в
каталоге» наступает до первого мутирующего клика и перечисляет листы, которые
фильтр реально предлагает (принцип #836). Источник истины — live-каталог;
полный офлайн-справочник собирает `hhru professional-roles --refresh`
(кэш — только для диагностики/поиска, не для решения о записи).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from playwright.sync_api import Page

from .create_resume import OTHER_ROLE_LABEL
from .external_forms.detect import normalize
from .professional_roles import search_professional_roles

# #920 (боевой прогон «Логопед» 2026-09-02): фильтр дерева hh.ru НЕСТАБИЛЕН —
# на один и тот же запрос боевой прогон получал ровно «Другое», а повтор тем же
# кодом — точный лист. Пустой/вырожденный ответ фильтра переспрашивается один
# раз (полный повтор fill + опроса), прежде чем стать отказом; при стабильном
# отсутствии профессии повтор возвращает тот же результат, fail-closed не
# ослабляется.
_AREA_FILTER_ATTEMPTS = 2


@dataclass(frozen=True)
class LeafEvaluation:
    """Результат сверки одного запроса с перечнем листов live-каталога.

    ``exact`` — есть нормализованное точное совпадение; ``candidates`` —
    ближайшие листы для отказа, ведущего к цели (перезапуск с первого раза
    по реальному имени листа): и листы, содержащие запрос («Плотник» ->
    «Столяр, плотник», направление гарда #920), и листы, содержащиеся в
    запросе («Врач-хирург» -> «Врач», боевой кейс #950 — узкое имя есть в
    подсказках, а в дереве только общее). Плейсхолдер «Другое» кандидатом
    никогда не является — это вырожденный catch-all, выбор которого решается
    явным флагом вызывающей команды, а не подбором.
    """

    value: str
    exact: bool
    candidates: tuple[str, ...]


def evaluate_leaf(value: str, available: Iterable[str]) -> LeafEvaluation:
    """Сверить запрос с перечнем листов: точное совпадение + подстрока-кандидаты."""
    unique = list(dict.fromkeys(label.strip() for label in available if label.strip()))
    query = normalize(value)
    exact = bool(query) and any(normalize(label) == query for label in unique)
    candidates = tuple(
        label
        for label in unique
        if query
        and normalize(label) != normalize(OTHER_ROLE_LABEL)
        and (query in normalize(label) or normalize(label) in query)
    )
    return LeafEvaluation(value=value, exact=exact, candidates=candidates)


def format_candidates(evaluation: LeafEvaluation) -> str:
    """Человекочитаемый перечень альтернатив для отказа по листу."""
    if evaluation.candidates:
        return "ближайшие доступные листы: " + "; ".join(evaluation.candidates)
    return "совпадений по подстроке не найдено"


@dataclass(frozen=True)
class PreflightOutcome:
    """Итог pre-flight сверки: проход (возможно с предупреждением) или отказ."""

    ok: bool
    message: str


def preflight_area(
    page: Page, area: str, *, allow_unresolved_area: bool = False
) -> PreflightOutcome:
    """Read-only сверка ``--area`` с live-каталогом поиска вакансий до визарда.

    Открывает фильтр профессий ``/search/vacancy`` (goto + ввод в поисковую
    строку модалки, ничего не выбирается и не сохраняется — граница
    «Чтение состояния») и проверяет, что фильтр по ``area`` отвечает
    точным листом. Отказ перечисляет предложенные листы; вырождение фильтра
    в пустоту/«Другое» переспрашивается (:data:`_AREA_FILTER_ATTEMPTS`).
    ``allow_unresolved_area`` не отменяет чтение: при отсутствии листа это
    проход с предупреждением, а не молчаливое согласие.
    """
    evaluation = LeafEvaluation(value=area, exact=False, candidates=())
    offered: list[str] = []
    for _ in range(_AREA_FILTER_ATTEMPTS):
        roles = search_professional_roles(page, [area])
        evaluation = evaluate_leaf(area, (role.label for role in roles))
        if evaluation.exact:
            return PreflightOutcome(True, "")
        offered = [role.label for role in roles if role.label != OTHER_ROLE_LABEL]
        if offered:
            break
        # Фильтр не вернул содержательных листов (пусто или только «Другое») —
        # известная нестабильность #920; переспрашиваем, при повторе — отказ.
    message = (
        f"профессия «{area}» не найдена в live-каталоге поиска вакансий "
        f"(сверка до входа в визард, #950); {format_candidates(evaluation)}. "
        'Полный справочник: hhru professional-roles --refresh, затем --query "подстрока".'
    )
    if allow_unresolved_area:
        return PreflightOutcome(True, f"{message} Будет выбрана роль-плейсхолдер «Другое» (id 40).")
    return PreflightOutcome(
        False,
        f"{message} Повторите с точным именем листа или используйте --allow-unresolved-area.",
    )
