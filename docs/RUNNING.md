# Running the data logger

The logger is **read-only**: it records Kalshi KXHIGH order books, trades and settlements, plus NWS observations and Daily Climate Reports, into one SQLite file (`data_store/weatherbot.db`). It never places orders.

It needs to run 24/7. Every hour it's off is order-book history you can't download later.

---

## 1. Create a read-only Kalshi API key (once)

1. Log in at kalshi.com and go to **Account → Profile → API Keys**.
2. Create a new key with **read** permission only. The logger refuses to start with a write-scoped key.
3. Download the private key file. Kalshi shows it **once**, so save it right away:
   - Windows: `C:\Users\<you>\.kalshi\kalshi_read.pem`
   - Linux / Mac: `~/.kalshi/kalshi_read.pem`
4. Keep the key file **outside** the repo folder.

## 2. Windows PC

### Install (once)

1. Install **Python 3.12** from python.org (3.11–3.13 work; brand-new versions may lack ready-made packages). On the first installer screen, tick **"Add python.exe to PATH"**.
2. Install **Git** from git-scm.com (default options are fine).
3. Open **PowerShell** and run:
   ```powershell
   cd $HOME\Documents
   git clone https://github.com/ramicd02/Weather-prediction.git
   cd Weather-prediction
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -e ".[dev]"
   ```
   Use `python -m pip` rather than `pip`: some Windows security policies block the `pip.exe` launcher. If PowerShell blocks `Activate.ps1`, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then try again.

### Configure (once)

1. Copy `.env.example` to `.env`:
   ```powershell
   copy .env.example .env
   notepad .env
   ```
   Fill it in and save:
   ```
   KALSHI_ENV=prod
   KALSHI_API_KEY_ID=<key id from Kalshi>
   KALSHI_PRIVATE_KEY_PATH=C:\Users\<you>\.kalshi\kalshi_read.pem
   ```
2. Create `config.local.yaml` (`notepad config.local.yaml`) with your contact email, then save:
   ```yaml
   nws:
     user_agent: "weatherbot-logger (contact: you@example.com)"
   ```
   The NWS asks every app to identify itself, and the logger skips weather data until you do. Git ignores this file, so updates never clash with it. Don't edit `config.yaml` for personal settings.

### Check it works

```powershell
python -m pytest -q                       # should say "124 passed"
python -m weatherbot.data.logger          # starts logging; leave the window open
```

Open a **second** PowerShell window in the same folder and run:

```powershell
.\.venv\Scripts\Activate.ps1
python -m weatherbot.data.logger status
```

After about 30 minutes, run `status` again and confirm:
- `Markets:` shows open markets
- `Rows:` counts are growing for `book_snapshots`, `trades` and `observations`
- `Errors/warnings in the last hour` is 0 or close to it
- Under `Stations:`, every series has a station. Any marked `<- confirm` was matched from the market rules text; check it on the market's Kalshi page, then pin it in `config.yaml` under `stations:`. Any marked `UNKNOWN` needs adding there.

### Keep it running on Windows

1. Set **Settings → System → Power → Sleep** to **Never** while plugged in.
2. `run_logger.bat` is in the repo folder (it contains):
   ```bat
   @echo off
   cd /d %~dp0
   if not exist data_store mkdir data_store
   :loop
   call .venv\Scripts\python.exe -m weatherbot.data.logger >> data_store\logger.log 2>&1
   timeout /t 10
   goto loop
   ```
