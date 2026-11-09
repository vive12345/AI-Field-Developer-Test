"""Tests for PII detection and the incremental StreamingRedactor."""

import random

import pytest

from llm_gateway.patterns import REDACTED, redact_text
from llm_gateway.redactor import StreamingRedactor

# --- detection rules ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "john@example.com",
        "jane.doe+tag@sub.acme-corp.co.uk",
        "123-45-6789",
        "4111 1111 1111 1111",  # Visa, spaces
        "5500-0000-0000-0004",  # Mastercard, dashes
        "378282246310005",  # Amex, no separators
    ],
)
def test_sensitive_values_are_redacted(text):
    assert redact_text(f"value: {text}.") == f"value: {REDACTED}."


@pytest.mark.parametrize(
    "text",
    [
        "1234567890123",  # long number failing the Luhn check (e.g. an order ID)
        "123-45-67890",  # SSN-like but too long
        "A123-45-6789",  # SSN-like but glued to a word
        "x4111111111111111",  # card-like but glued to a word
        "not an email@",
        "2024-01-15",
    ],
)
def test_non_sensitive_values_are_kept(text):
    assert redact_text(text) == text


# --- streaming == full-text redaction, for every possible chunking -----------

SAMPLES = [
    "Contact john.doe@example.com or call. SSN 123-45-6789, card 4111 1111 1111 1111 ok.",
    "Email a@b.co.uk and SSN 123-45-67890 (not ssn) and A123-45-6789.",
    "Order 1234567890123 is fine; 5500-0000-0000-0004! ssn:078-05-1120 end",
    "Ends with an email bob@corp.io",
    "No PII at all, just a friendly sentence.",
]


def stream_through(chunks: list[str]) -> str:
    redactor = StreamingRedactor()
    output = "".join(redactor.feed(chunk) for chunk in chunks)
    return output + redactor.flush()


def random_chunkings(text: str, count: int, seed: int = 0) -> list[list[str]]:
    rng = random.Random(seed)
    chunkings = []
    for _ in range(count):
        cuts = sorted(rng.sample(range(len(text) + 1), k=rng.randint(0, min(20, len(text)))))
        bounds = [0, *cuts, len(text)]
        chunkings.append([text[a:b] for a, b in zip(bounds, bounds[1:])])
    return chunkings


@pytest.mark.parametrize("text", SAMPLES)
def test_every_two_way_split_matches_full_redaction(text):
    expected = redact_text(text)
    for split in range(len(text) + 1):
        assert stream_through([text[:split], text[split:]]) == expected, f"split at {split}"


@pytest.mark.parametrize("text", SAMPLES)
def test_random_chunkings_match_full_redaction(text):
    expected = redact_text(text)
    for chunks in random_chunkings(text, count=300):
        assert stream_through(chunks) == expected, chunks


@pytest.mark.parametrize("text", SAMPLES)
def test_character_by_character_matches_full_redaction(text):
    assert stream_through(list(text)) == redact_text(text)


def test_first_half_of_pii_is_never_emitted():
    redactor = StreamingRedactor()

    emitted = redactor.feed("Write to john@ex")
    emitted += redactor.feed("ample.com today")

    assert "john" not in emitted
    assert emitted == f"Write to {REDACTED} "


# --- latency and memory -------------------------------------------------------


def test_plain_text_is_released_immediately_except_last_word():
    redactor = StreamingRedactor()

    assert redactor.feed("The weather is nice tod") == "The weather is nice "
    assert redactor.held_back == len("tod")


def test_held_back_buffer_stays_bounded_on_long_streams():
    rng = random.Random(42)
    alphabet = "abcdefghij0123456789 -.@"
    text = "".join(rng.choice(alphabet) for _ in range(100_000))

    redactor = StreamingRedactor()
    max_held = 0
    for start in range(0, len(text), 7):
        redactor.feed(text[start : start + 7])
        max_held = max(max_held, redactor.held_back)

    assert max_held < 400  # bounded by the longest possible match, not by response length
