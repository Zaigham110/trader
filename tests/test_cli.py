from pathlib import Path

from typer.testing import CliRunner

from goldtrader.cli import app

runner = CliRunner()


def test_config_check_and_db_init(tmp_path: Path):
    r = runner.invoke(app, ["config-check", "config/demo.yaml"])
    assert r.exit_code == 0 and "config_hash" in r.output
    r = runner.invoke(app, ["db-init", "--db", str(tmp_path / "t.sqlite")])
    assert r.exit_code == 0 and "schema version" in r.output


def test_synth_backtest_sweep_round_trip(tmp_path: Path):
    bars = tmp_path / "bars.parquet"
    r = runner.invoke(app, ["synth-data", "--out", str(bars), "--days", "12", "--seed", "5"])
    assert r.exit_code == 0 and "snapshot_id=" in r.output
    r = runner.invoke(
        app,
        [
            "backtest",
            "--data",
            str(bars),
            "--out",
            str(tmp_path / "rep"),
            "--db",
            str(tmp_path / "r.sqlite"),
        ],
    )
    assert r.exit_code == 0, r.output
    assert "Offline gate" in r.output and list((tmp_path / "rep").glob("*.json"))
    r = runner.invoke(
        app,
        [
            "sweep",
            "--data",
            str(bars),
            "--grid",
            "stop_atr=0.8,1.2;target_atr=1.5",
            "--db",
            str(tmp_path / "r.sqlite"),
            "--out",
            str(tmp_path / "sw"),
        ],
    )
    assert r.exit_code == 0, r.output
    assert "2 runs recorded" in r.output and "ledger size 3" in r.output  # 1 baseline + 2 sweep


def test_mt5_tests_and_spec_dump_fake(tmp_path: Path):
    r = runner.invoke(app, ["mt5-tests", "--fake", "--trade", "--out", str(tmp_path / "mt5.md")])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "mt5.md").exists() and "T03" in r.output
    r = runner.invoke(app, ["spec-dump", "--fake", "--db", str(tmp_path / "s.sqlite")])
    assert r.exit_code == 0, r.output
    assert "trade_contract_size" in r.output


def test_gateway_command_runs_briefly_with_fake(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GOLDTRADER_GATEWAY_TOKEN", "x" * 32)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f"data:\n  db_dir: {tmp_path / 'state'}\nsession:\n  dst_mode: winter\n")
    r = runner.invoke(
        app,
        [
            "gateway",
            "--fake",
            "--config",
            str(cfg),
            "--db",
            str(tmp_path / "g.sqlite"),
            "--port",
            "0",
            "--minutes",
            "0.02",
        ],
    )
    assert r.exit_code == 0, r.output
    assert "gateway up: symbol=XAUUSDm" in r.output


def test_status_command_reads_db_without_gateway(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GOLDTRADER_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("GATEWAY_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["status", "--db", str(tmp_path / "s.sqlite")])
    assert r.exit_code == 0, r.output
    assert "open incidents: 0" in r.output and "trades today: 0" in r.output
