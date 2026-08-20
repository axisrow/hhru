"""Characterization-тесты config.py: load_config и дата-классы.

Поведение парсинга не должно измениться после ввода config_sections/.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from hhru_bot.config import (
    ConfigError,
    ResumeConfig,
    SearchFilters,
    is_resume_url_placeholder,
    load_config,
)
from hhru_bot.config_sections.ai import parse_ai
from hhru_bot.config_sections.ai_profile import AIProfile

pytestmark = pytest.mark.unit


def _write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _minimal_config() -> str:
    return """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AAA111"
            search:
              text: "python developer"
    """


def test_load_config_minimal(tmp_path):
    path = _write_config(tmp_path, _minimal_config())
    config = load_config(path)
    assert len(config.resumes) == 1
    resume = config.resumes[0]
    assert resume.id == "r1"
    assert resume.search.text == "python developer"
    # resume_id вычисляется из хвоста resume_url
    assert resume.resume_id == "AAA111"
    # дефолты throttle
    assert config.throttle.daily_apply_limit == 40
    assert config.throttle.min_delay_seconds == 8


@pytest.mark.parametrize(
    "resume_url",
    [
        "https://hh.ru/resume/XXXXXXXXXXXXXXXXXXXXXXXX",
        "https://hh.ru/resume/YYYYYYYYYYYYYYYYYYYYYYYY",
        "/resume/XXXXXXXXXXXXXXXXXXXXXXXX/",
    ],
)
def test_is_resume_url_placeholder(resume_url):
    assert is_resume_url_placeholder(resume_url)


@pytest.mark.parametrize(
    "resume_url",
    [
        "https://hh.ru/resume/12345678",
        "https://hh.ru/resume/ABC123",
        "https://hh.ru/resume/XYXYXYXY",
    ],
)
def test_real_resume_url_is_not_placeholder(resume_url):
    assert not is_resume_url_placeholder(resume_url)


def test_load_config_full_search_filters(tmp_path):
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/BBB222"
            search:
              text: "data analyst"
              area: 1
              salary_from: 200000
              experience: "between3And6"
              schedule: "remote"
              exclude_employers: ["BadCorp"]
              exclude_keywords: ["1С"]
    """,
    )
    config = load_config(path)
    search: SearchFilters = config.resumes[0].search
    assert search.area == 1
    assert search.salary_from == 200000
    assert search.experience == "between3And6"
    assert search.schedule == "remote"
    assert search.exclude_employers == ["BadCorp"]
    assert search.exclude_keywords == ["1С"]


def test_load_config_cover_letter_default_fallback(tmp_path):
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        cover_letter_default: "Default letter for {vacancy_title}"
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/CCC333"
            search:
              text: "x"
    """,
    )
    config = load_config(path)
    resume: ResumeConfig = config.resumes[0]
    assert config.cover_letter_for(resume) == "Default letter for {vacancy_title}"


def test_load_config_cover_letter_override(tmp_path):
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/DDD444"
            cover_letter: "Custom for {company_name}"
            search:
              text: "x"
    """,
    )
    config = load_config(path)
    resume = config.resumes[0]
    assert config.cover_letter_for(resume) == "Custom for {company_name}"


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="не найден"):
        load_config(tmp_path / "nope.yaml")


def test_load_config_missing_required_field(tmp_path):
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/EEE555"
            # search отсутствует — обязательно
    """,
    )
    with pytest.raises(ConfigError, match="search"):
        load_config(path)


def test_load_config_duplicate_resume_id(tmp_path):
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: dup
            resume_url: "https://hh.ru/resume/X1"
            search:
              text: "a"
          - id: dup
            resume_url: "https://hh.ru/resume/X2"
            search:
              text: "b"
    """,
    )
    with pytest.raises(ConfigError, match="Дублирующийся"):
        load_config(path)


def test_get_resume_not_found(tmp_path):
    config = load_config(_write_config(tmp_path, _minimal_config()))
    with pytest.raises(ConfigError, match="не найдено"):
        config.get_resume("nope")


