"""Redact complete lexical candidates while bounding retained text."""

import re

# Supported ASCII email grammar and deliberately conservative numeric patterns
ATOM = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.!#$%&'*+/=?^_`{|}~@-"
)
PATTERNS = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}"
    r"|(?<![0-9])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![0-9])"
    r"|(?<![0-9])(?:[0-9][ \t\r\n-]*){12,18}[0-9](?![0-9])"
)


class Redactor:
    """Withhold unfinished candidates and fail closed when one exceeds the bound."""

    def __init__(self, limit: int = 512):
        """Limit candidate memory independently of response and provider chunk size."""
        if limit < 32:
            raise ValueError("Candidate limit must be at least 32")
        self.limit = limit
        self.buffer = ""
        self.mode = ""
        self.discard = False
        self.has_space = False
        self.max_buffered = 0

    def _append(self, char: str) -> str:
        """Emit one marker before suppressing an oversized unfinished candidate."""
        self.has_space |= char.isspace()
        if self.discard:
            return ""
        if len(self.buffer) == self.limit:
            self.buffer = ""
            self.discard = True
            return "[REDACTED]"
        self.buffer += char
        self.max_buffered = max(self.max_buffered, len(self.buffer))
        return ""

    def finish(self) -> str:
        """Release only a completed candidate, including the final token at EOF."""
        result = "" if self.discard else PATTERNS.sub("[REDACTED]", self.buffer)
        self.buffer = ""
        self.mode = ""
        self.discard = False
        self.has_space = False
        return result

    def feed(self, text: str) -> str:
        """Preserve safe delimiters and never release a possibly partial pattern."""
        output = []
        for char in text:
            if not self.mode:
                if char in ATOM:
                    self.mode = "number" if char in "0123456789" else "atom"
                    output.append(self._append(char))
                else:
                    output.append(char)
            elif self.mode == "atom":
                if char in ATOM:
                    output.append(self._append(char))
                else:
                    output.extend((self.finish(), char))
            elif char in "0123456789- \t\r\n":
                output.append(self._append(char))
            elif char in ATOM:
                if self.has_space:
                    output.append(self.finish())
                self.mode = "atom"
                output.append(self._append(char))
            else:
                output.extend((self.finish(), char))
        return "".join(output)
