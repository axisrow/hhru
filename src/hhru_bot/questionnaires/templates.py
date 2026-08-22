"""Доменная модель шаблонов ответов на анкеты (#482).

Чистые данные и валидация: ни браузера, ни SQLite, ни LLM.

Терминология здесь — не изобретение этого модуля, а результат ручного разбора
117 реальных вопросов, собранных read-only прогоном ``probe
--questionnaires-only`` (#443/#456):

  * **кластер** — тематическая категория вопроса (9 штук). Нужен ровно для
    одного рабочего решения: кластер ``compliance`` (документы, гражданство,
    разрешение на работу) отвечается ТОЛЬКО явным сохранённым значением, см.
    ``STRICT_CLUSTERS`` и ``resolver.compliance_gate``.
  * **шаблон** — семантический ключ, сводящий разные формулировки одного и того
    же вопроса в одну запись. В собранных данных ``salary`` встретился ~17 раз,
    ``location`` — 4, ``desired_role`` — 3: без шаблонов бот отправлял бы один и
    тот же смысл в LLM заново на каждой вакансии.

Seed-поля (``SEED_TEMPLATES``) — те четыре, что перечислены в #482. Это
СТАРТОВЫЙ набор, а не закрытый список: пользователь заводит свои шаблоны
командой ``questionnaire set``, и они участвуют в сопоставлении наравне с
seed'ами через подтверждённые формулировки (``resolver.match_phrase``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..external_forms.detect import normalize

#: Кластеры из ручного разбора собранных анкет. Порядок стабилен (кортеж, не
#: set) — используется как ``choices`` в CLI и в ``--help``.
CLUSTERS: tuple[str, ...] = (
    "conditions",
    "motivation",
    "expertise",
    "assessment",
    "marketing",
    "portfolio",
    "fit",
    "compliance",
    "mixed",
)

#: Кластер по умолчанию для шаблона, чью тему оператор не назвал явно.
#: ``mixed`` намеренно НЕ входит в ``STRICT_CLUSTERS``: неизвестная тема не
#: делает вопрос комплаенс-вопросом, иначе любой пользовательский шаблон без
#: ``--cluster`` молча перестал бы отвечать.
DEFAULT_CLUSTER = "mixed"

#: Кластеры, где ответ разрешён ТОЛЬКО явным непустым static-значением (#482:
#: «Документы и комплаенс отвечаются только явным сохранённым значением»).
#: Здесь уверенная ошибка — самая дорогая: неверный ответ про гражданство или
#: судимость хуже пропущенной вакансии, поэтому ни keyword-догадка, ни LLM, ни
#: будущий ML-слот не имеют права заполнить такое поле.
STRICT_CLUSTERS: frozenset[str] = frozenset({"compliance"})

#: Признак комплаенс-вопроса ПО ТЕКСТУ, независимо от того, сопоставлен ли он с
#: каким-либо шаблоном. Кластерной проверки (``STRICT_CLUSTERS``) недостаточно:
#: она опирается на найденный шаблон, а вопрос про судимость или паспорт может
#: не совпасть ни с одним — и тогда, не будь этого паттерна, он ушёл бы в общую
#: LLM-ступень и был бы отвечён свободной генерацией. Именно там ошибка дороже
#: всего, поэтому распознавание темы не должно зависеть от успеха сопоставления.
#:
#: Список намеренно узкий и покрывает документы, правовой статус и допуски —
#: то, что нельзя ни угадать, ни сгенерировать. Ложное срабатывание безвредно
#: (вопрос уйдёт в очередь и будет решён человеком), пропуск — нет.
_COMPLIANCE_PATTERN = re.compile(
    r"паспорт|passport|снилс|инн\b|судимост|гражданств|разрешени\w*\s+на\s+работу|"
    r"вид\s+на\s+жительств|патент|воинск|призыв|медицинск\w*\s+книжк|медкнижк|"
    r"справк\w*\s+о\s+несудимости|допуск\w*\s+к\s+гостайне|секретност",
    re.IGNORECASE,
)

STATIC = "static"
CONTEXTUAL = "contextual"
#: Допустимые режимы шаблона. Кортеж — порядок стабилен для ``choices``.
MODES: tuple[str, ...] = (STATIC, CONTEXTUAL)


@dataclass(frozen=True)
class SeedTemplate:
    """Стартовый шаблон с ключевыми словами для keyword-стратегии.

    ``keywords`` хранятся УЖЕ нормализованными (``normalize``) — та же функция
    применяется к тексту вопроса при сопоставлении, поэтому регистр и лишние
    пробелы в разметке hh.ru не влияют на результат. Нормализация на этапе
    объявления, а не на каждом сравнении: список фиксированный, а сопоставление
    выполняется для каждого вопроса каждой вакансии.
    """

    name: str
    cluster: str
    keywords: tuple[str, ...]


def _seed(name: str, cluster: str, *keywords: str) -> SeedTemplate:
    return SeedTemplate(name, cluster, tuple(normalize(word) for word in keywords))


#: Seed-поля #482. Ключевые слова выведены из формулировок, реально
#: встречавшихся в собранных анкетах, и намеренно узкие: keyword-стратегия
#: fail-closed (см. ``resolver.match_keyword``), поэтому широкое слово, попавшее
#: сразу в два шаблона, не даёт ложный ответ — оно снимает сопоставление вовсе,
#: и вопрос уходит в очередь на ручное решение.
SEED_TEMPLATES: tuple[SeedTemplate, ...] = (
    _seed(
        "salary",
        "conditions",
        "зарплатные ожидания",
        "ожидания по доходу",
        "ожидания по зарплате",
        "желаемая зарплата",
        "желаемый доход",
        "уровень дохода",
        "уровень оплаты",
        "на какую зарплату",
        "сколько хотите зарабатывать",
    ),
    _seed(
        "location",
        "conditions",
        "город проживания",
        "в каком городе",
        "где вы находитесь",
        "страна проживания",
        "ваш город",
        "город и страна",
    ),
    _seed(
        "desired_role",
        "motivation",
        "желаемая должность",
        "желаемая роль",
        "какие задачи",
        "чем хотите заниматься",
        "интересующая позиция",
    ),
    _seed(
        "business_segments",
        "marketing",
        "сегменты бизнеса",
        "в каких сферах",
        "с какими нишами",
        "опыт в сегменте",
        "b2b или b2c",
    ),
)

_SEEDS_BY_NAME: dict[str, SeedTemplate] = {seed.name: seed for seed in SEED_TEMPLATES}


class TemplateError(ValueError):
    """Некорректное описание шаблона (режим без обязательного поля и т.п.)."""


@dataclass(frozen=True)
class QuestionTemplate:
    """Сохранённый шаблон: как отвечать на вопросы этого смысла.

    ``static`` — готовое значение, отдаётся как есть, LLM не вызывается вовсе.
    ``contextual`` — инструкция для LLM плюс подтверждённые примеры
    формулировок: ответ генерируется под конкретную вакансию, но в рамках
    заданной оператором инструкции.
    """

    name: str
    cluster: str = DEFAULT_CLUSTER
    mode: str = STATIC
    answer: str | None = None
    instruction: str | None = None
    examples: tuple[str, ...] = ()

    @property
    def is_static(self) -> bool:
        return self.mode == STATIC

    def validate(self) -> None:
        """Fail-closed проверка. Бросает ``TemplateError``.

        Вызывается и при записи через CLI, и при чтении из БД: строка могла
        быть заведена более ранней версией или отредактирована вручную (прямой
        доступ к ``history.db`` — штатный для проекта способ, см. CLAUDE.md).
        Шаблон без обязательного поля неисполним, и обнаружить это лучше до
        того, как pipeline дойдёт до формы отклика.
        """
        if not self.name.strip():
            raise TemplateError("имя шаблона не может быть пустым")
        if self.mode not in MODES:
            raise TemplateError(
                f"неизвестный режим шаблона: {self.mode!r} (допустимо: static, contextual)"
            )
        if self.cluster not in CLUSTERS:
            raise TemplateError(f"неизвестный кластер: {self.cluster!r}")
        if self.is_static and not (self.answer or "").strip():
            raise TemplateError(f"шаблон '{self.name}' в режиме static требует непустого ответа")
        if not self.is_static and not (self.instruction or "").strip():
            raise TemplateError(
                f"шаблон '{self.name}' в режиме contextual требует непустой инструкции"
            )


def seed_template(name: str) -> SeedTemplate | None:
    return _SEEDS_BY_NAME.get(name)


def cluster_for(name: str) -> str:
    """Кластер seed-шаблона; для пользовательского шаблона — ``mixed``."""
    seed = _SEEDS_BY_NAME.get(name)
    return seed.cluster if seed is not None else DEFAULT_CLUSTER


def is_strict(cluster: str) -> bool:
    return cluster in STRICT_CLUSTERS


def is_compliance_text(text: str) -> bool:
    """Комплаенс ли это по самому тексту вопроса, без опоры на шаблон."""
    return bool(_COMPLIANCE_PATTERN.search(text))
