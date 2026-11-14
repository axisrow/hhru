"""Стюард JS-реестра известных модалок — overlay_registry.js (#1227).

Слой данных реестра живёт в двух копиях: расширение (JS-таблица — источник
якоря/контекста для policy-гейта клика) и CLI-слой (modal_registry.py —
имена семейств; selector_groups/_generated.py — канонические data-qa).
Дословное совпадение копий иначе не проверяется ничем: расхождение прятало
бы решение о клике в расширении от каталога CLI-слоя — ту же боль «каждый
бой узнаёт модалки заново», ради которой реестр заводился.

Паттерн — как у allowlist live-канала: литерал в двух копиях + гвард-тест
на совпадение (extensions/hhru-live/README.md, «Команды (allowlist)»).
Граница #1227: стюард проверяет ТОЛЬКО данные; поведенческие гарантии
(allowApply-гейт, dangerous раньше реестра, fail-closed пустой таблицы) —
в test_hhru_live_behavior.py через run_executor_scenario.js.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from hhru_bot.live.modal_registry import MODAL_REGISTRY
from hhru_bot.selector_groups._generated import VALUES as GENERATED_SELECTORS

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).parents[1]
RUNNER = REPO_ROOT / "tests" / "js_harness" / "run_overlay_registry.js"

# ТОЧНЫЙ data-qa из сгенерированного CLI-селектора вида [data-qa='X'] /
# [data-qa="X"]: ровно одно вхождение — якорный носитель; больше одного
# (или ноль) — селектор не адресует единственный data-qa, строке реестра
# нужен однозначный ключ.
_DATA_QA_RE = re.compile(r"""data-qa=['"]([^'"]+)['"]""")


def _load_rules() -> list[dict]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node не найден в PATH — стюард JS-реестра пропущен")
    result = subprocess.run(
        [node, str(RUNNER)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"node harness завершился с ошибкой:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return json.loads(result.stdout)["rules"]


def test_registry_rows_are_unique_and_typed() -> None:
    rules = _load_rules()
    assert rules, "реестр без строк — policy-гейт не разрешает ни один контекст"
    ids = [rule["id"] for rule in rules]
    targets = [rule["targetQa"] for rule in rules]
    assert len(ids) == len(set(ids)), "id — ключ строки реестра"
    assert len(targets) == len(set(targets)), "targetQa адресует одну строку"
    for rule in rules:
        for field in ("id", "targetQa", "context", "selectorKey"):
            value = rule.get(field)
            assert isinstance(value, str) and value.strip(), f"{rule.get('id')}: поле {field}"
        assert rule["context"].startswith("apply_flow_"), (
            f"{rule['id']}: реестр разрешает только контексты apply-флоу"
        )


def test_registry_rows_name_python_registry_families() -> None:
    # Строка JS-реестра — то же семейство, что запись Python-реестра:
    # расхождение имён делит знание о модалке между слоями (эпик #1227 —
    # против этого). Python-реестр шире (census-матчинг знает модалки без
    # кликового якоря), поэтому отношение — подмножество, не равенство.
    js_ids = {rule["id"] for rule in _load_rules()}
    python_names = {record.name for record in MODAL_REGISTRY}
    orphan = js_ids - python_names
    assert not orphan, f"строки без записи в modal_registry: {sorted(orphan)}"


def test_registry_target_qa_matches_generated_cli_selectors() -> None:
    # Якорь строки = data-qa из сгенерированного CLI-селектора: канал и
    # расширение адресуют ОДНУ кнопку (правка селектора CLI-пути без правки
    # реестра — или наоборот — валит этот тест, а не молча расходится в бою).
    for rule in _load_rules():
        selector = GENERATED_SELECTORS.get(rule["selectorKey"])
        assert selector is not None, f"{rule['id']}: ключа {rule['selectorKey']} нет в _generated"
        found = _DATA_QA_RE.findall(selector)
        assert len(found) == 1, f"{rule['id']}: {selector} не несёт ровно один data-qa"
        assert found[0] == rule["targetQa"], (
            f"{rule['id']}: targetQa={rule['targetQa']}, CLI-селектор {selector}"
        )
