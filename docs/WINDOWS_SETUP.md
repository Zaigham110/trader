# Windows setup — complete steps for the owner's machine

Applies to a Windows 10/11 64-bit PC or VPS (D-053). Every command runs in **PowerShell on the
Windows machine** (from a Mac: Microsoft Remote Desktop → the Windows machine → PowerShell).
Copy one block at a time. Expected results are given so you can tell when a step worked.
Never paste secrets into chat, files in the repository, or screenshots.

## Fastest route: one command

Parts A and B below are automated by `scripts/windows/bootstrap.ps1`. In PowerShell on the Windows
machine run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
irm https://raw.githubusercontent.com/Zaigham110/trader/claude/ecstatic-clarke-8nleqk/scripts/windows/bootstrap.ps1 | iex
```

It installs Git, Python and uv with winget if missing, clones the repository to `$HOME\trader`,
creates the environment, installs the package and runs the tests. Then do Part C (keys and
secrets) by hand and run `.\scripts\windows\preflight.ps1` for Part D. Part A step 1 (power and
update settings) and step 5 (MT5 install and login) remain manual.

## Part A — the machine (15 minutes)

1. **Windows settings.** Settings → System → Power: screen off is fine, *sleep never* (on a VPS this
   is already the case). Settings → Windows Update → Advanced options → Active hours: set them so
   restarts happen only at the weekend (gold is closed Saturday). Settings → Time & language →
   make sure *Set time automatically* is on (the system compares server time with the clock).
2. **Install Git for Windows.** Download from https://git-scm.com/download/win, keep the defaults.
3. **Install Python 3.12 (64-bit).** Download "Windows installer (64-bit)" from
   https://www.python.org/downloads/windows/ (any 3.12.x; 3.13 also works). In the installer tick
   **Add python.exe to PATH**, then *Install Now*.
4. **Install uv.** Open a new PowerShell window and run:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

   Close and reopen PowerShell. `uv --version` must print a version.

5. **Install MetaTrader 5 from Exness.** Personal Area → *Download MetaTrader 5* (the Exness build),
   install, start it, log in with your **demo** account (login, password, server are shown in the
   Personal Area under the account's *Trading platform* details). In MT5: *Tools → Options →
   Expert Advisors* → tick **Allow algorithmic trading**. Leave the terminal running and logged in
   whenever the system runs. Find the gold symbol in Market Watch (right-click → *Show All*):
   note its exact name (expected `XAUUSDm` on a Standard account).

## Part B — the software (10 minutes)

6. **Get the code.**

```powershell
cd $HOME
git clone https://github.com/Zaigham110/trader.git
cd trader
git checkout claude/ecstatic-clarke-8nleqk
```

7. **Create the environment and install.**

```powershell
uv venv .venv
uv pip install -e ".[dev,windows]"
.venv\Scripts\goldctl --help
```

   The last command prints the list of commands. If `goldctl` is not found, run
   `.venv\Scripts\python -m goldtrader.cli --help` instead and use that form throughout.

8. **Run the tests once** (about 2 minutes; everything must pass):

```powershell
.venv\Scripts\python -m pytest -q
```

## Part C — accounts and keys (20 minutes)

9. **Anthropic API key, paid for by your Claude Max plan (D-054).** Max plans include 100 USD of
   Claude API credit per billing cycle (Verified 2026-11-16: platform.claude.com/docs/en/about-claude/
   api-credits-for-subscribers). Steps:
   a. On https://claude.ai go to *Settings → Billing → API credits* and link a Claude Console
      organization (create one at https://platform.claude.com first if you have none). New
      subscribers can claim after 7 days on the plan. Each plan links to one organization and the
      link cannot be changed by you later, so pick the organization you will build in.
   b. In the Console, create a **workspace** for this project and set a workspace spend limit
      (for example 80 USD/month) so nothing else in the organization can consume the credit.
   c. In that workspace create an **API key**. Keep it in your password manager; you type it once in
      step 12. No payment method is needed; usage draws from the included credit, which expires at
      the end of each cycle without rollover. When it is exhausted, API requests stop until the
      next cycle (the bots pause; trading safety does not depend on the model).
   The credit does not cover Claude Code or Claude.ai usage, and the system must not use your
   Claude.ai login or a Claude Code token: Anthropic's terms reserve those for its own apps.
10. **Telegram bot.** In Telegram open **@BotFather** → `/newbot` → choose a name and a username
    ending in `bot`. BotFather replies with the **bot token** (looks like `1234567890:AA...`).
    Then open a chat with your new bot and send it any message, for example `hello`.
11. **Your Telegram chat id.** In a browser open
    `https://api.telegram.org/bot<TOKEN>/getUpdates` with your token in place of `<TOKEN>`.
    In the reply find `"chat":{"id":123456789` — that number is your chat id. (If the page shows
    an empty `result`, send the bot another message and reload.)
12. **Store the secrets in Windows Credential Manager** (prompted, nothing is echoed):

