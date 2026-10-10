"""A tiny tagged result so pages render EMPTY/ERROR states instead of tracebacks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, Optional, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Result(Generic[T]):
    status: Literal["OK", "EMPTY", "ERROR"]
    value: Optional[T] = None
    message: str = ""

    @property
    def is_ok(self) -> bool:
        return self.status == "OK"

    @classmethod
    def ok(cls, value: T) -> "Result[T]":
        return cls("OK", value)

    @classmethod
    def empty(cls, message: str) -> "Result[T]":
        return cls("EMPTY", None, message)

    @classmethod
    def error(cls, message: str) -> "Result[T]":
        return cls("ERROR", None, message)
