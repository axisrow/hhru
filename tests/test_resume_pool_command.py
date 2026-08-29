"""Тесты команды resume-pool (#754): dry-run план, batch-цикл, durable-гарантии.

WRITE-hh-ru batch-команда: боевой режим требует --force или интерактивного
prompt; каждая копия использует тот же durable seam (DurableMutationAttempt,
has_unresolved_uncertain), что и обычный copy-resume — batch останавливается
на первом отказе/uncertain, не обходит guard ради продолжения (issue #754 п.7).
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import hhru_bot.browser
import hhru_bot.commands.resume_pool as cmd
import hhru_bot.copy_resume
from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
from hhru_bot.copy_resume import CopyResumeResult
from hhru_bot.history import History
from hhru_bot.resume_clusters import CLUSTERS

pytestmark = pytest.mark.integration

SOURCE_ID = "a" * 38
NEW_IDS = [chr(ord("b") + i) * 38 for i in range(len(CLUSTERS))]


def _source_resume() -> ResumeConfig:
    return ResumeConfig(
        id="backend",
        resume_url=f"https://hh.ru/resume/{SOURCE_ID}",
        search=SearchFilters(text="python"),
    )


def _fake_config(tmp_path, resumes=None):
    return AppConfig(
        storage_state_file=tmp_path / "session.json",
        throttle=ThrottleConfig(),
        cover_letter_default="...",
        resumes=resumes if resumes is not None else [_source_resume()],
        user_agent=None,
    )


def _args(tmp_path, **overrides):
    base = {
        "config": "unused.yaml",
        "history": str(tmp_path / "h.db"),
        "headless": True,
        "source": "backend",
        "dry_run": False,
        "force": False,
        "write_config": False,
        "limit": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Подменяет config/браузер; results — очередь исходов по одному на копию."""
    state = SimpleNamespace(
        results=[CopyResumeResult("backend", True, new_id) for new_id in NEW_IDS],
        calls=[],
        titles=[],
    )

    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: _fake_config(tmp_path))

    @contextmanager
    def fake_launch(*a, **kw):
        yield SimpleNamespace(new_page=lambda: SimpleNamespace())

    monkeypatch.setattr(hhru_bot.browser, "launch_context", fake_launch)

    def fake_copy(page, resume, dry_run, *, before_click=None):
        idx = len(state.calls)
        state.calls.append((resume.id, dry_run))
        result = (
            state.results[idx]
            if idx < len(state.results)
            else CopyResumeResult("backend", False, reason="no more scripted results")
        )
        if not dry_run and (result.success or result.uncertain):
            before_click()
        return result

    monkeypatch.setattr(hhru_bot.copy_resume, "copy_resume_on_hh", fake_copy)
    monkeypatch.setattr(
        cmd,
        "_set_copy_title",
        lambda page, resume_id, title: state.titles.append((resume_id, title)),
    )
    return state


# --- dry-run: план ---


def test_dry_run_prints_full_plan_for_all_clusters(env, capsys, tmp_path):
    cmd.run(_args(tmp_path, dry_run=True))
    out = capsys.readouterr().out
    assert f"требуется создать {len(CLUSTERS)} из {len(CLUSTERS)} кластеров" in out
    for cluster in CLUSTERS:
        assert cluster.key in out
        assert cluster.title in out
    assert "[INFO] Ничего не отправлено." in out
    assert env.calls == []


def test_dry_run_excludes_already_covered_clusters(env, capsys, tmp_path):
    covered_key = CLUSTERS[0].key
    covered = ResumeConfig(
        id=f"backend-{covered_key}",
        resume_url="https://hh.ru/resume/" + "c" * 38,
        search=SearchFilters(text="python"),
        cluster=covered_key,
    )
    monkeypatch_config = _fake_config(tmp_path, resumes=[_source_resume(), covered])
    import hhru_bot.config

    orig = hhru_bot.config.load_config_or_exit
    hhru_bot.config.load_config_or_exit = lambda path: monkeypatch_config
    try:
        cmd.run(_args(tmp_path, dry_run=True))
    finally:
        hhru_bot.config.load_config_or_exit = orig
    out = capsys.readouterr().out
    assert f"требуется создать {len(CLUSTERS) - 1} из {len(CLUSTERS)} кластеров" in out
    assert f"уже покрыт: {covered_key}" in out


def test_dry_run_limit_reports_trimmed_plan(env, capsys, tmp_path):
    cmd.run(_args(tmp_path, dry_run=True, limit=1))
    out = capsys.readouterr().out
    assert f"требуется создать {len(CLUSTERS)} из {len(CLUSTERS)} кластеров" in out
    assert "--limit обрезал план" in out


def test_fully_covered_pool_reports_nothing_to_do(env, capsys, tmp_path):
    covered = [
        ResumeConfig(
            id=f"backend-{c.key}",
            resume_url=f"https://hh.ru/resume/{c.key}{'d' * 30}",
            search=SearchFilters(text="python"),
            cluster=c.key,
        )
        for c in CLUSTERS
    ]
    config = _fake_config(tmp_path, resumes=[_source_resume(), *covered])
    import hhru_bot.config as config_module

    orig = config_module.load_config_or_exit
    config_module.load_config_or_exit = lambda path: config
    try:
        cmd.run(_args(tmp_path, dry_run=True))
    finally:
        config_module.load_config_or_exit = orig
    out = capsys.readouterr().out
    assert "Все кластеры уже покрыты" in out
    assert env.calls == []


# --- боевой batch-цикл ---