def test_load_config_account_user_agent_default_none(tmp_path):
    # user_agent не задан → None → браузер использует родной UA Playwright (#9).
    config = load_config(_write_config(tmp_path, _minimal_config()))
    assert config.user_agent is None


def test_load_config_account_user_agent_explicit(tmp_path):
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
          user_agent: "Mozilla/5.0 (X11; Linux x86_64) Chrome/999.0 Safari/537.36"
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/FFF666"
            search:
              text: "x"
        """,
    )
    config = load_config(path)
    assert config.user_agent == ("Mozilla/5.0 (X11; Linux x86_64) Chrome/999.0 Safari/537.36")


def test_load_config_account_user_agent_wrong_type(tmp_path):
    # user_agent не-строка → ConfigError (контракт валидации типа, #9).
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
          user_agent: 123
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/GGG777"
            search:
              text: "x"
        """,
    )
    with pytest.raises(ConfigError, match="user_agent"):
        load_config(path)


def test_load_config_resumes_explicit_falsy_non_list_raises(tmp_path):
    # cycle-review PR #322: raw.get("resumes") or [] раньше маскировал явное
    # resumes: 0 (falsy, но не отсутствующее значение) под «раздела нет» и
    # тихо давал пустой список вместо ошибки валидации типа (#320 разрешил
    # отсутствие/пустой список как валидный overlay, но НЕ произвольный
    # falsy-но-не-list тип).
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes: 0
        """,
    )
    with pytest.raises(ConfigError, match="resumes"):
        load_config(path)


def test_load_config_resumes_missing_key_is_valid_empty_overlay(tmp_path):
    # #320: отсутствие раздела resumes — валидный пустой overlay, НЕ ошибка.
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        """,
    )
    config = load_config(path)
    assert config.resumes == []


