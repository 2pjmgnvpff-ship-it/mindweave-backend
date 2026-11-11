"""XSS-safe transcript HTML highlighter and natural slip formatting (pure domain)."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable


def build_transcript_html(raw_transcript: str, matched_names: Iterable[str]) -> str:
    """Generate XSS-safe `transcriptHtml` wrapping matched person names in `<mark>...</mark>`.

    All `<`, `>`, `&`, `"`, and `'` characters in BOTH the raw transcript and any matched
    names are HTML-escaped so arbitrary user/ASR text can never inject HTML or scripts.
    """
    if not raw_transcript:
        return ""

    unique_names = sorted(
        {name.strip() for name in matched_names if name and name.strip()},
        key=len,
        reverse=True,
    )
    if not unique_names:
        return html.escape(raw_transcript, quote=True)

    pattern = re.compile("|".join(re.escape(name) for name in unique_names))
    chunks: list[str] = []
    cursor = 0

    for match in pattern.finditer(raw_transcript):
        start, end = match.span()
        if start > cursor:
            chunks.append(html.escape(raw_transcript[cursor:start], quote=True))
        escaped_name = html.escape(match.group(0), quote=True)
        chunks.append(f"<mark>{escaped_name}</mark>")
        cursor = end

    if cursor < len(raw_transcript):
        chunks.append(html.escape(raw_transcript[cursor:], quote=True))

    return "".join(chunks)


def format_duration_mmss(duration_ms: int | None) -> str:
    """Format duration in milliseconds as V10 `m:ss` (e.g. `0:04`, `0:18`, `1:05`)."""
    if duration_ms is None or duration_ms <= 0:
        return "0:00"
    total_sec = max(1, round(duration_ms / 1000))
    minutes, seconds = divmod(total_sec, 60)
    return f"{minutes}:{seconds:02d}"


def build_natural_slip_tag(candidate_title: str, fallback_body: str) -> str:
    """Derive a concise, natural journal slip tag without developer jargon (Invariant I6)."""
    cleaned_title = candidate_title.strip()
    if cleaned_title:
        return cleaned_title[:36]
    compact = " ".join(fallback_body.strip().split())
    if not compact:
        return "随手手札"
    # Take first clause up to 18 chars so it reads like a natural handwritten note header
    first_clause = re.split(r"[，。；！？,.;!?\n]", compact, maxsplit=1)[0].strip()
    target = first_clause or compact
    return target[:18] if len(target) <= 18 else f"{target[:17]}…"
