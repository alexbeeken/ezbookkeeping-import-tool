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

### 2. Find your ezBookkeeping username

`config.json` needs `ezbookkeeping_username` — that is the **login username**, not the numeric `[Uid]`.

Ways to find it:

- **Web UI:** use the username you sign in with (User Settings / Profile also shows it).
- **CLI:** if you already know the username, confirm it (and see the numeric Uid) with:

```bash
ezbookkeeping userdata user-get --username YOUR_USERNAME
```

Example output includes:

```text
[Uid] 1234567890
[Username] alice
[Email] alice@example.com
...
```

Put `alice` (the `[Username]` value) into `ezbookkeeping_username`. If you sync into two ezBookkeeping users, use a separate `config.json` per username (each with its own `state_file`).

There is no `user-list` CLI command — you look up users by username.

### 3. Map accounts

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

### 4. Categories

ezBookkeeping uses a **two-level** category tree. Transactions must use a **secondary** category (the leaf). A primary-only category named `Uncategorized` will **not** match — the CLI will still say it “needs to be created.”

Create categories like this in **Transaction Categories**:

1. Add a **primary** expense category (e.g. `Misc` or `Imported`).
2. Under it, add a **secondary** expense category named exactly what you put in config (default: `Uncategorized`).
3. Repeat for **income** (primary + secondary). Income and expense are separate type trees — an expense `Uncategorized` does not cover income transactions.

Then set config to the **secondary** name (and optionally the primary):

| Config key | Purpose |
|---|---|
| `default_expense_category` | Secondary expense name (must exist) |
| `default_income_category` | Secondary income name (must exist) |
| `default_expense_parent` / `default_income_parent` | Optional primary names (CSV `Category` column) |

Example:

```json
"default_expense_parent": "Misc",
"default_expense_category": "Uncategorized",
"default_income_parent": "Misc",
"default_income_category": "Uncategorized"
```

In the generated CSV, `Sub Category` is the secondary name; `Category` is the primary. Names must match exactly (spelling/spacing), and the secondary must not be hidden.

### 5. Dry run, then live import

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
5. `ezbookkeeping userdata transaction-import --username … --file … --type ezbookkeeping_csv`
6. Imported SimpleFIN transaction IDs are stored in `sync_state.json` so overlapping windows do not create duplicates
7. Errors go to stdout and `bridge_error.log`

## Config reference

| Key | Description |
|---|---|
| `simplefin_access_url` | Claimed Access URL (`https://user:pass@host/…`) |
| `ezbookkeeping_cli` | CLI binary name or absolute path (default `ezbookkeeping`) |
| `ezbookkeeping_username` | Target ezBookkeeping **login username** (not numeric Uid; see setup step 2) |
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
