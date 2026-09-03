"""Normalize provider-only response markers before they reach chat content."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


SEARCH_DIRECTIVE_PREFIX = "search("
MAX_SEARCH_DIRECTIVE_CHARS = 8_192

# The official-web gateway currently emits search metadata as assistant text:
# search("<JSON string>")slow|<display query>|1\n
# Keep parsing transport-specific metadata here rather than teaching the UI
# about every raw provider fragment.
_SEARCH_DIRECTIVE = re.compile(
    r'^search\((?P<input>"(?:\\.|[^"\\])*")\)'
    r'(?P<mode>[A-Za-z][A-Za-z0-9_-]*)\|'
    r'(?P<query>[^|\r\n]{1,2000})(?:\|(?P<index>\d+))?'
    r'(?P<ending>\r?\n)',
)
_SEARCH_DIRECTIVE_AT_END = re.compile(
    r'^search\((?P<input>"(?:\\.|[^"\\])*")\)'
    r'(?P<mode>[A-Za-z][A-Za-z0-9_-]*)\|'
    r'(?P<query>[^|\r\n]{1,2000})\|(?P<index>\d+)$',
)
_OUTER_MARKDOWN_FENCE = re.compile(
    r"\A[ \t]*```(?:markdown|md)[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```[ \t]*\Z",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SearchActivity:
    query: str
    original_query: str
    mode: str
    index: int


def _activity(match: re.Match[str]) -> SearchActivity | None:
    try:
        original_query = json.loads(match.group("input"))
    except (TypeError, json.JSONDecodeError):
        return None
    query = match.group("query").strip()
    mode = match.group("mode").lower()
    # The gateway has emitted both `|QUERY|1` and `|QUERY` variants in real
    # streams. Treat an omitted index as the first search activity.
    index = int(match.group("index") or 1)
    if not isinstance(original_query, str) or not original_query.strip():
        return None
    if not query or mode not in {"fast", "slow"} or not 1 <= index <= 1000:
        return None
    return SearchActivity(
        query=query,
        original_query=original_query.strip(),
        mode=mode,
        index=index,
    )


@dataclass
class SearchDirectiveStreamParser:
    """Strip leading search directives across arbitrary stream boundaries.

    Parsing is limited to the beginning of an assistant turn. Once ordinary
    response text starts, text such as ``search("term")`` is left untouched.
    Malformed or oversized candidates fail open so provider content is never
    silently discarded.
    """

    pending: str = ""
    accepting_directives: bool = True
    activities: list[SearchActivity] = field(default_factory=list)

    def feed(self, value: str) -> tuple[list[str], list[SearchActivity]]:
        self.pending += str(value or "")
        visible: list[str] = []
        discovered: list[SearchActivity] = []

        while self.pending:
            if not self.accepting_directives:
                visible.append(self.pending)
                self.pending = ""
                break

            match = _SEARCH_DIRECTIVE.match(self.pending)
            if match:
                activity = _activity(match)
                if activity is None or match.end() > MAX_SEARCH_DIRECTIVE_CHARS:
                    self.accepting_directives = False
                    visible.append(self.pending)
                    self.pending = ""
                    break
                self.activities.append(activity)
                discovered.append(activity)
                self.pending = self.pending[match.end():]
                continue

            # A short prefix, or a directive whose closing metadata has not
            # arrived yet, must remain buffered until the next provider chunk.
            if SEARCH_DIRECTIVE_PREFIX.startswith(self.pending):
                break
            if self.pending.startswith(SEARCH_DIRECTIVE_PREFIX):
                # A trailing CR may be the first half of a CRLF delimiter.
                has_decisive_line_break = "\n" in self.pending or "\r" in self.pending[:-1]
                if len(self.pending) <= MAX_SEARCH_DIRECTIVE_CHARS and not has_decisive_line_break:
                    break

            self.accepting_directives = False
            visible.append(self.pending)
            self.pending = ""

        return [item for item in visible if item], discovered

    def finish(self) -> tuple[list[str], list[SearchActivity]]:
        discovered: list[SearchActivity] = []
        if self.accepting_directives and self.pending:
            match = _SEARCH_DIRECTIVE_AT_END.fullmatch(self.pending)
            if match:
                activity = _activity(match)
                if activity is not None and match.end() <= MAX_SEARCH_DIRECTIVE_CHARS:
                    self.activities.append(activity)
                    discovered.append(activity)
                    self.pending = ""
        visible = [self.pending] if self.pending else []
        self.pending = ""
        self.accepting_directives = False
        return visible, discovered


def strip_search_directives(value: str) -> tuple[str, list[SearchActivity]]:
    """Clean a complete response, including historical persisted messages."""
    parser = SearchDirectiveStreamParser()
    visible, activities = parser.feed(value)
    tail, final_activities = parser.finish()
    return "".join([*visible, *tail]), [*activities, *final_activities]


def normalize_assistant_content(value: str) -> tuple[str, list[SearchActivity]]:
    """Remove transport markers and a redundant whole-response Markdown fence."""
    content, activities = strip_search_directives(value)
    fenced = _OUTER_MARKDOWN_FENCE.fullmatch(content)
    if fenced:
        content = fenced.group("body")
    return content, activities
