#!/usr/bin/env python3
"""
SimpleFIN → ezBookkeeping sync bridge.

Designed for cron on a resource-constrained Raspberry Pi Zero 2 W (DietPi):
fetch recent SimpleFIN transactions, write an ezBookkeeping CSV, import via
the local CLI, then exit so RAM is freed.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
DEFAULT_STATE_PATH = os.path.join(SCRIPT_DIR, "sync_state.json")
DEFAULT_ERROR_LOG = os.path.join(SCRIPT_DIR, "bridge_error.log")
SF_TAG_RE = re.compile(r"\[sf:([^\]]+)\]")

# Official ezbookkeeping_csv headers (CLI --type ezbookkeeping_csv).
CSV_HEADERS = [
    "Time",
    "Timezone",
    "Type",
    "Category",
    "Sub Category",
    "Account",
    "Account Currency",
    "Amount",
    "Account2",
    "Account2 Currency",
    "Account2 Amount",
    "Geographic Location",
    "Tags",
    "Description",
]


def setup_logging(error_log_path: str, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    handlers.append(logging.FileHandler(error_log_path))
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def load_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        if default is not None:
            return default
        raise FileNotFoundError(f"File not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: str, data: Any) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp_path, path)


def load_config(path: str) -> dict[str, Any]:
    config = load_json(path)
    required = ["simplefin_access_url", "ezbookkeeping_username", "account_map"]
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(f"config.json missing required keys: {', '.join(missing)}")
    if not isinstance(config["account_map"], dict) or not config["account_map"]:
        raise ValueError("config.json account_map must be a non-empty object")
    return config


def require_requests():
    try:
        import requests  # noqa: WPS433
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: requests\n"
            "Install with: pip3 install --user -r requirements.txt"
        ) from exc
    return requests


def claim_access_url(setup_token: str, timeout: int = 30) -> str:
    """Exchange a one-time SimpleFIN Setup Token for a reusable Access URL."""
    requests = require_requests()
    token = setup_token.strip()
    try:
        claim_url = base64.b64decode(token).decode("utf-8").strip()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid Setup Token (base64 decode failed): {exc}") from exc

    if not claim_url.startswith("https://"):
        raise ValueError("Decoded claim URL must use HTTPS")

    response = requests.post(
        claim_url,
        headers={"Content-Length": "0"},
        timeout=timeout,
    )
    if response.status_code == 403:
        raise RuntimeError(
            "Claim failed (403). Token may already be used or compromised — "
            "disable it in SimpleFIN Bridge and create a new Setup Token."
        )
    response.raise_for_status()
    access_url = response.text.strip()
    if "://" not in access_url or "@" not in access_url:
        raise RuntimeError(f"Unexpected Access URL response: {access_url[:120]}")
    return access_url


def parse_access_url(access_url: str) -> tuple[str, str, str]:
    """
    Split https://user:pass@host/path into (base_url, username, password).
    Returns base_url without credentials.
    """
    parsed = urlparse(access_url)
    if parsed.scheme != "https":
        raise ValueError("simplefin_access_url must use HTTPS")
    if not parsed.username or parsed.password is None:
        raise ValueError(
            "simplefin_access_url must embed Basic Auth credentials "
            "(https://user:pass@host/...)"
        )
    # Rebuild URL without credentials.
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    base_url = f"{parsed.scheme}://{netloc}{parsed.path.rstrip('/')}"
    return base_url, parsed.username, parsed.password


def fetch_accounts(
    access_url: str,
    start_ts: int | None,
    end_ts: int | None,
    timeout: int,
    include_pending: bool,
    *,
    balances_only: bool = False,
) -> dict[str, Any]:
    requests = require_requests()
    base_url, username, password = parse_access_url(access_url)
    params: dict[str, Any] = {"version": "2"}
    if balances_only:
        # Skip transaction history; still counts toward SimpleFIN /accounts quota.
        params["balances-only"] = "1"
    else:
        if start_ts is None:
            raise ValueError("start_ts is required unless balances_only=True")
        params["start-date"] = start_ts
        if end_ts is not None:
            params["end-date"] = end_ts
        if include_pending:
            params["pending"] = "1"

    try:
        response = requests.get(
            f"{base_url}/accounts",
            auth=(username, password),
            params=params,
            timeout=timeout,
        )
    except requests.Timeout as exc:
        raise RuntimeError(f"SimpleFIN request timed out after {timeout}s") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"SimpleFIN network error: {exc}") from exc

    if response.status_code == 403:
        raise RuntimeError(
            "SimpleFIN authentication failed (403). Access may have been revoked."
        )
    if response.status_code == 402:
        raise RuntimeError("SimpleFIN payment required (402).")
    if response.status_code >= 400:
        raise RuntimeError(
            f"SimpleFIN HTTP {response.status_code}: {response.text[:300]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("SimpleFIN returned invalid JSON") from exc

    # Prefer structured errlist (v2); fall back to deprecated errors strings.
    errlist = data.get("errlist") or []
    for err in errlist:
        if isinstance(err, dict):
            logging.warning(
                "SimpleFIN errlist: code=%s msg=%s conn_id=%s account_id=%s",
                err.get("code"),
                err.get("msg"),
                err.get("conn_id"),
                err.get("account_id"),
            )
        else:
            logging.warning("SimpleFIN errlist: %s", err)
    for err in data.get("errors") or []:
        logging.warning("SimpleFIN error: %s", err)

    return data


def txn_key(account_id: str, txn: dict[str, Any]) -> str:
    txn_id = str(txn.get("id") or "")
    if not txn_id:
        # Fallback fingerprint if an institution omits ids.
        txn_id = (
            f"{txn.get('posted')}|{txn.get('amount')}|{txn.get('description')}"
        )
    return f"{account_id}:{txn_id}"


def simplefin_txn_id(txn: dict[str, Any]) -> str:
    return str(txn.get("id") or "")


def extract_sf_id_from_comment(comment: str) -> str | None:
    match = SF_TAG_RE.search(comment or "")
    return match.group(1) if match else None


def bare_sf_ids_from_keys(keys: set[str]) -> set[str]:
    """Derive SimpleFIN txn ids from accountId:txnId state keys."""
    bare: set[str] = set()
    for key in keys:
        if ":" in key:
            bare.add(key.split(":", 1)[1])
        elif key:
            bare.add(key)
    return bare


def load_sf_ids_from_db(db_path: str) -> set[str]:
    """
    Read [sf:…] tags already present in ezBookkeeping comments.
    Returns bare SimpleFIN transaction ids (not account-prefixed).
    """
    if not db_path:
        return set()
    if not os.path.exists(db_path):
        logging.warning("ezbookkeeping_db not found at %s", db_path)
        return set()

    found: set[str] = set()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logging.warning("Could not open ezBookkeeping DB %s: %s", db_path, exc)
        return set()
    try:
        rows = conn.execute(
            'SELECT comment FROM "transaction" '
            'WHERE deleted=0 AND comment LIKE "%[sf:%"'
        )
        for (comment,) in rows:
            sf_id = extract_sf_id_from_comment(comment or "")
            if sf_id:
                found.add(sf_id)
    finally:
        conn.close()
    logging.info(
        "Loaded %d SimpleFIN id(s) already present in ezBookkeeping DB", len(found)
    )
    return found


def build_seen_sets(
    state_keys: set[str], db_sf_ids: set[str]
) -> tuple[set[str], set[str]]:
    """
    Returns (full_keys, bare_sf_ids) used for dedup.
    full_keys keep accountId:txnId from state; bare_sf_ids union state + DB.
    """
    full_keys = set(state_keys)
    bare = bare_sf_ids_from_keys(full_keys) | set(db_sf_ids)
    return full_keys, bare


def format_amount(value: Decimal) -> str:
    """Format amount without thousands separators; keep up to 2 decimal places."""
    quantized = value.quantize(Decimal("0.01"))
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def classify_transaction(
    amount: Decimal,
    *,
    positive_is_income: bool = False,
) -> tuple[str, Decimal]:
    """
    Map a SimpleFIN amount to ezBookkeeping Type + absolute Amount.

    The SimpleFIN protocol says positive = money deposited. Some Bridge
    institution feeds are reversed in practice; default is positive → Expense.
    Set positive_is_income true in config to follow the protocol literally.
    """
    money_in = amount > 0 if positive_is_income else amount < 0
    if money_in:
        return "Income", abs(amount)
    return "Expense", abs(amount)


def timezone_offset_string(dt: datetime) -> str:
    offset = dt.utcoffset()
    if offset is None:
        return "+00:00"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def resolve_timezone(name: str | None):
    """Return a tzinfo from an IANA name, or local/UTC fallback."""
    if not name:
        return datetime.now().astimezone().tzinfo or timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        logging.warning("Unknown timezone %r; using local timezone", name)
        return datetime.now().astimezone().tzinfo or timezone.utc


def parse_hhmmss(value: str) -> tuple[int, int, int]:
    parts = value.strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"Expected HH:MM:SS, got {value!r}")
    hour, minute, second = (int(parts[0]), int(parts[1]), int(parts[2]))
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError(f"Invalid time of day: {value!r}")
    return hour, minute, second


def is_date_only_timestamp(utc_dt: datetime) -> bool:
    """
    Banks via SimpleFIN often send date-only posts at a fixed UTC anchor
    (commonly 00:00 or 12:00 UTC). Noon UTC becomes 05:00 in PDT.
    """
    return (
        utc_dt.minute == 0
        and utc_dt.second == 0
        and utc_dt.microsecond == 0
        and utc_dt.hour in (0, 12)
    )


def resolve_transaction_datetime(
    txn: dict[str, Any],
    tzinfo,
    *,
    date_only_time: str = "00:00:00",
    prefer_transacted_at: bool = True,
) -> datetime:
    """
    Build the local datetime written to the ezBookkeeping CSV.

    Prefer transacted_at when present. For date-only posted stamps, keep the
    local calendar date and replace the clock with date_only_time.
    """
    posted = int(txn.get("posted") or 0)
    transacted_at = int(txn.get("transacted_at") or 0)
    if prefer_transacted_at and transacted_at > 0:
        epoch = transacted_at
    elif posted > 0:
        epoch = posted
    else:
        raise ValueError("transaction missing posted/transacted_at timestamp")

    utc_dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    local_dt = utc_dt.astimezone(tzinfo)

    # Only normalize the SimpleFIN posted date-only pattern. If we used a real
    # transacted_at that happens to land on 00:00/12:00 UTC, still normalize —
    # bank "times" from SimpleFIN are rarely meaningful wall-clock times.
    if is_date_only_timestamp(utc_dt):
        hour, minute, second = parse_hhmmss(date_only_time)
        local_dt = local_dt.replace(
            hour=hour, minute=minute, second=second, microsecond=0
        )
    return local_dt


def build_csv_rows(
    accounts_payload: dict[str, Any],
    account_map: dict[str, str],
    seen_keys: set[str],
    seen_sf_ids: set[str],
    default_expense_category: str,
    default_income_category: str,
    default_expense_parent: str,
    default_income_parent: str,
    tzinfo,
    skip_pending: bool,
    date_only_time: str = "00:00:00",
    positive_is_income: bool = False,
) -> tuple[list[dict[str, str]], list[str], dict[str, int]]:
    rows: list[dict[str, str]] = []
    new_keys: list[str] = []
    stats = {
        "accounts_mapped": 0,
        "accounts_unmapped_with_txns": 0,
        "txns_fetched": 0,
        "txns_already_seen": 0,
        "txns_pending_skipped": 0,
        "txns_zero_skipped": 0,
        "txns_bad_skipped": 0,
        "txns_new": 0,
    }

    for account in accounts_payload.get("accounts") or []:
        sf_id = str(account.get("id") or "")
        if not sf_id:
            continue
        ez_account = account_map.get(sf_id)
        account_txns = account.get("transactions") or []
        if not ez_account:
            if account_txns:
                stats["accounts_unmapped_with_txns"] += 1
                logging.warning(
                    "Skipping SimpleFIN account id=%s name=%r (%d txn(s); not in account_map)",
                    sf_id,
                    account.get("name"),
                    len(account_txns),
                )
            continue

        stats["accounts_mapped"] += 1
        currency = str(account.get("currency") or "")
        # Skip custom-currency URLs; ezBookkeeping expects ISO codes.
        if currency.startswith("http"):
            logging.warning(
                "Account %s uses custom currency URL; leaving Account Currency blank",
                sf_id,
            )
            currency = ""

        for txn in account_txns:
            stats["txns_fetched"] += 1
            if skip_pending and txn.get("pending"):
                stats["txns_pending_skipped"] += 1
                continue
            key = txn_key(sf_id, txn)
            sf_txn = simplefin_txn_id(txn)
            if key in seen_keys or (sf_txn and sf_txn in seen_sf_ids):
                stats["txns_already_seen"] += 1
                continue

            try:
                amount = Decimal(str(txn.get("amount", "0")))
            except InvalidOperation:
                stats["txns_bad_skipped"] += 1
                logging.error("Bad amount on txn %s: %r", key, txn.get("amount"))
                continue

            if amount == 0:
                stats["txns_zero_skipped"] += 1
                logging.debug("Skipping zero-amount txn %s", key)
                continue

            txn_type, abs_amount = classify_transaction(
                amount, positive_is_income=positive_is_income
            )
            try:
                when = resolve_transaction_datetime(
                    txn, tzinfo, date_only_time=date_only_time
                )
            except ValueError:
                stats["txns_bad_skipped"] += 1
                logging.warning("Skipping txn %s with missing posted timestamp", key)
                continue

            if txn_type == "Expense":
                parent = default_expense_parent
                sub = default_expense_category
            else:
                parent = default_income_parent
                sub = default_income_category

            description = str(txn.get("description") or "").replace("\n", " ").strip()
            # Put [sf:…] first so a naive CSV split on commas still keeps the id
            # (ezBookkeeping CLI has truncated quoted descriptions at commas before).
            sf_marker = f"[sf:{txn.get('id')}]"
            description = f"{sf_marker} {description}".strip() if description else sf_marker

            rows.append(
                {
                    "Time": when.strftime("%Y-%m-%d %H:%M:%S"),
                    "Timezone": timezone_offset_string(when),
                    "Type": txn_type,
                    "Category": parent,
                    "Sub Category": sub,
                    "Account": ez_account,
                    "Account Currency": currency,
                    "Amount": format_amount(abs_amount),
                    "Account2": "",
                    "Account2 Currency": "",
                    "Account2 Amount": "",
                    "Geographic Location": "",
                    "Tags": "",
                    "Description": description,
                }
            )
            new_keys.append(key)
            stats["txns_new"] += 1

    if stats["accounts_unmapped_with_txns"]:
        logging.warning(
            "%d SimpleFIN account(s) had transactions but no account_map entry",
            stats["accounts_unmapped_with_txns"],
        )
    return rows, new_keys, stats


def log_fetch_stats(stats: dict[str, int], seen_count: int) -> None:
    logging.info(
        "Fetch summary: mapped_accounts=%d fetched=%d new=%d already_seen=%d "
        "pending_skipped=%d zero_skipped=%d bad_skipped=%d unmapped_accounts=%d "
        "(dedup_state_size=%d)",
        stats["accounts_mapped"],
        stats["txns_fetched"],
        stats["txns_new"],
        stats["txns_already_seen"],
        stats["txns_pending_skipped"],
        stats["txns_zero_skipped"],
        stats["txns_bad_skipped"],
        stats["accounts_unmapped_with_txns"],
        seen_count,
    )
    if stats["txns_fetched"] == 0:
        logging.warning(
            "SimpleFIN returned 0 transactions in this window for mapped accounts. "
            "The bank feed may not have refreshed yet (often daily), or the "
            "connection needs re-auth in SimpleFIN Bridge."
        )
    elif stats["txns_new"] == 0 and stats["txns_already_seen"] > 0:
        logging.info(
            "All fetched transactions are already in sync_state.json. "
            "If you deleted them in ezBookkeeping and want to re-import, remove "
            "those ids from sync_state.json (or delete the file). If you made "
            "new purchases that are still pending, set include_pending to true."
        )
    elif stats["txns_new"] == 0 and stats["txns_pending_skipped"] > 0:
        logging.warning(
            "Only pending transactions were found and include_pending is false. "
            "Set \"include_pending\": true in config.json to import them."
        )


def write_csv(path: str, rows: list[dict[str, str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADERS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def import_via_cli(
    cli_path: str,
    username: str,
    csv_path: str,
    working_directory: str | None,
) -> None:
    cmd = [
        cli_path,
        "userdata",
        "transaction-import",
        "--username",
        username,
        "--file",
        csv_path,
        "--type",
        "ezbookkeeping_csv",
    ]
    logging.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            cwd=working_directory or None,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"ezBookkeeping CLI not found at {cli_path!r}. "
            "Set ezbookkeeping_cli in config.json."
        ) from exc

    if result.stdout:
        logging.info("CLI stdout:\n%s", result.stdout.strip())
    if result.stderr:
        logging.error("CLI stderr:\n%s", result.stderr.strip())
    if result.returncode != 0:
        raise RuntimeError(
            f"ezBookkeeping CLI failed with exit code {result.returncode}"
        )


def prune_seen(seen: set[str], keep: set[str], max_entries: int) -> set[str]:
    """
    Optionally cap the dedup set size.

    max_entries <= 0 means unlimited (preferred for daily sync idempotency).
    When capping, always retain `keep` (usually newly imported keys) first.
    """
    if max_entries <= 0 or len(seen) + len(keep) <= max_entries:
        return set(seen) | set(keep)

    retained = set(keep)
    # Prefer keys that look like accountId:txnId with UUID-ish ids (stable order).
    for key in sorted(seen):
        if len(retained) >= max_entries:
            break
        retained.add(key)
    logging.warning(
        "Dedup state pruned to %d entries (max_seen_ids=%d). "
        "Set max_seen_ids to 0 to disable pruning.",
        len(retained),
        max_entries,
    )
    return retained


def persist_sync_state(
    state_path: str,
    *,
    state: dict[str, Any],
    imported_keys: set[str],
    new_keys: list[str],
    max_seen_ids: int,
    db_path: str | None,
    last_run: str,
    last_import_count: int,
    stats: dict[str, int] | None = None,
) -> None:
    """
    Write sync_state only after a successful import (or empty successful fetch).
    Merges DB-backed sf ids so a lost state file still converges on rerun.
    """
    merged = set(imported_keys) | set(new_keys)
    if db_path:
        # Bare SimpleFIN ids already in ezBookkeeping comments.
        merged |= load_sf_ids_from_db(db_path)

    state["imported_ids"] = sorted(prune_seen(merged, set(new_keys), max_seen_ids))
    state["last_run"] = last_run
    state["last_import_count"] = last_import_count
    if stats is not None:
        state["last_fetch_stats"] = stats
    save_json(state_path, state)


def list_accounts(access_url: str, timeout: int) -> None:
    # Short window so we only pay for balances + a bit of history while mapping.
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=7)
    data = fetch_accounts(
        access_url,
        start_ts=int(start.timestamp()),
        end_ts=int(end.timestamp()),
        timeout=timeout,
        include_pending=False,
    )
    accounts = data.get("accounts") or []
    if not accounts:
        print("No accounts returned.")
        return
    print("SimpleFIN accounts (add these ids to config.json account_map):\n")
    for account in accounts:
        txn_count = len(account.get("transactions") or [])
        print(
            f"  id={account.get('id')!r}  name={account.get('name')!r}  "
            f"currency={account.get('currency')!r}  balance={account.get('balance')!r}  "
            f"recent_txns={txn_count}"
        )


def run_sync(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    error_log = config.get("error_log", DEFAULT_ERROR_LOG)
    setup_logging(error_log, verbose=args.verbose)

    state_path = config.get("state_file", DEFAULT_STATE_PATH)
    if state_path and not os.path.isabs(state_path):
        state_path = os.path.join(SCRIPT_DIR, state_path)
    state = load_json(state_path, default={"imported_ids": []})
    state_keys = set(state.get("imported_ids") or [])

    db_path = config.get("ezbookkeeping_db")
    if db_path and not os.path.isabs(db_path):
        db_path = os.path.join(SCRIPT_DIR, db_path)
    db_sf_ids = load_sf_ids_from_db(db_path) if db_path else set()
    seen_keys, seen_sf_ids = build_seen_sets(state_keys, db_sf_ids)

    lookback_days = int(config.get("lookback_days", 7))
    overlap_days = int(config.get("overlap_days", 5))
    # Fetch lookback + overlap so daily runs do not miss late-posted transactions.
    window_days = lookback_days + overlap_days
    if window_days > 90:
        logging.warning("Clamping fetch window to 90 days (SimpleFIN limit)")
        window_days = 90

    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=window_days)
    timeout = int(config.get("request_timeout_seconds", 60))
    include_pending = bool(config.get("include_pending", False))
    # 0 = unlimited; avoids randomly dropping ids and re-importing them.
    max_seen_ids = int(config.get("max_seen_ids", 0))

    logging.info(
        "Fetching SimpleFIN transactions from %s to %s (%d days)",
        start.date(),
        end.date(),
        window_days,
    )

    payload = fetch_accounts(
        config["simplefin_access_url"],
        start_ts=int(start.timestamp()),
        end_ts=None,
        timeout=timeout,
        include_pending=include_pending,
    )

    tzinfo = resolve_timezone(config.get("timezone"))
    rows, new_keys, stats = build_csv_rows(
        accounts_payload=payload,
        account_map={str(k): str(v) for k, v in config["account_map"].items()},
        seen_keys=seen_keys,
        seen_sf_ids=seen_sf_ids,
        default_expense_category=config.get("default_expense_category", "Uncategorized"),
        default_income_category=config.get("default_income_category", "Uncategorized"),
        default_expense_parent=config.get("default_expense_parent", ""),
        default_income_parent=config.get("default_income_parent", ""),
        tzinfo=tzinfo,
        skip_pending=not include_pending,
        date_only_time=str(config.get("date_only_time", "00:00:00")),
        positive_is_income=bool(config.get("positive_is_income", False)),
    )
    log_fetch_stats(stats, seen_count=len(seen_keys) + len(seen_sf_ids))

    if not rows:
        logging.info("No new transactions to import.")
        # Still refresh state from DB so a wiped state file heals itself.
        persist_sync_state(
            state_path,
            state=state,
            imported_keys=seen_keys,
            new_keys=[],
            max_seen_ids=max_seen_ids,
            db_path=db_path,
            last_run=end.isoformat(),
            last_import_count=0,
            stats=stats,
        )
        return 0

    logging.info("Prepared %d new transaction(s) for import", len(rows))

    keep_csv = config.get("keep_csv_path")
    if keep_csv:
        write_csv(keep_csv, rows)
        csv_path = keep_csv
        temp_dir = None
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="simplefin_sync_")
        csv_path = os.path.join(temp_dir.name, "import.csv")
        write_csv(csv_path, rows)

    try:
        if args.dry_run:
            logging.info("Dry run: wrote CSV to %s (CLI import skipped)", csv_path)
            if not keep_csv:
                # Persist dry-run output next to the script for inspection.
                preview = os.path.join(SCRIPT_DIR, "dry_run_import.csv")
                write_csv(preview, rows)
                logging.info("Copied dry-run CSV to %s", preview)
            return 0

        import_via_cli(
            cli_path=config.get("ezbookkeeping_cli", "ezbookkeeping"),
            username=config["ezbookkeeping_username"],
            csv_path=csv_path,
            working_directory=config.get("ezbookkeeping_working_directory"),
        )
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    # Only reached on successful import — keep state write tightly after CLI success.
    persist_sync_state(
        state_path,
        state=state,
        imported_keys=seen_keys,
        new_keys=new_keys,
        max_seen_ids=max_seen_ids,
        db_path=db_path,
        last_run=end.isoformat(),
        last_import_count=len(new_keys),
        stats=stats,
    )
    logging.info("Import complete. Recorded %d txn id(s) in state.", len(new_keys))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync SimpleFIN transactions into ezBookkeeping via CLI CSV import."
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config.json (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--claim-token",
        metavar="SETUP_TOKEN",
        help="One-time: exchange a SimpleFIN Setup Token for an Access URL, then exit.",
    )
    parser.add_argument(
        "--list-accounts",
        action="store_true",
        help="Fetch and print SimpleFIN account ids for account_map setup.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and write CSV only; do not call ezBookkeeping CLI.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.claim_token:
            access_url = claim_access_url(args.claim_token)
            print(access_url)
            print(
                "\nStore this URL as simplefin_access_url in config.json. "
                "Keep it secret — it embeds your credentials.",
                file=sys.stderr,
            )
            return 0

        if args.list_accounts:
            config = load_json(args.config)
            setup_logging(config.get("error_log", DEFAULT_ERROR_LOG), args.verbose)
            list_accounts(
                config["simplefin_access_url"],
                timeout=int(config.get("request_timeout_seconds", 60)),
            )
            return 0

        return run_sync(args)
    except Exception as exc:  # noqa: BLE001
        # Ensure failures are visible even before logging is configured.
        setup_logging(DEFAULT_ERROR_LOG, verbose=True)
        logging.exception("Sync failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
