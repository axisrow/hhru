# Похожие проекты (для идей)

Источник идей, не код для копирования — смотреть, а не переиспользовать.

> 15 декабря 2025 hh.ru закрыл соискательский API — отклик и работа с
> резюме для сторонних приложений отключены, остался поиск вакансий и
> `GET /me`. Все живые проекты 2026 года, как и наш, работают через
> браузер (Playwright), а не через API.

### Браузерные боты (наш стек — Playwright/Selenium)

- [Steev193/hh-ru-apply](https://github.com/Steev193/hh-ru-apply) — Node +
  Playwright, MIT. Вынесенные селекторы + codegen для их обновления.
- [tgeruzov/hh-auto-responder](https://github.com/tgeruzov/hh-auto-responder)
  — Tampermonkey userscript. Роутинг трёх исходов отклика.
- [YAMAKAYAMACO/hh-autoresponder](https://github.com/YAMAKAYAMACO/hh-autoresponder)
  — Python + Playwright + SQLite, ближе всего к нашей архитектуре.
- [fikstt2/hh-ai-agent](https://github.com/fikstt2/hh-ai-agent) — Python +
  Playwright. Персистентная сессия: вход руками один раз, потом headless.
- [semernyakov/hh-auto-apply](https://github.com/semernyakov/hh-auto-apply) —
  Python + Playwright, MIT. Парсер cooldown поднятия резюме из текста hh.ru.
- [beatwad/XX_Auto_Jobs_Applier](https://github.com/beatwad/XX_Auto_Jobs_Applier)
  — Python + Playwright, MIT. Config-driven (YAML), капча через Telegram.
- [s3rgeym/hh-applicant-tool](https://github.com/s3rgeym/hh-applicant-tool) —
  Python, API + Playwright, 500+ звёзд. Эталон по шаблонам писем, схеме
  SQLite и троттлингу. README запрещает коммерческое использование —
  только как референс, код не брать.

- [konard/hh-job-application-automation](https://github.com/konard/hh-job-application-automation)
  — Bun, Playwright и Puppeteer, Unlicense. Q&A-файл с нечётким матчем
  (Левенштейн + keyword overlap) для тест-вопросов формы.

### Поднятие резюме

- [Vlad9572324/hh.ru-clicker](https://github.com/Vlad9572324/hh.ru-clicker) —
  bump через API `/applicant/resumes/touch` (проверять актуальность после
  дек. 2025).
- [rycln/hhraiser](https://github.com/rycln/hhraiser) — Go. Джиттер
  расписания против антифрода.

### Адаптация и хранение резюме

Отдельный трек (issue [#671](https://github.com/axisrow/hhru/issues/671)) от
ботов-автооткликов выше: у нас резюме — внешний артефакт на hh.ru, эти
проекты работают с резюме как с локальными данными (JSON/PDF/веб-форма).
Разобран по коду один прошедший фильтр по лицензии и активности кандидат
из восьми рассмотренных — остальные отсеяны, причины ниже.

- [srbhr/Resume-Matcher](https://github.com/srbhr/Resume-Matcher) — Python
  (FastAPI) + TypeScript, Apache-2.0, ревизия
  `116f9cc3b00e1ac91734a6c2679bf41ea64a0edc` (2026-08-11). Единственный
  кандидат про адаптацию резюме под конкретную вакансию — задачу, которую
  наш проект пока не решает. Заимствуемая идея не код, а два механизма:
  - `apps/backend/app/services/improver.py` двигает содержимое резюме под
    вакансию через diff-патчи по regex-whitelist путей
    (`_ALLOWED_PATH_PATTERNS` — только `summary`, `description`-поля и
    списки навыков/языков/сертификатов) и явный blocklist полей
    (`_BLOCKED_FIELD_NAMES` — `company`, `institution`, `title`, `years` и
    т.п. трогать нельзя); LLM физически не может переписать факты вроде
    места работы или диплома, только формулировки.
  - `apps/backend/app/services/refiner.py::validate_master_alignment` —
    постфактум-проверка, что ни один навык, сертификат или работодатель в
    адаптированной версии не появился «из воздуха»: сверяет их с исходным
    («мастер») резюме и репортит `fabricated_skill`/`fabricated_cert`/
    `fabricated_company` как critical-нарушения.

  У нас похожий принцип уже есть с другой стороны — не пост-проверкой
  LLM-вывода, а входным гейтом до генерации (`STRICT_CLUSTERS` +
  `templates.is_compliance_text` в пакете `questionnaires/`, см.
  «Ключевые архитектурные решения» в `CLAUDE.md`). Whitelist путей на
  запись и alignment-проверка Resume-Matcher — предметный пример
  того же принципа «уверенная ошибка здесь необратима», применённый к
  тексту резюме, а не к ответам на анкеты; полезно как референс, если
  будет отдельная задача на адаптацию резюме под вакансию.

Отсеяны без разбора по коду (см. issue #671 для метрик):

- **Reactive-Resume**, **OpenResume** — веб-конструкторы резюме с нуля.
  У нас резюме уже существует на hh.ru и правится по DOM; локальный
  конструктор — не наша задача. Дополнительно у OpenResume лицензия
  AGPL-3.0 (портирование кода закрыто) и последний push — октябрь 2024.
- **RenderCV**, **sb2nov/resume**, **McDowell CV** — генерация
  предсказуемого PDF/LaTeX резюме как локального файла. Наш артефакт живёт
  в интерфейсе hh.ru, а не в файле, который мы рендерим сами — вне скоупа.
- **JSON Resume CLI** (`jsonresume/resume-cli`) — репозиторий
  архивирован; сама структура данных как формат остаётся жизнеспособной
  идеей, но брать нечего — код не развивается.
- **YAMLResume** — та же задача структурированного хранения резюме, что
  уже закрыта внутри проекта своими средствами (`CandidateFacts` в
  конфиге, issue #751), без заимствования у reference-проекта.

### Изучены точечно (отдельные селекторы, не полный аудит)

Разобраны по конкретным находкам (`docs/research/reference-selector-diff-audit.md`),
не по всему функционалу — сравнивать их с остальными в таблице фич нечестно.

- [Vadtop/hh-mcp-server](https://github.com/Vadtop/hh-mcp-server) — Python +
  Playwright, MIT.
- [AgentShekel/hh-bot](https://github.com/AgentShekel/hh-bot) — Python +
  Playwright, лицензия не подтверждена (NOASSERTION).
- [RumyantsevQa/hh-ai-auto-apply-assistant](https://github.com/RumyantsevQa/hh-ai-auto-apply-assistant)
  — Python, MIT.
- [kavotavochavo1-ctrl/hh-ai-job-bot](https://github.com/kavotavochavo1-ctrl/hh-ai-job-bot)
  — Python + Playwright, MIT.
- [lil-zon/hh-auto-apply](https://github.com/lil-zon/hh-auto-apply) — Python,
  без LICENSE.

### Обход DDoS-Guard / антидетект браузера

- [Kaliiiiiiiiii-Vinyzu/patchright-python](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)
  — drop-in замена Playwright, чинит Playwright-детект.
- [daijro/camoufox](https://github.com/daijro/camoufox) — антидетект-форк
  Firefox (автор предупреждает: не для стабильного прода).
- [ultrafunkamsterdam/nodriver](https://github.com/ultrafunkamsterdam/nodriver)
  / [cdpdriver/zendriver](https://github.com/cdpdriver/zendriver) — сильнее
  всех против DDoS-Guard, но не Playwright.

Гарантий обхода DDoS-Guard в headless нет; капчу приходится проходить руками
— это уже частично делает `login`.
