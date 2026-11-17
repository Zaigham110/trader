def test_inspect_secret_flags_paste_failures():
    from goldtrader.secrets import inspect_secret

    assert inspect_secret(None) == ("absent", [])
    assert inspect_secret("sk-ant-api03-" + "a" * 80)[1] == []
    _, problems = inspect_secret("\x16")  # Ctrl+V typed into a Windows hidden prompt
    assert any("control" in p for p in problems) and any("short" in p for p in problems)
    assert any("quotes" in p for p in inspect_secret('"abc123"')[1])
    assert any("OAuth" in p for p in inspect_secret("sk-ant-oat01-xyz")[1])
    assert any("whitespace" in p for p in inspect_secret("abc123 \n")[1])
