# SimpleFIN → ezBookkeeping Sync

Lightweight cron bridge for a Raspberry Pi Zero 2 W (DietPi). It wakes up, pulls recent transactions from [SimpleFIN Bridge](https://bridge.simplefin.org), writes an ezBookkeeping CSV, imports via the local `ezbookkeeping` CLI, and exits so RAM stays free for Navidrome and friends.

**Stack:** Python 3 + `requests` only. No daemon, no Pandas, no Docker.

## Requirements

- Raspberry Pi Zero 2 W (or any Linux host) running DietPi / Debian
- Python 3 (`dietpi-software` or `apt install python3 python3-pip`)
- [ezBookkeeping](https://ezbookkeeping.mayswind.net/) installed natively (CLI on `PATH` or a full path in config)
- A SimpleFIN Bridge account and Access URL
- Matching **account names** and **categories** already created in ezBookkeeping (CLI import will not create them)

## Quick setup

```bash
cd /path/to/ezbookkeeping-import
python3 -m pip install --user -r requirements.txt
cp config.json.example config.json
```

### 1. Claim a SimpleFIN Access URL (one-time)

Create a Setup Token at https://bridge.simplefin.org/simplefin/create, then:

```bash
python3 simplefin_sync.py --claim-token 'PASTE_SETUP_TOKEN_HERE'
```

Copy the printed Access URL into `config.json` as `simplefin_access_url`. Treat it like a password — it embeds Basic Auth credentials. Setup Tokens are single-use.

### 2. Map accounts

List SimpleFIN account IDs:

```bash
python3 simplefin_sync.py --list-accounts
```

Edit `config.json` `account_map` so each SimpleFIN id points at an **existing** ezBookkeeping account name:

```json
"account_map": {
  "ACT-123": "Checking",
  "ACT-456": "Credit Card"
}
```

### 3. Categories

Create income/expense categories in ezBookkeeping first. Then set:

| Config key | Purpose |
|---|---|
| `default_expense_category` | Sub Category name for outflows (required to exist) |
| `default_income_category` | Sub Category name for inflows (required to exist) |
| `default_expense_parent` / `default_income_parent` | Optional parent Category names |

### 4. Dry run, then live import

```bash
python3 simplefin_sync.py --dry-run
# Inspect dry_run_import.csv, then:
python3 simplefin_sync.py
```

## Cron on DietPi

```bash
chmod +x /path/to/ezbookkeeping-import/simplefin_sync.py
crontab -e
```

SimpleFIN asks clients to stay under ~24 `/accounts` requests per day and prefers off-the-hour minutes. Example: daily at 02:17:

```cron
17 2 * * * /usr/bin/python3 /path/to/ezbookkeeping-import/simplefin_sync.py >> /var/log/simplefin_sync.log 2>&1
```

Run the job as the same OS user that can execute the ezBookkeeping CLI against your data directory. If the CLI needs a specific cwd, set `ezbookkeeping_working_directory` in `config.json`.

## How it works

1. `GET {ACCESS_URL}/accounts?version=2&start-date=…` with embedded Basic Auth
2. Fetch window = `lookback_days` + `overlap_days` (default 7+5; capped at 90). Overlap follows SimpleFIN’s recommendation so late-posted transactions are not missed
3. Positive SimpleFIN amounts → **Income**; negative → **Expense**. Amounts are written without thousands separators
4. CSV uses the native `ezbookkeeping_csv` columns (`Time`, `Type`, `Sub Category`, `Account`, `Amount`, …)
5. `ezbookkeeping transaction-import --username … --file … --type ezbookkeeping_csv`
6. Imported SimpleFIN transaction IDs are stored in `sync_state.json` so overlapping windows do not create duplicates
7. Errors go to stdout and `bridge_error.log`

## Config reference

| Key | Description |
|---|---|
| `simplefin_access_url` | Claimed Access URL (`https://user:pass@host/…`) |
| `ezbookkeeping_cli` | CLI binary name or absolute path (default `ezbookkeeping`) |
| `ezbookkeeping_username` | Target ezBookkeeping username |
| `ezbookkeeping_working_directory` | Optional cwd for the CLI process |
| `timezone` | IANA timezone for CSV timestamps (e.g. `America/Los_Angeles`) |
| `lookback_days` / `overlap_days` | Fetch window pieces |
| `include_pending` | Include pending SimpleFIN transactions |
| `account_map` | SimpleFIN account id → ezBookkeeping account **name** |
| `state_file` / `error_log` | Paths relative to the script directory if not absolute |
| `keep_csv_path` | If set, leave the generated CSV at this path |

## Notes

- Accounts, categories, and tags referenced in the CSV **must already exist** in ezBookkeeping or the CLI aborts.
- Do not commit `config.json` or `sync_state.json` — both are gitignored.
- This script never stores your bank login; SimpleFIN only exposes read-only balances and transactions via the Access URL.
