# Phase 7 runbook — demo trading on the owner's PC

Status: started 2026-11-05 (D-051). Duration: 4–8 weeks of demo trading, then the performance
report and the owner's decision on Phase 8. Nothing in this phase trades real money.

## 0. What you need

Mac users (D-053): every command below runs **on the Windows machine**, not on the Mac. Install
Microsoft Remote Desktop from the Mac App Store, connect to the Windows machine, and work in a
PowerShell window there. Telegram works from your phone or Mac as usual. To send me files, copy
them from the Windows machine (Remote Desktop folder redirection, or commit `reports/` to a branch
of this repository with `git add reports && git commit && git push`; `state/` is git-ignored).

- Windows machine (PC or VPS) that stays on during gold hours (Sunday 23:05 to Friday 21:58 UTC), MT5 installed from
  Exness and logged into the **demo** account once, *Allow algorithmic trading* enabled.
- Your Anthropic API key, a Telegram bot token (from @BotFather) and your Telegram chat id.
- Git, `uv` (https://docs.astral.sh/uv/) and this repository cloned.

The complete, step-by-step Windows installation is in `WINDOWS_SETUP.md`; the sections below
summarise the same commands.

## 1. Install (once)

```powershell
cd <repo>
uv venv .venv
uv pip install -e ".[dev,windows]"
```

## 2. Secrets (once, never in files)

Use the credential store (preferred) or set the variables in the Scheduled Task environment.

```powershell
.venv\Scripts\goldctl secret-set MT5_LOGIN
.venv\Scripts\goldctl secret-set MT5_PASSWORD
.venv\Scripts\goldctl secret-set MT5_SERVER
.venv\Scripts\goldctl secret-set ANTHROPIC_API_KEY
.venv\Scripts\goldctl secret-set TELEGRAM_BOT_TOKEN
.venv\Scripts\goldctl secret-set TELEGRAM_OWNER_CHAT_ID
.venv\Scripts\goldctl secret-set GATEWAY_TOKEN
```

`GATEWAY_TOKEN` is any random string of at least 32 characters (both processes read it).

## 3. Preflight

```powershell
.venv\Scripts\goldctl doctor --config config\demo.yaml --db state\demo.sqlite
.venv\Scripts\goldctl llm-check
.venv\Scripts\goldctl mt5-tests --real --no-trade --out reports\mt5\readonly.md
.venv\Scripts\goldctl mt5-tests --real --trade --out reports\mt5\trade.md
.venv\Scripts\goldctl spec-dump --real --db state\demo.sqlite
```

Send me `reports\mt5\readonly.md`, `reports\mt5\trade.md` and the `spec-dump` output. I fold the
verified contract values into `MT5_EXNESS_INTEGRATION.md` and close KU-001, KU-003, KU-012,
KU-019 and KU-022.

## 4. Data warm-up (once)

Download at least one month of XAUUSD tick history from the Personal Area (Tools & Services →
Tick History) and import it so the features have history on day one:

```powershell
.venv\Scripts\goldctl import-ticks <downloaded.csv> --out data\bars\exness_1m.parquet
.venv\Scripts\goldctl backtest --data data\bars\exness_1m.parquet --db state\research.sqlite
```

Send me the backtest report from `reports\backtests\`.

## 5. Start the processes

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install-tasks.ps1 -Provider anthropic
Start-ScheduledTask goldtrader-gateway
Start-Sleep 15
Start-ScheduledTask goldtrader-brain
.venv\Scripts\goldctl status
```

In Telegram send `/status` to the bot. You should get a code-generated summary within a few
seconds. Then ask it something in plain language, for example "what is the risk setting for this
account?" and confirm the answer carries a `source:` line.

## 6. Daily (two minutes)

- `goldctl status` or Telegram `/status`.
- Read any alert. A critical alert (kill switch, stop missing, spec change) needs your acknowledgement
  in Telegram and a message to me with the text.

## 7. Weekly (five minutes)

```powershell
.venv\Scripts\goldctl report --period week
.venv\Scripts\goldctl learn status
powershell -ExecutionPolicy Bypass -File scripts\windows\backup-db.ps1
```

Send me the report file from `reports\phase7\` and, if you can, a copy of `state\demo.sqlite`
(it contains no secrets). I review the journals, costs, slippage and the learning pipeline, and
answer with findings and any config change proposal (which you confirm in Telegram).

## 8. Exit criteria (TEST_PLAN §4, Phase 7)

The `goldctl report` checklist shows each criterion with pass/fail: uptime ≥ 99% of open hours,
no position without a broker-side stop beyond one reconcile cycle, no unhandled crash, all
incidents acknowledged, every trade journaled end to end, net P&L reconciled to the broker's
deals, and the minimum days and trades. When all pass for the full period, I write the Phase 7
performance report and you decide on Phase 8.

## 9. If something goes wrong

| Symptom | What to do |
| --- | --- |
| `goldctl status` says gateway STALE or unreachable | Check MT5 is logged in and algorithmic trading is enabled; `Restart-ScheduledTask goldtrader-gateway`; send me `state\gateway.exit.log` |
| Telegram silent | `goldctl doctor`; check the bot token and chat id; the brain logs the reason |
| Kill switch ON | Nothing trades until you send `/resume` in Telegram and confirm; send me the incident text first |
| AI budget reached | Bots pause, safety continues; raise `llm.daily_budget_usd` only by confirmed chat command or config edit |
| Unexpected trade | Send me the trade id from `/status`; `get_trade_detail` in chat shows proposal, gates, risk verdict and fills |

Never paste secrets into chat or into this repository.