```powershell
cd $HOME\trader
.venv\Scripts\goldctl secret-set MT5_LOGIN
.venv\Scripts\goldctl secret-set MT5_PASSWORD
.venv\Scripts\goldctl secret-set MT5_SERVER
.venv\Scripts\goldctl secret-set ANTHROPIC_API_KEY
.venv\Scripts\goldctl secret-set TELEGRAM_BOT_TOKEN
.venv\Scripts\goldctl secret-set TELEGRAM_OWNER_CHAT_ID
.venv\Scripts\goldctl secret-set GATEWAY_TOKEN
```

   `MT5_SERVER` is the server name shown in MT5's login window (for example `Exness-MT5Trial7`).
   `GATEWAY_TOKEN` is any random text of at least 32 characters you make up (for example from
   https://www.random.org/strings/ or by typing 40 random letters); both processes read it.

## Part D — preflight (10 minutes)

13. **Doctor and model check.** Both must end without FAIL lines:

```powershell
.venv\Scripts\goldctl doctor --config config\demo.yaml --db state\demo.sqlite
```

```powershell
.venv\Scripts\goldctl llm-check
```

14. **On-account tests** (MT5 must be running and logged in; first read-only, then with
    0.01-lot test trades on the **demo** account; the suite refuses a real account):

```powershell
.venv\Scripts\goldctl mt5-tests --real --no-trade --out reports\mt5\readonly.md
```

```powershell
.venv\Scripts\goldctl mt5-tests --real --trade --out reports\mt5\trade.md
```

```powershell
.venv\Scripts\goldctl spec-dump --real --db state\demo.sqlite > reports\mt5\spec.json
```

   If `--real` reports the symbol was not found, add `--symbol <name from step 5>`.
   Send me the three files in `reports\mt5\`.

   If `llm-check` fails with a 400 error, the lines under it quote the API's own message. "Credit
   balance is too low" means the Max plan's monthly API credit is not linked to the Console
   organization the key belongs to: in the Console open *Settings → Billing* and confirm the
   organization shows the promotional credit; if not, create the key in the organization that does
   (step 9). Any other message: send me the lines.

14b. **Fill the bar history from the terminal** (fast, no download; the features need about 20 days):

```powershell
.venv\Scripts\goldctl backfill --real --days 60 --db state\demo.sqlite
```

15. **One month of history.** Personal Area → *?* menu → *Tools & Services* → *Tick History* →
    XAUUSD → last full month → download the CSV. Then:

```powershell
.venv\Scripts\goldctl import-ticks "$HOME\Downloads\<file>.csv" --out data\bars\exness_1m.parquet
```

```powershell
.venv\Scripts\goldctl backtest --data data\bars\exness_1m.parquet --db state\research.sqlite
```

   Send me the newest `.md` and `.json` from `reports\backtests\`.

## Part E — start the system (5 minutes)

16. **Set the demo balance.** Personal Area → the demo account → *Set balance* → 5000 USD
    (this matches `config\demo.yaml`).
17. **Register and start the two tasks** in an **administrator** PowerShell (right-click
    PowerShell → *Run as administrator*):

```powershell
cd $HOME\trader
powershell -ExecutionPolicy Bypass -File scripts\windows\install-tasks.ps1 -Provider anthropic
Start-ScheduledTask goldtrader-gateway
Start-Sleep 15
Start-ScheduledTask goldtrader-brain
```

18. **Check.** In a normal PowerShell:

```powershell
cd $HOME\trader
.venv\Scripts\goldctl status
```

   Expected: `gateway ok`, `brain ok`, `open incidents: 0`, `gateway: session=...`. In Telegram
   send `/status` to your bot; it answers within a few seconds. Then ask it in plain words
   "what are my risk limits?" and check the reply has a `source:` line.

19. **Nightly backup task** (administrator PowerShell, once):

```powershell
$a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -File $HOME\trader\scripts\windows\backup-db.ps1" -WorkingDirectory "$HOME\trader"
$t = New-ScheduledTaskTrigger -Daily -At 23:30
Register-ScheduledTask -TaskName "goldtrader-backup" -Action $a -Trigger $t -Force | Out-Null
```

Send me: the outputs of steps 13 and 18, the three files from step 14, and the backtest report
from step 15. From then on follow `PHASE7_RUNBOOK.md` §6–§9 (daily status, weekly report).

## Stopping, updating, removing

```powershell
Stop-ScheduledTask goldtrader-brain; Stop-ScheduledTask goldtrader-gateway
```

Update to a newer version of the code (the tasks must be stopped first):

```powershell
cd $HOME\trader; git pull; uv pip install -e ".[dev,windows]"; .venv\Scripts\python -m pytest -q
Start-ScheduledTask goldtrader-gateway; Start-Sleep 15; Start-ScheduledTask goldtrader-brain
```

Remove the tasks: `powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall-tasks.ps1`.
