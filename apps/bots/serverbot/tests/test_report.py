from serverbot.handlers import format_report_messages


def test_report_is_escaped_and_split_before_sending():
    report = "<service>&" * 800
    messages = format_report_messages(report)
    assert len(messages) > 1
    for text in messages:
        assert text.startswith("<pre>") and text.endswith("</pre>")
        assert "<service>" not in text
    from html import unescape
    assert "".join(unescape(text[5:-6]) for text in messages) == report
