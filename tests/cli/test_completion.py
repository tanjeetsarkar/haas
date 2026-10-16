from __future__ import annotations

from haas_cli.completion import common_prefix, suggest

SKILLS = ["github-ops", "weather", "web-research"]


def values(text, **kw):
    c = suggest(text, skills=SKILLS, **kw)
    return c.start, [i.value for i in c.items]


def test_command_completion_in_help_order_with_arg_spaces():
    start, vals = values("/")
    assert start == 0 and vals[0] == "/help" and "/exit" in vals and "/preload " in vals
    assert values("/s")[1] == ["/status", "/skills", "/secret ", "/steps "]
    assert values("/new")[1] == ["/new"]  # no args -> no trailing space
    assert values("/NEW")[1] == ["/new"]
    assert values("/zzz")[1] == []


def test_non_commands_get_no_suggestions():
    assert values("hello")[1] == [] and values("")[1] == []
    assert values("/status\nmore")[1] == []
    assert values("/steps 5")[1] == []


def test_preload_completes_skill_names_excluding_taken():
    assert values("/preload ") == (9, [*SKILLS, "clear"])
    assert values("/preload we") == (9, ["weather", "web-research"])
    assert values("/preload w")[1] == ["weather", "web-research"]
    assert values("/preload weather ") == (17, ["github-ops", "web-research"])
    assert values("/preload weather g")[1] == ["github-ops"]
    assert "clear" not in values("/preload weather ")[1]


def test_secret_subcommands_and_names():
    assert values("/secret ")[1] == ["set", "list", "unset"]
    assert values("/secret u")[1] == ["unset"]
    assert values("/secret set ")[1] == []  # NAME is free text
    assert (
        suggest("/secret unset G", secrets=["GITHUB_TOKEN", "BRAVE"]).items[0].value
        == "GITHUB_TOKEN"
    )
    assert suggest("/secret unset ", secrets=["A", "B"]).start == 14


def test_common_prefix():
    assert common_prefix(["/status", "/steps "]) == "/st"
    assert common_prefix([]) == ""
