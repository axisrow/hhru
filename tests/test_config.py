"""Characterization-тесты config.py: load_config и дата-классы.

Поведение парсинга не должно измениться после ввода config_sections/.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from hhru_bot.config import ConfigError, ResumeConfig, SearchFilters, load_config


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


def test_session_secret_is_gitignored_when_config_in_repo(tmp_path):
    # Инвариант безопасности (#23 review): shipped storage_state_file из
    # config.example.yaml должен резолвиться (относительно директории конфига)
    # в путь, покрытый .gitignore. Иначе login (auth.py) запишет cookies/
    # localStorage сессии hh.ru в НЕ-ignored файл → account takeover при коммите.
    #
    # Конфиг лежит в config/config.yaml (как shipped example), запуск из корня.
    shipped = _shipped_storage_state_path()
    config_in_subdir = tmp_path / "config" / "config.yaml"
    config_in_subdir.parent.mkdir(parents=True)
    config_in_subdir.write_text(
        textwrap.dedent(
            f"""
            account:
              storage_state_file: {shipped}
            resumes:
              - id: r1
                resume_url: "https://hh.ru/resume/SEC1"
                search:
                  text: x
            """
        ),
        encoding="utf-8",
    )
    # Имитируем реальный layout: конфиг в <repo>/config/, data/ в <repo>/data/.
    # load_config резолвит storage_state_file относительно директории конфига.
    repo_copy = tmp_path / "repo"
    repo_copy.mkdir()
    (repo_copy / "config").mkdir()
    (repo_copy / "data" / "storage_state").mkdir(parents=True)
    # Инициализируем git + копируем .gitignore, чтобы check-ignore был валиден.
    subprocess.run(["git", "-C", str(repo_copy), "init", "-q"], check=True)
    (repo_copy / ".gitignore").write_text(
        "config/config.yaml\ndata/storage_state/*.json\ndata/*.db\n", encoding="utf-8"
    )
    cfg = repo_copy / "config" / "config.yaml"
    cfg.write_text(config_in_subdir.read_text(encoding="utf-8"), encoding="utf-8")

    config = load_config(cfg)
    assert _is_gitignored_repo(config.storage_state_file, repo_copy), (
        f"storage_state_file резолвится в НЕ-ignored путь: {config.storage_state_file}. "
        "Секрет сессии hh.ru может попасть в git-коммит."
    )


def test_session_secret_independent_of_cwd(tmp_path, monkeypatch):
    # Codex round-2: при запуске из ЧУЖОЙ директории с абсолютным --config путь
    # всё равно должен указывать на gitignored место (рядом с конфигом), а не
    # в CWD вызывающего. Регрессия: relative-to-cwd ломал это.
    shipped = _shipped_storage_state_path()
    repo_copy = tmp_path / "repo"
    (repo_copy / "config").mkdir(parents=True)
    (repo_copy / "data" / "storage_state").mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo_copy), "init", "-q"], check=True)
    (repo_copy / ".gitignore").write_text(
        "config/config.yaml\ndata/storage_state/*.json\ndata/*.db\n", encoding="utf-8"
    )
    cfg = repo_copy / "config" / "config.yaml"
    cfg.write_text(
        textwrap.dedent(
            f"""
            account:
              storage_state_file: {shipped}
            resumes:
              - id: r1
                resume_url: "https://hh.ru/resume/SEC2"
                search:
                  text: x
            """
        ),
        encoding="utf-8",
    )

    # Запуск из /tmp (чужой CWD) с абсолютным путём к конфигу.
    monkeypatch.chdir(tmp_path)
    config = load_config(cfg.resolve())
    assert _is_gitignored_repo(config.storage_state_file, repo_copy), (
        f"storage_state_file зависит от CWD и попал в НЕ-ignored путь: {config.storage_state_file}"
    )
    # Путь указывает рядом с конфигом, а не в текущий CWD.
    assert repo_copy in config.storage_state_file.parents


def test_session_secret_gitignored_for_legacy_config_value(tmp_path):
    # Codex round-4: существующие user-конфиги (созданные до этого PR) всё ещё
    # содержат СТАРОЕ shipped значение 'data/storage_state/hh_session.json'
    # (без ../). С relative-to-config резолвом при конфиге в config/ это даёт
    # config/data/storage_state/... — НЕ покрыто точечным правилом
    # 'data/storage_state/*.json'. Defence-in-depth: РЕАЛЬНЫЙ .gitignore репо
    # должен покрывать session-файлы в ЛЮБОЙ директории ('**/storage_state/*.json'),
    # иначе legacy конфиг публикует секрет сессии при git add.
    repo_root = Path(__file__).resolve().parents[1]
    repo_copy = tmp_path / "repo"
    (repo_copy / "config").mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo_copy), "init", "-q"], check=True)
    # Копируем РЕАЛЬНЫЙ .gitignore репозитория — не выдуманный mock.
    shutil.copyfile(repo_root / ".gitignore", repo_copy / ".gitignore")
    cfg = repo_copy / "config" / "config.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            account:
              storage_state_file: data/storage_state/hh_session.json
            resumes:
              - id: r1
                resume_url: "https://hh.ru/resume/LEG1"
                search:
                  text: x
            """
        ),
        encoding="utf-8",
    )
    config = load_config(cfg)
    # Legacy значение резолвится в config/data/... — должно быть gitignored.
    assert _is_gitignored_repo(config.storage_state_file, repo_copy), (
        f"legacy storage_state_file резолвится в НЕ-ignored путь: "
        f"{config.storage_state_file}. Defence-in-depth .gitignore нужен "
        "(**/storage_state/*.json)."
    )
