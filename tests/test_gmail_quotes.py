"""Tests for quote-stripping heuristics."""

from gsuite_mcp import gmail_quotes


def test_strips_on_wrote_attribution():
    text = (
        "Sounds good, let's ship it.\n\n"
        "On Mon, Jul 1, 2026 at 3:04 PM Alice <a@x.com> wrote:\n"
        "> the original question\n"
    )
    net, stripped = gmail_quotes.strip_quoted_history(text)
    assert stripped is True
    assert net == "Sounds good, let's ship it."


def test_strips_original_message_separator():
    text = "My reply.\n\n-----Original Message-----\nFrom: Bob\nOlder stuff"
    net, stripped = gmail_quotes.strip_quoted_history(text)
    assert stripped is True
    assert net == "My reply."


def test_strips_bare_quote_block():
    text = "New content here.\n> quoted line one\n> quoted line two\n"
    net, stripped = gmail_quotes.strip_quoted_history(text)
    assert stripped is True
    assert net == "New content here."


def test_no_boundary_keeps_full_text():
    text = "Just a plain message with no quoting at all.\nSecond line."
    net, stripped = gmail_quotes.strip_quoted_history(text)
    assert stripped is False
    assert net == text


def test_all_quote_keeps_text_prefer_keep():
    # Stripping would leave nothing -> prefer keep (stripped=False)
    text = "> only quoted content\n> nothing new\n"
    net, stripped = gmail_quotes.strip_quoted_history(text)
    assert stripped is False
    assert net == text


def test_html_to_text_cuts_at_gmail_quote():
    html = '<div>Fresh reply</div><div class="gmail_quote">old quoted stuff</div>'
    assert gmail_quotes.html_to_text(html).strip() == "Fresh reply"


def test_html_to_text_strips_tags_and_entities():
    html = "<p>Hello&nbsp;&amp; welcome</p>"
    assert gmail_quotes.html_to_text(html).strip() == "Hello & welcome"


def test_strips_forwarded_message_marker():
    text = (
        "My note.\n\n"
        "---------- Forwarded message ---------\n"
        "From: Bob\n"
        "older"
    )
    net, stripped = gmail_quotes.strip_quoted_history(text)
    assert stripped is True
    assert net == "My note."
