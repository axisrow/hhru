"""Тесты внешних сессий провайдеров (#1103): реестр, выбор сессии, конфиг."""

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path

import pytest

from hhru_bot.config import ConfigError, load_config
from hhru_bot.external_sessions import provider_for_url, resolve_external_session

pytestmark = pytest.mark.unit


def _write_config(tmp_path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _minimal_config(extra_account: str = "") -> str:
    extra = textwrap.indent(textwrap.dedent(extra_account), "  ") if extra_account.strip() else ""
    return (
        "account:\n"
        "  storage_state_file: storage_state/hh_session.json\n"
        f"{extra}"
        "resumes:\n"
        "  - id: r1\n"
        '    resume_url: "https://hh.ru/resume/AAA111"\n'
        "    search:\n"
        "      text: python\n"
    )


class TestProviderForUrl:
    def test_yandex_domain_and_subdomain(self):
        for url in (
            "https://yandex.ru",
            "https://forms.yandex.ru/surveys/1",
            "https://ya.ru",
        ):
            provider = provider_for_url(url)
            assert provider is not None and provider.name == "yandex", url

    def test_foreign_domain_is_none(self):
        assert provider_for_url("https://example.com/form") is None
        # Сокращатель ya.cc НЕ входит в домены провайдера: следование за ним
        # запрещено (граница #1103), ya.cc не должен опознаваться как яндексовый.
        assert provider_for_url("https://ya.cc/t/abc") is None

    def test_lookalike_suffix_is_not_subdomain(self):
        # notyandex.ru не поддомен yandex.ru — матч по суффиксу строки
        # без точки был бы ложным срабатыванием.
        assert provider_for_url("https://notyandex.ru") is None


class TestResolveExternalSession:
    def test_auto_by_domain_with_existing_file(self, tmp_path):
        session = tmp_path / "yandex_session.json"
        session.write_text("{}", encoding="utf-8")
        result = resolve_external_session("https://forms.yandex.ru/surveys/1", {"yandex": session})
        assert result is not None
        assert result[0].name == "yandex"
        assert result[1] == session

    def test_auto_missing_file_returns_none(self, tmp_path):
        session = tmp_path / "yandex_session.json"
        assert (
            resolve_external_session("https://forms.yandex.ru/surveys/1", {"yandex": session})
            is None
        )

    def test_auto_unrelated_domain_returns_none(self, tmp_path):
        session = tmp_path / "yandex_session.json"
        session.write_text("{}", encoding="utf-8")
        assert resolve_external_session("https://example.com", {"yandex": session}) is None

    def test_forced_provider_overrides_domain(self, tmp_path):
        session = tmp_path / "yandex_session.json"
        session.write_text("{}", encoding="utf-8")
        result = resolve_external_session(
            "https://example.com", {"yandex": session}, forced_provider="yandex"
        )
        assert result is not None and result[1] == session

    def test_forced_provider_missing_file_raises(self, tmp_path):
        session = tmp_path / "yandex_session.json"
        with pytest.raises(ValueError, match="login-external"):
            resolve_external_session(
                "https://example.com", {"yandex": session}, forced_provider="yandex"
            )

    def test_forced_provider_not_configured_raises(self):
        with pytest.raises(ValueError, match="login-external"):
            resolve_external_session("https://example.com", {}, forced_provider="yandex")

    def test_forced_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Неизвестный провайдер"):
            resolve_external_session("https://example.com", {}, forced_provider="nope")


class TestExternalSessionsConfig:
    def test_section_parsed_and_resolved_from_config_dir(self, tmp_path):
        cfg = _write_config(
            tmp_path,
            _minimal_config(
                extra_account="""
                external_sessions:
                  yandex:
                    storage_state_file: storage_state/yandex_session.json
                """
            ),
        )
        config = load_config(cfg)
        assert set(config.external_sessions) == {"yandex"}
        resolved = config.external_sessions["yandex"]
        assert resolved == (tmp_path / "storage_state" / "yandex_session.json").resolve()
        # Секрет второго уровня: сессия провайдера никогда не совпадает с
        # hh-сессией и не подмешивается в неё.
        assert resolved != config.storage_state_file

    def test_absent_section_is_empty(self, tmp_path):
        config = load_config(_write_config(tmp_path, _minimal_config()))
        assert config.external_sessions == {}

    def test_unknown_provider_rejected(self, tmp_path):
        cfg = _write_config(
            tmp_path,
            _minimal_config(
                extra_account="""
                external_sessions:
                  yandeks:
                    storage_state_file: storage_state/yandex_session.json
                """
            ),
        )
        with pytest.raises(ConfigError, match="yandeks"):
            load_config(cfg)

    def test_missing_storage_state_file_rejected(self, tmp_path):
        cfg = _write_config(
            tmp_path,
            _minimal_config(
                extra_account="""
                external_sessions:
                  yandex: {}
                """
            ),
        )
        with pytest.raises(ConfigError, match="storage_state_file"):
            load_config(cfg)


class TestLoginExternalCommand:
    def test_fail_when_provider_not_configured(self, tmp_path, monkeypatch, capsys):
        from hhru_bot.commands import login_external

        config = load_config(_write_config(tmp_path, _minimal_config()))
        monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda *_args, **_kw: config)
        login_external.run(
            argparse.Namespace(provider="yandex", account_dir=None, config=str(tmp_path))
        )
        out = capsys.readouterr().out
        assert "[FAIL]" in out and "external_sessions.yandex" in out
        # Никакого исключения: [FAIL]-отказ, как у остальных команд.
