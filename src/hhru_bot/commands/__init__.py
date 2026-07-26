"""Пакет команд CLI.

Каждый модуль (кроме _common) реализует register(subparsers) для авторегистрации.
cli.build_parser обходит модули через pkgutil и вызывает register у каждого.
Добавление команды = новый файл commands/<name>.py, 0 правок cli.py.
"""

from __future__ import annotations
