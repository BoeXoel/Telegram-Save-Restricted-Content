from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from app.config import ContentFilterConfig


@dataclass(frozen=True)
class FilterMatch:
    """A privacy-safe description of a configured content-filter match."""

    rule_type: str
    rule_number: int

    @property
    def reason_code(self) -> str:
        return "ad_filtered"

    @property
    def reason(self) -> str:
        return f"Skipped by {self.rule_type} filter rule #{self.rule_number}"


class ContentFilter:
    """Match text bodies and captions without retaining their content."""

    def __init__(self, config: ContentFilterConfig) -> None:
        self.enabled = config.enabled
        self.case_sensitive = config.case_sensitive
        self._keywords = config.keywords
        flags = 0 if config.case_sensitive else re.IGNORECASE
        self._regex = tuple(re.compile(pattern, flags) for pattern in config.regex)

    def match_texts(self, texts: Iterable[str | None]) -> FilterMatch | None:
        if not self.enabled:
            return None
        for text in texts:
            match = self.match_text(text)
            if match:
                return match
        return None

    def match_text(self, text: str | None) -> FilterMatch | None:
        if not self.enabled or not text:
            return None

        haystack = text if self.case_sensitive else text.casefold()
        for index, keyword in enumerate(self._keywords, start=1):
            needle = keyword if self.case_sensitive else keyword.casefold()
            if needle in haystack:
                return FilterMatch("keyword", index)

        for index, pattern in enumerate(self._regex, start=1):
            if pattern.search(text):
                return FilterMatch("regex", index)
        return None
