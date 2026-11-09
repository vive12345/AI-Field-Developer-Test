"""Incremental PII redaction over a text stream that arrives in arbitrary chunks.

The challenge: PII can be split across chunks ("john@ex" + "ample.com").
Emitting each chunk as-is would leak the first half before we know it is PII;
buffering the whole response would destroy time-to-first-token.

The approach: after each chunk, emit everything that is provably final and
hold back only the tail that could still become (part of) a match:

- `regex` partial matching reports a match that ran into the end of the
  buffer and could continue with more input ("john@ex" -> partial email).
- A complete match can also still grow when everything from its start to the
  end of the buffer is a valid prefix of a longer match:
  "123-45-6789" -> "123-45-67890" (no longer an SSN), or
  "a@b.co."     -> "a@b.co.uk"    (a longer email). Such matches are held too.

Because every pattern has a bounded length, the held-back tail is bounded as
well: memory is O(longest possible match), independent of response length.
"""

from llm_gateway.patterns import PII_PATTERN, REDACTED, is_sensitive


class StreamingRedactor:
    def __init__(self) -> None:
        self._pending = ""
        # Last character already emitted. Kept as lookbehind context so word
        # boundaries (\b) are evaluated exactly as they would be on the full text.
        self._context = ""

    @property
    def held_back(self) -> int:
        """Number of characters currently withheld from the client."""
        return len(self._pending)

    def feed(self, chunk: str) -> str:
        """Add a chunk; return the text that is now safe to send."""
        self._pending += chunk
        return self._drain(final=False)

    def flush(self) -> str:
        """End of stream: nothing more can arrive, so release everything (redacted)."""
        return self._drain(final=True)

    def _drain(self, final: bool) -> str:
        text = self._context + self._pending
        start = len(self._context)

        output: list[str] = []
        cursor = start
        hold_from = len(text)

        for match in PII_PATTERN.finditer(text, pos=start, partial=not final):
            if not final and self._could_still_grow(text, match):
                hold_from = match.start()
                break
            if is_sensitive(match):
                output.extend((text[cursor : match.start()], REDACTED))
                cursor = match.end()

        output.append(text[cursor:hold_from])
        emitted = "".join(output)

        self._pending = text[hold_from:]
        if hold_from > start:
            self._context = text[hold_from - 1]
        return emitted

    @staticmethod
    def _could_still_grow(text: str, match) -> bool:
        if match.partial:
            return True
        # Is text[match.start():] a prefix of some (longer) match? fullmatch with
        # partial=True answers exactly that, and fails fast on the first character
        # that cannot continue a match (usually the space right after it).
        return PII_PATTERN.fullmatch(text, pos=match.start(), partial=True) is not None
