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
    start_ts: int,
    end_ts: int | None,
    timeout: int,
    include_pending: bool,
) -> dict[str, Any]:
    requests = require_requests()
    base_url, username, password = parse_access_url(access_url)
    params: dict[str, Any] = {
        "version": "2",
        "start-date": start_ts,
    }
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


def format_amount(value: Decimal) -> str:
    """Format amount without thousands separators; keep up to 2 decimal places."""
    quantized = value.quantize(Decimal("0.01"))
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def classify_transaction(
    amount: Decimal,
) -> tuple[str, Decimal]:
    """
    SimpleFIN: positive = money in (Income), negative = money out (Expense).
    ezBookkeeping Amount is absolute; Type carries direction.
    """
    if amount < 0:
        return "Expense", abs(amount)
    return "Income", abs(amount)


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


def build_csv_rows(
    accounts_payload: dict[str, Any],
    account_map: dict[str, str],
    seen: set[str],
    default_expense_category: str,
    default_income_category: str,
    default_expense_parent: str,
    default_income_parent: str,
    tzinfo,
    skip_pending: bool,
) -> tuple[list[dict[str, str]], list[str]]:
    rows: list[dict[str, str]] = []
    new_keys: list[str] = []
    skipped_unmapped = 0

    for account in accounts_payload.get("accounts") or []:
        sf_id = str(account.get("id") or "")
        if not sf_id:
            continue
        ez_account = account_map.get(sf_id)
        if not ez_account:
            if account.get("transactions"):
                skipped_unmapped += 1
                logging.warning(
                    "Skipping SimpleFIN account id=%s name=%r (not in account_map)",
                    sf_id,
                    account.get("name"),
                )
            continue

        currency = str(account.get("currency") or "")
        # Skip custom-currency URLs; ezBookkeeping expects ISO codes.
        if currency.startswith("http"):
            logging.warning(
                "Account %s uses custom currency URL; leaving Account Currency blank",
                sf_id,
            )
            currency = ""

        for txn in account.get("transactions") or []:
            if skip_pending and txn.get("pending"):
                continue
            key = txn_key(sf_id, txn)
            if key in seen:
                continue

            try:
                amount = Decimal(str(txn.get("amount", "0")))
            except InvalidOperation:
                logging.error("Bad amount on txn %s: %r", key, txn.get("amount"))
                continue

            if amount == 0:
                logging.debug("Skipping zero-amount txn %s", key)
                continue

            txn_type, abs_amount = classify_transaction(amount)
            posted = int(txn.get("posted") or 0)
            if posted <= 0:
                logging.warning("Skipping txn %s with missing posted timestamp", key)
                continue

            when = datetime.fromtimestamp(posted, tz=timezone.utc).astimezone(tzinfo)
            if txn_type == "Expense":
                parent = default_expense_parent
                sub = default_expense_category
            else:
                parent = default_income_parent
                sub = default_income_category

            description = str(txn.get("description") or "").replace("\n", " ").strip()
            # Keep SimpleFIN id in description for debugging / manual dedup.
            if description:
                description = f"{description} [sf:{txn.get('id')}]"
            else:
                description = f"[sf:{txn.get('id')}]"

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

    if skipped_unmapped:
        logging.warning(
            "%d SimpleFIN account(s) had transactions but no account_map entry",
            skipped_unmapped,
        )
    return rows, new_keys


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
    """Keep newly imported keys plus a capped remainder of prior keys."""
    retained = set(keep)
    for key in seen:
        if len(retained) >= max_entries:
            break
        retained.add(key)
    return retained


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
    state = load_json(state_path, default={"imported_ids": []})
    seen = set(state.get("imported_ids") or [])

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
    rows, new_keys = build_csv_rows(
        accounts_payload=payload,
        account_map={str(k): str(v) for k, v in config["account_map"].items()},
        seen=seen,
        default_expense_category=config.get("default_expense_category", "Uncategorized"),
        default_income_category=config.get("default_income_category", "Uncategorized"),
        default_expense_parent=config.get("default_expense_parent", ""),
        default_income_parent=config.get("default_income_parent", ""),
        tzinfo=tzinfo,
        skip_pending=not include_pending,
    )

    if not rows:
        logging.info("No new transactions to import.")
        state["last_run"] = end.isoformat()
        save_json(state_path, state)
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

    max_entries = int(config.get("max_seen_ids", 5000))
    seen.update(new_keys)
    state["imported_ids"] = sorted(prune_seen(seen, set(new_keys), max_entries))
    state["last_run"] = end.isoformat()
    state["last_import_count"] = len(new_keys)
    save_json(state_path, state)
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
