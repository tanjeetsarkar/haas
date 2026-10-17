from haas_cli.tui.safety import sanitize_terminal_text


def test_sanitize_removes_terminal_sequences_and_controls():
    value = "normal\x1b[31m red\x1b[0m\x1b]0;title\x07\nnext\x00"
    assert sanitize_terminal_text(value) == "normal red\nnext"


def test_sanitize_bounds_rendered_text():
    rendered = sanitize_terminal_text("x" * 200, limit=80)
    assert len(rendered) <= 80
    assert rendered.endswith("full content remains on the server]")
    assert len(sanitize_terminal_text("x" * 5, limit=2)) == 2
