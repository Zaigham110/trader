"""Two instances side by side: demo and live keep separate secrets and terminals (D-081)."""

from goldtrader.config.loader import parse_config
from goldtrader.secrets import KNOWN, SCOPED, scoped


def test_a_live_config_reads_only_its_own_secret_names():
    for name in SCOPED:
        assert scoped(name, "demo") == name
        assert scoped(name, "live") == f"{name}_LIVE" and f"{name}_LIVE" in KNOWN
    # shared between instances: the AI key, the owner's chat id, the go-live phrase
    for name in ("ANTHROPIC_API_KEY", "TELEGRAM_OWNER_CHAT_ID", "ALLOW_LIVE"):
        assert scoped(name, "live") == name


def test_cli_client_uses_the_instance_secrets_and_terminal(monkeypatch):
    import goldtrader.mt5.real as real
    from goldtrader import cli

    calls: list[dict] = []

    class Stub:
        def initialize(self, **kw):
            calls.append(kw)
            return True

    monkeypatch.setattr(real, "RealMT5", Stub)
    for k, v in {
        "GOLDTRADER_MT5_LOGIN": "111",
        "GOLDTRADER_MT5_PASSWORD": "demo-pw",
        "GOLDTRADER_MT5_SERVER": "Demo-Server",
        "GOLDTRADER_MT5_LOGIN_LIVE": "222",
        "GOLDTRADER_MT5_PASSWORD_LIVE": "live-pw",
        "GOLDTRADER_MT5_SERVER_LIVE": "Real-Server",
    }.items():
        monkeypatch.setenv(k, v)
    demo = parse_config({})
    live = parse_config(
        {
            "account": {"environment": "live", "terminal_path": "C:\\MT5 Live\\terminal64.exe"},
            "owner": {"mode": "live"},
        }
    )
    cli._client(False, demo)
    cli._client(False, live)
    assert calls[0] == {"login": 111, "password": "demo-pw", "server": "Demo-Server"}
    assert calls[1] == {
        "path": "C:\\MT5 Live\\terminal64.exe",
        "login": 222,
        "password": "live-pw",
        "server": "Real-Server",
    }
    # without its own login the live instance does not borrow the demo one
    for k in ("LOGIN", "PASSWORD", "SERVER"):
        monkeypatch.delenv(f"GOLDTRADER_MT5_{k}_LIVE")
    cli._client(False, live)
    assert calls[2] == {"path": "C:\\MT5 Live\\terminal64.exe"}


def test_a_bare_reinitialise_reaches_the_same_terminal(monkeypatch):
    import sys
    import types

    seen: list[tuple] = []
    fake = types.SimpleNamespace(initialize=lambda *a, **kw: seen.append((a, kw)) or True)
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    from goldtrader.mt5.real import RealMT5

    c = RealMT5()
    c.initialize(path="C:\\MT5 Live\\terminal64.exe", login=222, password="pw", server="Real")
    c.initialize()  # what GatewayService.start() does
    assert seen[0] == seen[1]
    assert seen[1][0] == ("C:\\MT5 Live\\terminal64.exe",) and seen[1][1]["login"] == 222