3. Start it from PowerShell with `.\run_logger.bat` (the `.\` is required). Start it automatically: press **Win + R**, type `shell:startup`, and put a shortcut to `run_logger.bat` in that folder. It starts when you log in and restarts after crashes.

Windows updates and power cuts will still cause gaps. A small Linux server (below) is more reliable once real money is involved.

## 3. Linux server or VPS (more reliable)

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
git clone https://github.com/ramicd02/Weather-prediction.git ~/Weather-prediction
cd ~/Weather-prediction
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env && nano .env        # fill in as above; key path e.g. /home/<you>/.kalshi/kalshi_read.pem
chmod 600 .env ~/.kalshi/*.pem
nano config.yaml                          # set your email in nws.user_agent
python -m pytest -q
```

Install it as a service so it starts on boot and restarts on failure:

```bash
sudo cp deploy/weatherbot-logger.service /etc/systemd/system/
sudo sed -i "s/REPLACE_USER/$USER/g" /etc/systemd/system/weatherbot-logger.service
sudo systemctl daemon-reload
sudo systemctl enable --now weatherbot-logger
journalctl -u weatherbot-logger -f       # live logs (Ctrl+C to exit)
python -m weatherbot.data.logger status  # from the repo folder, venv active
```

## 4. Settlement check

Kalshi's rules settle on The Weather Company's number, not the NWS Daily Climate Report. This command measures how often the two agree, per city:

```
python -m weatherbot.data.settlement_check
```

It pulls every settled KXHIGH market from Kalshi (read-only), backfills the NWS CLI highs from the Iowa Environmental Mesonet, and prints a table. It's safe to run while the logger is running. The first run takes a while; later runs only fetch what's new. Add `--no-fetch` to reprint the report from saved data, or `--series KXHIGHCHI` for one city. Disagreeing days go to `data_store/settlement_mismatches.csv`.

"Outcome agree" is the share of city-days where the CLI high would have settled every market the same way Kalshi did. Only cities near 100% are candidates for trading.

## 5. NBM forecast backfill

Downloads past National Blend of Models forecasts (the NBP text bulletin) for every settlement station from NOAA's free archive. Each forecast gives tomorrow's high as a median, spread and 10th–90th percentiles.

```
python -m weatherbot.data.nbm sample      # first: checks one file and saves data_store/nbm_sample.txt
python -m weatherbot.data.nbm backfill    # Nov 2024 to yesterday, 13Z run (~32 MB a day); safe to stop and rerun
python -m weatherbot.data.nbm cli         # NWS highs for every year the forecasts cover
python -m weatherbot.data.nbm report      # NBM vs NWS high per city, plus old-vs-v5.0 on the same months
```

Each forecast is stamped `available_at` = run time + 2 hours, and backtests may only use forecasts available before the trade time. Options: `--start 2026-01-01`, `--end`, `--cycles 1,7,13,19` (more runs, ~4x the download), `--concurrency 4`. Files carry no NBM version, so runs are labelled 4.x, transition (2026-04-30 to 05-04) or 5.0 by date.

## 6. Kalshi price history (Phase 2)

Hourly bid, ask, trade price and volume for every settled weather market, for the backtest. Read-only, resumable, a few hours, safe alongside the logger (it uses about 3/4 of the read rate the logger is allowed).

```
python -m weatherbot.backtest.prices --limit 20   # quick check first
python -m weatherbot.backtest.prices              # everything; safe to stop and rerun
```

Then score the model on the tuning months (raw NBM vs calibrated vs market; takes about a minute):

```
python -m weatherbot.backtest.score
```

Per-day detail goes to `data_store/score_days.csv`. Nothing after `backtest.tuning_end` is ever loaded.

Two checks on whether any edge exists (does the model add information to the market's price, and is the market slow at some hour):

```
python -m weatherbot.backtest.diagnose
```

## 7. Day-to-day

| Task | Command |
| --- | --- |
| Health check | `python -m weatherbot.data.logger status` |
| Stop (Windows) | Close the window or press Ctrl+C |
| Stop / start (Linux) | `sudo systemctl stop weatherbot-logger` / `start` |
| Update code | `git pull`, then `python -m pip install -e ".[dev]"`, then restart the logger |
| Back up data | Copy `data_store/weatherbot.db` while the logger is stopped, or use `sqlite3 data_store/weatherbot.db ".backup backup.db"` while running |

## What gets stored

| Table | Contents |
| --- | --- |
| `markets` | Every KXHIGH market seen: strikes, `strike_type`, close time, rules text, settlement station, result |
| `book_snapshots` | Full YES/NO bid ladders on every snapshot, every best-price change, and at most once a minute otherwise |
| `trades` | Every public trade: price, size, taker side |
| `observations` | Latest NWS observation per settlement station every 10 minutes, with the raw METAR |
| `cli_reports` | Preliminary and final Daily Climate Reports: date, max/min °F, full text |
| `events` | Warnings and errors (shown by `status`) |

Prices are integers in 1/10,000 of a dollar (`4200` = $0.42) because Kalshi supports sub-penny prices. Sizes are integers in 1/100 of a contract. Times are UTC.