def test_load_config_resumes_explicit_null_is_valid_empty_overlay(tmp_path):
    # resumes: (пустое значение, YAML null) — тоже валидный пустой overlay,
    # отличается от resumes: 0/false, которые являются ошибкой типа.
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
        """,
    )
    config = load_config(path)
    assert config.resumes == []


def _shipped_storage_state_path() -> str:
    """Берёт storage_state_file прямо из config.example.yaml — shipped контракт."""
    import yaml

    repo_root = Path(__file__).resolve().parents[1]
    example = repo_root / "config" / "config.example.yaml"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    return raw["account"]["storage_state_file"]


def _is_gitignored_repo(path: Path, repo: Path) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", str(path)],
            check=False,
        ).returncode
        == 0
    )


def _repo_copy_with_real_gitignore(tmp_path: Path) -> Path:
    """Пустой git-репо с РЕАЛЬНЫМ .gitignore проекта (не выдуманный mock).

    Инварианты безопасности проверяем против того файла, который реально лежит в
    репозитории — иначе тест зелёный, а секрет коммитится.
    """
    repo_root = Path(__file__).resolve().parents[1]
    repo_copy = tmp_path / "repo"
    repo_copy.mkdir()
    subprocess.run(["git", "-C", str(repo_copy), "init", "-q"], check=True)
    shutil.copyfile(repo_root / ".gitignore", repo_copy / ".gitignore")
    return repo_copy


def _write_config_in_data(repo_copy: Path, storage_state: str, resume_id: str) -> Path:
    cfg = repo_copy / "data" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        textwrap.dedent(
            f"""
            account:
              storage_state_file: {storage_state}
            resumes:
              - id: r1
                resume_url: "https://hh.ru/resume/{resume_id}"
                search:
                  text: x
            """
        ),
        encoding="utf-8",
    )
    return cfg


def test_session_secret_is_gitignored_when_config_in_repo(tmp_path):
    # Инвариант безопасности (#23 review): shipped storage_state_file из
    # config.example.yaml должен резолвиться (относительно директории конфига)
    # в путь, покрытый .gitignore. Иначе login (auth.py) запишет cookies/
    # localStorage сессии hh.ru в НЕ-ignored файл → account takeover при коммите.
    #
    # После #133 конфиг живёт в data/config.yaml, а shipped-значение —
    # storage_state/hh_session.json → data/storage_state/ → покрыто 'data/'.
    repo_copy = _repo_copy_with_real_gitignore(tmp_path)
    cfg = _write_config_in_data(repo_copy, _shipped_storage_state_path(), "SEC1")

    config = load_config(cfg)
    assert _is_gitignored_repo(config.storage_state_file, repo_copy), (
        f"storage_state_file резолвится в НЕ-ignored путь: {config.storage_state_file}. "
        "Секрет сессии hh.ru может попасть в git-коммит."
    )
    # Сессия остаётся ВНУТРИ data/ — не уезжает за пределы игнорируемой папки.
    assert (repo_copy / "data") in config.storage_state_file.parents


def test_session_secret_independent_of_cwd(tmp_path, monkeypatch):
    # Codex round-2: при запуске из ЧУЖОЙ директории с абсолютным --config путь
    # всё равно должен указывать на gitignored место (рядом с конфигом), а не
    # в CWD вызывающего. Регрессия: relative-to-cwd ломал это.
    repo_copy = _repo_copy_with_real_gitignore(tmp_path)
    cfg = _write_config_in_data(repo_copy, _shipped_storage_state_path(), "SEC2")

    # Запуск из /tmp (чужой CWD) с абсолютным путём к конфигу.
    monkeypatch.chdir(tmp_path)
    config = load_config(cfg.resolve())
    assert _is_gitignored_repo(config.storage_state_file, repo_copy), (
        f"storage_state_file зависит от CWD и попал в НЕ-ignored путь: {config.storage_state_file}"
    )
    # Путь указывает рядом с конфигом, а не в текущий CWD.
    assert repo_copy in config.storage_state_file.parents


def test_session_secret_gitignored_outside_data_dir(tmp_path):
    # Defence-in-depth (#23 round-4, сохранено в #133): значение
    # storage_state_file подконтрольно ПОЛЬЗОВАТЕЛЮ и может увести сессию за
    # пределы data/ — например '../secrets/storage_state/hh_session.json'.
    # Путь НЕ должен проходить ни через одну директорию 'data' (иначе его
    # покрыло бы уже само правило 'data/' без leading slash, и тест не
    # отличал бы наличие catch-all от его отсутствия — см. review PR #134).
    # Правило 'data/' здесь недостаточно, поэтому РЕАЛЬНЫЙ .gitignore обязан
    # покрывать session-файлы в ЛЮБОЙ директории ('**/storage_state/*.json'),
    # иначе секрет hh.ru публикуется при `git add .`.
    repo_copy = _repo_copy_with_real_gitignore(tmp_path)
    cfg = _write_config_in_data(repo_copy, "../secrets/storage_state/hh_session.json", "LEG1")

    config = load_config(cfg)
    assert _is_gitignored_repo(config.storage_state_file, repo_copy), (
        f"storage_state_file вне data/ резолвится в НЕ-ignored путь: "
        f"{config.storage_state_file}. Defence-in-depth .gitignore нужен "
        "(**/storage_state/*.json)."
    )


def test_session_backup_gitignored_outside_data_dir(tmp_path):
    # #166 review (Codex): import-cookies (cookie_import.write_storage_state)
    # кладёт бэкап существующей сессии рядом как '<file>.bak' / '.bak.N'.
    # Правило '**/storage_state/*.json' покрывает сам storage_state_file, но
    # НЕ покрывает '<file>.json.bak' — та же утечка hhtoken при
    # storage_state_file вне data/ (пользовательский конфиг), только через
    # бэкап, а не через основной файл. Регрессия: 'git add .' публикует
    # предыдущую сессию hh.ru.
    repo_copy = _repo_copy_with_real_gitignore(tmp_path)
    cfg = _write_config_in_data(repo_copy, "../secrets/storage_state/hh_session.json", "LEG2")

    config = load_config(cfg)
    backup = config.storage_state_file.with_name(config.storage_state_file.name + ".bak")
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.touch()
    assert _is_gitignored_repo(backup, repo_copy), (
        f"Бэкап storage_state_file вне data/ резолвится в НЕ-ignored путь: {backup}. "
        "Defence-in-depth .gitignore нужен для '**/storage_state/*.json.bak*'."
    )


def test_session_write_temp_file_gitignored_outside_data_dir(tmp_path):
    # #166 review (Codex, cycle 3): write_storage_state пишет новую сессию
    # атомарно через temp-файл '<file>.json.tmp' + os.replace() (не корраптит
    # активную сессию при обрыве записи). Этот temp-файл несёт то же секретное
    # содержимое (hhtoken), что и storage_state_file/.bak, и должен быть
    # покрыт тем же defence-in-depth правилом вне data/.
    repo_copy = _repo_copy_with_real_gitignore(tmp_path)
    cfg = _write_config_in_data(repo_copy, "../secrets/storage_state/hh_session.json", "LEG3")

    config = load_config(cfg)
    tmp_file = config.storage_state_file.with_name(config.storage_state_file.name + ".tmp")
    tmp_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file.touch()
    assert _is_gitignored_repo(tmp_file, repo_copy), (
        f"Temp-файл записи storage_state_file вне data/ резолвится в НЕ-ignored путь: {tmp_file}. "
        "Defence-in-depth .gitignore нужен для '**/storage_state/*.json.tmp'."
    )


def test_repo_gitignore_covers_all_mutable_data(tmp_path):
    # #133: ВСЕ изменяемые данные под data/ и покрыты одной строкой 'data/'.
    # Регрессия: точечные правила ('data/*.db', 'logs/*.log' ...) не покрывали
    # новые артефакты, и каждый новый файл был шансом закоммитить приватное.
    repo_copy = _repo_copy_with_real_gitignore(tmp_path)
    mutable = [
        "data/config.yaml",
        "data/history.db",
        "data/storage_state/hh_session.json",
        "data/logs/hhru_bot.log",
        "data/logs/scheduled.log",
        "data/logs/probe_42_form.html",
        "data/logs/probe_42_form.png",
        # Артефакт, которого сегодня нет — правило 'data/' покрывает и его.
        "data/whatever_new_artifact.json",
    ]
    result = subprocess.run(
        ["git", "-C", str(repo_copy), "check-ignore", *mutable],
        capture_output=True,
        text=True,
        check=False,
    )
    ignored = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    assert not [m for m in mutable if m not in ignored], (
        f"не покрыты .gitignore: {[m for m in mutable if m not in ignored]}"
    )


def test_repo_gitignore_keeps_config_example_tracked():
    # config/config.example.yaml — версионируемый шаблон формата, НЕ данные.
    # Он обязан оставаться под контролем git: без него нечего копировать в
    # data/config.yaml, а тесты (_shipped_storage_state_path) читают его из репо.
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "-C", str(repo_root), "check-ignore", "config/config.example.yaml"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, (
        "config/config.example.yaml попал в .gitignore — шаблон конфига должен версионироваться."
    )


# --- ai_profile: опциональная resume-секция для AI-писем (#17) ---


def test_load_config_ai_profile_none_when_absent(tmp_path):
    # Без секции ai_profile → ResumeConfig.ai_profile = None (AI выключен).
    config = load_config(_write_config(tmp_path, _minimal_config()))
    assert config.resumes[0].ai_profile is None


def test_load_config_ai_profile_full(tmp_path):
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AI100"
            search:
              text: "python developer"
            ai_profile:
              summary: "Бэкенд-разработчик"
              skills: ["python", "django"]
              highlights: ["Поднял throughput в 3 раза"]
              desired_role: "Senior Python Developer"
              tone: friendly
        """,
    )
    config = load_config(path)
    profile: AIProfile = config.resumes[0].ai_profile  # type: ignore[assignment]
    assert profile is not None
    assert profile.summary == "Бэкенд-разработчик"
    assert profile.skills == ["python", "django"]
    assert profile.highlights == ["Поднял throughput в 3 раза"]
    assert profile.desired_role == "Senior Python Developer"
    assert profile.tone == "friendly"