def test_batch_creates_all_clusters_in_order(env, capsys, tmp_path):
    cmd.run(_args(tmp_path, force=True))
    out = capsys.readouterr().out
    for cluster, new_id in zip(CLUSTERS, NEW_IDS, strict=True):
        assert f"[OK] кластер {cluster.key} -> resume_id {new_id}" in out
    assert env.calls == [("backend", False)] * len(CLUSTERS)
    assert env.titles == [(new_id, c.title) for c, new_id in zip(CLUSTERS, NEW_IDS, strict=True)]


def test_batch_stops_on_first_uncertain(env, capsys, tmp_path):
    env.results[1] = CopyResumeResult("backend", False, uncertain=True, reason="клик мог уйти")
    cmd.run(_args(tmp_path, force=True))
    out = capsys.readouterr().out
    assert f"[OK] кластер {CLUSTERS[0].key}" in out
    assert "[FAIL] (uncertain)" in out
    # Third and later clusters never attempted.
    assert len(env.calls) == 2

    history = History(tmp_path / "h.db")
    assert history.has_unresolved_uncertain(SOURCE_ID, "copy_resume")


def test_batch_stops_on_first_plain_failure(env, capsys, tmp_path):
    env.results[0] = CopyResumeResult(
        "backend", False, reason="duplicate_action_missing: действие не найдено"
    )
    cmd.run(_args(tmp_path, force=True))
    out = capsys.readouterr().out
    assert "duplicate_action_missing" in out
    assert env.calls == [("backend", False)]


def test_pool_blocked_by_preexisting_unresolved_uncertain(env, tmp_path, capsys):
    history = History(tmp_path / "h.db")
    history.record_action(SOURCE_ID, SOURCE_ID, "copy_resume", "uncertain", "клик мог уйти")

    with pytest.raises(SystemExit) as exc:
        cmd.run(_args(tmp_path, force=True))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "не подтверждено (uncertain)" in out
    assert env.calls == []


def test_limit_creates_only_requested_count(env, capsys, tmp_path):
    cmd.run(_args(tmp_path, force=True, limit=2))
    assert len(env.calls) == 2


def test_write_config_records_cluster_for_each_copy(env, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "account:\n  storage_state_file: storage_state/hh_session.json\n"
        'cover_letter_default: "..."\n'
        "resumes:\n"
        '  - id: "backend"\n'
        f'    resume_url: "https://hh.ru/resume/{SOURCE_ID}"\n'
        '    search:\n      text: "python"\n',
        encoding="utf-8",
    )
    cmd.run(_args(tmp_path, force=True, write_config=True, config=str(config_path)))

    from hhru_bot.config import load_config

    reloaded = load_config(config_path)
    written = {r.cluster: r.id for r in reloaded.resumes if r.cluster is not None}
    assert set(written) == {c.key for c in CLUSTERS}


def test_run_refuses_bare_source_before_touching_browser(env, capsys, tmp_path, monkeypatch):
    bare = SimpleNamespace(id=SOURCE_ID, resume_id=SOURCE_ID, search=SimpleNamespace(text=""))
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda path: SimpleNamespace(
            get_resume=lambda rid: bare,
            resumes=[bare],
            storage_state_file=tmp_path / "session.json",
            user_agent=None,
        ),
    )
    with pytest.raises(SystemExit):
        cmd.run(_args(tmp_path, force=True, source=SOURCE_ID, write_config=True))
    assert env.calls == []


def test_run_allows_bare_source_without_write_config(env, capsys, tmp_path, monkeypatch):
    """cycle-review PR #770: reject_bare_source гейтится write_config, как у
    copy-resume (commands/copy_resume.py:392-393) -- bare-резюме без --write-config
    не требует search.text, т.к. ничего не записывается в config.yaml."""
    bare = SimpleNamespace(
        id=SOURCE_ID, resume_id=SOURCE_ID, search=SimpleNamespace(text=""), cluster=None
    )
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda path: SimpleNamespace(
            get_resume=lambda rid: bare,
            resumes=[bare],
            storage_state_file=tmp_path / "session.json",
            user_agent=None,
        ),
    )
    cmd.run(_args(tmp_path, force=True, source=SOURCE_ID, write_config=False))
    out = capsys.readouterr().out
    assert "[FAIL]" not in out
    assert len(env.calls) == len(CLUSTERS)


def test_run_refuses_slug_collision_before_touching_browser(env, capsys, tmp_path, monkeypatch):
    """cycle-review PR #770 (AO reviewer, blocking): коллизия slug должна
    отсекаться до launch_context -- та же логика 'отказ до записи', что у
    copy-resume (commands/copy_resume.py:386-389), иначе первый клик
    'Дублировать' в batch создаёт неоткатываемый дубль на hh.ru, который
    write_resume_config потом откажется записать в config.yaml."""
    clashing_key = CLUSTERS[0].key
    clashing = ResumeConfig(
        id=f"backend-{clashing_key}",  # тот же slug, что build_pool_plan сгенерирует
        resume_url="https://hh.ru/resume/" + "e" * 38,
        search=SearchFilters(text="python"),
        cluster=None,  # не привязан к кластеру -> build_pool_plan сочтёт кластер непокрытым
    )
    config = _fake_config(tmp_path, resumes=[_source_resume(), clashing])
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: config)

    with pytest.raises(SystemExit) as exc:
        cmd.run(_args(tmp_path, force=True, write_config=True))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert clashing.id in out
    assert env.calls == []


def test_run_without_force_non_tty_exits_1(env, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(SystemExit) as exc:
        cmd.run(_args(tmp_path))
    assert exc.value.code == 1
    assert env.calls == []
