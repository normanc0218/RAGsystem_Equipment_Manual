"""
Stage 2 template detection — no LLM, no API calls.

Uses two cheap signals on the email snippet + subject:
  1. Shannon entropy on word distribution  — low = repetitive/template vocabulary
  2. gzip compression ratio               — low = highly repetitive content

Both signals are computed in pure Python in microseconds.
"""
import gzip
import math
import re
from collections import Counter

_WORD_RE = re.compile(r'\b[a-z]{2,}\b')

# Statistical thresholds — both must trigger to avoid false positives on short text
ENTROPY_THRESHOLD = 3.0        # bits; very low = near-zero vocabulary variety
COMPRESSION_THRESHOLD = 0.60   # ratio; very low = near-identical repeated content
MIN_WORDS = 15                 # skip statistical checks below this word count

# Keyword patterns reliable on short text (subject / snippet)
_TEMPLATE_SUBJECT_RE = re.compile(
    r'\b(unsubscribe|% off|% sale|flash sale|limited.?time|deal of|'
    r'your (order|invoice|receipt|shipment|delivery|subscription|account|password)|'
    r'order (confirmed|shipped|delivered|cancelled)|'
    r'invoice #|receipt #|payment (received|due|failed)|'
    r'security alert|verify your|confirm your|reset your password|'
    r'you have been (invited|added|removed))\b',
    re.IGNORECASE,
)


def _word_entropy(text: str) -> float:
    words = _WORD_RE.findall(text.lower())
    if len(words) < MIN_WORDS:
        return 99.0  # not enough words — don't flag
    counts = Counter(words)
    total = len(words)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def _compression_ratio(text: str) -> float:
    encoded = text.encode("utf-8")
    if len(encoded) < 40:
        return 1.0
    return len(gzip.compress(encoded, compresslevel=9)) / len(encoded)


def is_template_email(snippet: str, subject: str = "") -> bool:
    """Return True if the email looks like a template or automated message."""
    text = f"{subject} {snippet}".strip()
    if not text or len(text) < 20:
        return False
    # Fast keyword check — reliable on short text
    if _TEMPLATE_SUBJECT_RE.search(text):
        return True
    # Statistical check — only meaningful on longer text (both signals must fire)
    return (
        _word_entropy(text) < ENTROPY_THRESHOLD
        and _compression_ratio(text) < COMPRESSION_THRESHOLD
    )


def template_score(snippet: str, subject: str = "") -> dict:
    """Return raw scores for debugging/tuning."""
    text = f"{subject} {snippet}".strip()
    return {
        "entropy": round(_word_entropy(text), 3),
        "compression_ratio": round(_compression_ratio(text), 3),
        "is_template": is_template_email(snippet, subject),
    }
