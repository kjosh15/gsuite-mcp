"""Pure heuristics for stripping quoted history from email message bodies.

Detection is deliberately conservative: when no quote boundary is found — or
when stripping would leave only whitespace — the original text is returned
unchanged (prefer keeping net-new content over dropping it).
"""

import re

# Ordered earliest-match wins; each marks the start of quoted history.
_MARKERS = [
    re.compile(r"^On .+wrote:\s*$", re.MULTILINE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^-{3,}\s*Forwarded message\s*-{3,}\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^>", re.MULTILINE),
]

_GMAIL_QUOTE_DIV = re.compile(r'<div[^>]*class="[^"]*gmail_quote[^"]*"', re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_ENTITIES = (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'))


def html_to_text(html: str) -> str:
    """Minimal HTML→text: cut at the gmail_quote container, then strip tags."""
    m = _GMAIL_QUOTE_DIV.search(html)
    if m:
        html = html[: m.start()]
    text = _TAG.sub("", html)
    for ent, ch in _ENTITIES:
        text = text.replace(ent, ch)
    return text


def strip_quoted_history(text: str) -> tuple[str, bool]:
    """Return ``(net_new_text, stripped)``.

    Cuts at the earliest recognized quote boundary. Returns the original text
    with ``stripped=False`` when no boundary is found or when stripping would
    leave only whitespace.
    """
    earliest: int | None = None
    for marker in _MARKERS:
        m = marker.search(text)
        if m and (earliest is None or m.start() < earliest):
            earliest = m.start()
    if earliest is None:
        return text, False
    net_new = text[:earliest].rstrip()
    if not net_new.strip():
        return text, False
    return net_new, True