def test_load_config_ai_profile_defaults_partial(tmp_path):
    # Поля опциональны по отдельности: задан только один — остальные дефолты.
    # Консистентно с scoring: пустая {} трактуется как отсутствие секции (None).
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AI200"
            search:
              text: "x"
            ai_profile:
              summary: "Краткое описание"
        """,
    )
    profile: AIProfile = load_config(path).resumes[0].ai_profile  # type: ignore[assignment]
    assert profile is not None
    assert profile.summary == "Краткое описание"
    assert profile.skills == []
    assert profile.highlights == []
    assert profile.desired_role == ""
    assert profile.tone == "formal"


def test_load_config_ai_profile_empty_dict_is_none(tmp_path):
    # Пустая секция ai_profile: {} трактуется как отсутствие (None) —
    # консистентно с поведением parse_scoring для scoring: {}.
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AI250"
            search:
              text: "x"
            ai_profile: {}
        """,
    )
    assert load_config(path).resumes[0].ai_profile is None


def test_load_config_ai_profile_invalid_tone(tmp_path):
    # Неизвестный tone → ConfigError (явный контракт, не молчаливый пропуск).
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AI300"
            search:
              text: "x"
            ai_profile:
              tone: casual
        """,
    )
    with pytest.raises(ConfigError, match="tone"):
        load_config(path)


def test_load_config_ai_profile_skills_wrong_type(tmp_path):
    # skills не список строк → ConfigError.
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AI400"
            search:
              text: "x"
            ai_profile:
              skills: "python"
        """,
    )
    with pytest.raises(ConfigError, match="skills"):
        load_config(path)


