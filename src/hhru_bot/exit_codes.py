"""Typed exit statuses returned by commands to the CLI dispatcher."""

from enum import Enum


class CommandExitCode(Enum):
    """A command-level status that must be handled by :mod:`hhru_bot.cli`."""

    PERSISTENCE_FAILED = 2
    # ``responses --alert-new`` uses a distinct non-error status so an
    # external scheduler can distinguish a newly discovered invitation from
    # an ordinary successful poll.
    NEW_INVITATIONS = 10
    # The command reached a confirmed unauthenticated page before any
    # irreversible action.  Keep this distinct from the generic fail-closed
    # status (1), persistence failures, and POSIX signal statuses so scheduled
    # callers can stop retrying and surface the required manual remediation.
    SESSION_EXPIRED = 78
    SIGHUP = 129
    SIGINT = 130
    SIGTERM = 143
