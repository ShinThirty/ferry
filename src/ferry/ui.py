"""What ferry says, and where.

Three audiences. normal: a person at the terminal. hook: Claude's SessionStart,
which reads stdout into the session — so warnings go there too, and a failure
must never block the session. bg: a detached run whose stdout is .ferry/log.
"""
from __future__ import annotations

import sys
import time
from typing import NoReturn

from . import system

NORMAL, HOOK, BG = "normal", "hook", "bg"


def human(b: int) -> str:
    units = ("B", "KB", "MB", "GB")
    n, i = float(b), 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{int(n)} {units[i]}" if i == 0 else f"{n:.1f} {units[i]}"


def day(t: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class UI:
    def __init__(self, mode: str = NORMAL):
        self.mode = mode
        self.said = False          # whether anything worth reporting was said
        self.red, self.grn, self.ylw, self.bld, self.rst = (
            ("\033[31m", "\033[32m", "\033[33m", "\033[1m", "\033[0m") if system.ansi_colours() else ("",) * 5)

    @staticmethod
    def _out(text: str, *, err: bool = False) -> None:
        print(text, file=sys.stderr if err else sys.stdout, flush=True)

    def info(self, msg: str) -> None:
        if self.mode == NORMAL:
            self._out(msg)

    def hdr(self, msg: str) -> None:
        if self.mode == NORMAL:
            self._out(f"\n{self.bld}== {msg} =={self.rst}")

    def note(self, msg: str) -> None:
        """Worth the terminal or the log, not the session."""
        if self.mode == NORMAL:
            self.said = True
            self._out(f"      {msg}")
        elif self.mode == BG:
            self.said = True
            self._out(f"{_now()}  {msg}")

    def ok(self, msg: str) -> None:
        self.said = True
        if self.mode == NORMAL:
            self._out(f"{self.grn}  ok{self.rst}  {msg}")
        elif self.mode == HOOK:
            self._out(f"ferry: {msg}")
        else:
            self._out(f"{_now()}  {msg}")

    def warn(self, msg: str) -> None:
        self.said = True
        if self.mode == NORMAL:
            self._out(f"{self.ylw}warn{self.rst}  {msg}", err=True)
        elif self.mode == HOOK:
            self._out(f"ferry: WARNING — {msg}")
        else:
            self._out(f"{_now()}  warn  {msg}")

    def die(self, msg: str) -> NoReturn:
        if self.mode == NORMAL:
            self._out(f"{self.red}fail{self.rst}  {msg}", err=True)
            raise SystemExit(1)
        self.warn(msg)
        raise SystemExit(0 if self.mode == HOOK else 1)

    def mark(self, text: str) -> str:
        """text, highlighted at a terminal."""
        return f"{self.ylw}{text}{self.rst}"
