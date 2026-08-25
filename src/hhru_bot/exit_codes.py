"""Typed exit statuses returned by commands to the CLI dispatcher."""

from enum import Enum


class CommandExitCode(Enum):
    """A command-level status that must be handled by :mod:`hhru_bot.cli`."""

    PERSISTENCE_FAILED = 2
    SIGHUP = 129
    SIGINT = 130
    SIGTERM = 143
