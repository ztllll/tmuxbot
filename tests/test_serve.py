from tmuxbot.__main__ import build_parser
from tmuxbot.serve import browser_url


def test_cli_exposes_serve_and_doctor() -> None:
    parser = build_parser()
    assert parser.parse_args(["serve"]).command == "serve"
    assert parser.parse_args(["serve", "--open"]).open_browser is True
    assert parser.parse_args(["doctor", "--json"]).as_json is True


def test_browser_url_uses_loopback_and_fragment_grant() -> None:
    assert browser_url("0.0.0.0", 8765, "abc") == "http://127.0.0.1:8765/#grant=abc"
    assert "?" not in browser_url("127.0.0.1", 8765, "abc")