# --- ai_profile.cover_letter_examples: few-shot стиль писём (#96) ---


def test_load_config_ai_profile_cover_letter_examples(tmp_path):
    # cover_letter_examples — список прошлых писем как образцы стиля (#96).
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AI500"
            search:
              text: "x"
            ai_profile:
              summary: "Бэкенд-разработчик"
              cover_letter_examples:
                - "Здравствуйте! Пишу как бэкенд-разработчик."
                - "Добрый день. Мой опыт в python релевантен."
        """,
    )
    profile: AIProfile = load_config(path).resumes[0].ai_profile  # type: ignore[assignment]
    assert profile is not None
    assert profile.cover_letter_examples == [
        "Здравствуйте! Пишу как бэкенд-разработчик.",
        "Добрый день. Мой опыт в python релевантен.",
    ]


def test_load_config_ai_profile_cover_letter_examples_default_empty(tmp_path):
    # Без cover_letter_examples → [] (опционально, обратная совместимость #17).
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AI600"
            search:
              text: "x"
            ai_profile:
              summary: "Краткое описание"
        """,
    )
    profile: AIProfile = load_config(path).resumes[0].ai_profile  # type: ignore[assignment]
    assert profile is not None
    assert profile.cover_letter_examples == []


def test_load_config_ai_profile_cover_letter_examples_wrong_type(tmp_path):
    # cover_letter_examples не список строк → ConfigError.
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AI700"
            search:
              text: "x"
            ai_profile:
              cover_letter_examples: "не список"
        """,
    )
    with pytest.raises(ConfigError, match="cover_letter_examples"):
        load_config(path)


def test_parse_ai_empty_dict_enables_ai_without_fields():
    # Issue #230: наличие секции включает AI; поля больше не обязательны.
    cfg = parse_ai({}, "ai")
    assert cfg is not None
    assert cfg.provider is None
    assert cfg.model is None
    assert cfg.base_url is None


def test_parse_ai_question_answers_are_explicitly_opt_in():
    assert parse_ai({"answer_questions": True}, "ai").answer_questions is True
    with pytest.raises(ConfigError, match="answer_questions"):
        parse_ai({"answer_questions": "yes"}, "ai")


def test_parse_ai_absent_returns_none():
    assert parse_ai(None, "ai") is None


def test_parse_ai_legacy_routing_fields_fail_closed():
    # Issue #230: legacy provider/model/base_url не констрейнят маршрут —
    # при их задании парсер падает, чтобы оператор явно мигрировал.
    with pytest.raises(ConfigError, match="устарели"):
        parse_ai(
            {"provider": "openai", "model": "gpt-4o", "base_url": "https://api.openai.com/v1"},
            "ai",
        )


def test_parse_ai_legacy_single_field_fail_closed():
    with pytest.raises(ConfigError, match="ai.model"):
        parse_ai({"model": "gpt-4o"}, "ai")
