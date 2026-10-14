"""Name patterns (case-insensitive globs or /regexps/) and target-name casing."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Pattern:
    text: str
    regex: re.Pattern = field(compare=False)

    @classmethod
    def parse(cls, text: str) -> Pattern:
        if len(text) >= 2 and text.startswith("/") and text.endswith("/"):
            body = text[1:-1]
        else:
            body = fnmatch.translate(text)
        try:
            return cls(text=text, regex=re.compile(body, flags=re.IGNORECASE))
        except re.error as e:
            raise ValueError(f"invalid pattern {text!r}: {e}") from e

    @property
    def is_literal(self) -> bool:
        """True for a plain name with no wildcard (used to warn about rules that match nothing)."""
        return not (self.text.startswith("/") or any(ch in self.text for ch in "*?["))

    def matches(self, name: str) -> bool:
        if self.text.startswith("/"):
            return self.regex.search(name) is not None
        return self.regex.fullmatch(name) is not None


class PatternSet:
    def __init__(self, patterns: list[str] | None) -> None:
        self.patterns = [Pattern.parse(text=p) for p in (patterns or [])]

    def __bool__(self) -> bool:
        return bool(self.patterns)

    def matches(self, name: str) -> bool:
        return any(p.matches(name=name) for p in self.patterns)

    def first_match(self, name: str) -> Pattern | None:
        return next((p for p in self.patterns if p.matches(name=name)), None)


class TablePatternSet(PatternSet):
    """SCHEMA.TABLE patterns; a glob without a dot matches the table name in any schema."""

    def matches_table(self, schema: str, table: str) -> bool:
        for p in self.patterns:
            if p.text.startswith("/") or "." in p.text:
                if p.matches(name=f"{schema}.{table}"):
                    return True
            elif p.matches(name=table):
                return True
        return False


def apply_case(name: str, case: str) -> str:
    if case == "lower":
        return name.lower()
    if case == "upper":
        return name.upper()
    return name
