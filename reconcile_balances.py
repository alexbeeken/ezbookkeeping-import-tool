#!/usr/bin/env python3
"""
Align ezBookkeeping account balances to SimpleFIN reported balances.

After transaction sync, import one Income/Expense adjustment per account whose
ledger balance disagrees with SimpleFIN (within a small tolerance). This matches
ezBookkeeping's own Reconciliation UI: Modify Balance is only for opening.

Intended for cron shortly after simplefin_sync.py. Uses balances-only=1 so the
request stays light, but it still counts toward SimpleFIN's daily /accounts quota.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import simplefin_sync as sync


# ezBookkeeping account.category values that are liabilities.
LIABILITY_CATEGORIES = {3, 5}  # credit card, debt


def dollars_to_cents(value: Decimal) -> int:
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_dollars(cents: int) -> Decimal:
    return (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))


def load_ez_accounts(db_path: str) -> dict[str, dict[str, Any]]:
    """Map account name → {account_id, balance_cents, category, currency}."""
    if not db_path or not os.path.exists(db_path):
        raise FileNotFoundError(
            f"ezbookkeeping_db not found: {db_path!r}. "
            "Set ezbookkeeping_db in config.json."
        )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT account_id, name, balance, category, currency "
            "FROM account WHERE deleted=0"
        )
        out: dict[str, dict[str, Any]] = {}
        for account_id, name, balance, category, currency in rows:
            if name in out:
                logging.warning(
                    "Duplicate ezBookkeeping account name %r; using first match",
                    name,
                )
                continue
            out[str(name)] = {
                "account_id": account_id,
                "balance_cents": int(balance),
                "category": int(category),
                "currency": str(currency or ""),
            }
        return out
    finally:
        conn.close()


def parse_sf_balance(account: dict[str, Any], *, use_available: bool) -> Decimal | None:
    raw = None
    if use_available and account.get("available-balance") not in (None, ""):
        raw = account.get("available-balance")
    else:
        raw = account.get("balance")
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        logging.error(
            "Bad SimpleFIN balance for account %s: %r", account.get("id"), raw
        )
        return None


def build_adjustment_rows(
    *,
    sf_accounts: list[dict[str, Any]],
    account_map: dict[str, str],
    ez_accounts: dict[str, dict[str, Any]],
    tolerance_cents: int,
    use_available_balance: bool,
    expense_parent: str,
    expense_category: str,
    income_parent: str,
    income_category: str,
    tzinfo,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """
    Returns (csv_rows, report_rows).
    report_rows always include every mapped account for logging.
    """
    by_id = {str(a.get("id") or ""): a for a in sf_accounts if a.get("id")}
    now = datetime.now(tz=tzinfo)
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    tz_str = sync.timezone_offset_string(now)

    rows: list[dict[str, str]] = []
    report: list[dict[str, Any]] = []

    for sf_id, ez_name in account_map.items():
        sf = by_id.get(sf_id)
        ez = ez_accounts.get(ez_name)
        entry: dict[str, Any] = {
            "sf_id": sf_id,
            "ez_name": ez_name,
            "action": "skip",
        }

        if not sf:
            entry["action"] = "missing_sf"
            logging.warning(
                "SimpleFIN account %s (%s) not in balances response", sf_id, ez_name
            )
            report.append(entry)
            continue
        if not ez:
            entry["action"] = "missing_ez"
            logging.warning(
                "ezBookkeeping account %r (mapped from %s) not found in DB",
                ez_name,
                sf_id,
            )
            report.append(entry)
            continue

        sf_bal = parse_sf_balance(sf, use_available=use_available_balance)
        if sf_bal is None:
            entry["action"] = "bad_balance"
            report.append(entry)
            continue

        # SimpleFIN and ezBookkeeping use the same sign convention:
        # assets positive, liabilities (owed) negative.
        target_cents = dollars_to_cents(sf_bal)
        ez_cents = ez["balance_cents"]
        delta = target_cents - ez_cents

        entry.update(
            {
                "sf_balance": sync.format_amount(sf_bal),
                "ez_balance": sync.format_amount(cents_to_dollars(ez_cents)),
                "target": sync.format_amount(cents_to_dollars(target_cents)),
                "delta_cents": delta,
                "is_liability": ez["category"] in LIABILITY_CATEGORIES,
                "currency": ez["currency"] or str(sf.get("currency") or ""),
            }
        )

        if abs(delta) <= tolerance_cents:
            entry["action"] = "ok"
            report.append(entry)
            continue

        txn_type = "Income" if delta > 0 else "Expense"
        abs_amount = cents_to_dollars(abs(delta))
        if txn_type == "Expense":
            parent, sub = expense_parent, expense_category
        else:
            parent, sub = income_parent, income_category

        description = (
            f"[sf-reconcile] set balance to {entry['target']} "
            f"(was {entry['ez_balance']}, SimpleFIN {entry['sf_balance']})"
        )
        currency = entry["currency"]
        if isinstance(currency, str) and currency.startswith("http"):
            currency = ""

        rows.append(
            {
                "Time": time_str,
                "Timezone": tz_str,
                "Type": txn_type,
                "Category": parent,
                "Sub Category": sub,
                "Account": ez_name,
                "Account Currency": currency,
                "Amount": sync.format_amount(abs_amount),
                "Account2": "",
                "Account2 Currency": "",
                "Account2 Amount": "",
                "Geographic Location": "",
                "Tags": "",
                "Description": description,
            }
        )
        entry["action"] = "adjust"
        entry["type"] = txn_type
        entry["amount"] = sync.format_amount(abs_amount)
        report.append(entry)

    return rows, report


def log_report(report: list[dict[str, Any]]) -> None:
    ok = sum(1 for r in report if r["action"] == "ok")
    adjust = [r for r in report if r["action"] == "adjust"]
    problems = [r for r in report if r["action"] not in ("ok", "adjust")]
    logging.info(
        "Reconcile summary: ok=%d adjust=%d problems=%d",
        ok,
        len(adjust),
        len(problems),
    )
    for r in adjust:
        logging.info(
            "  %s: %s %s → target %s (ez was %s, SF %s)",
            r["ez_name"],
            r["type"],
            r["amount"],
            r["target"],
            r["ez_balance"],
            r["sf_balance"],
        )
    for r in problems:
        logging.warning("  %s (%s): %s", r["ez_name"], r["sf_id"], r["action"])


def run_reconcile(args: argparse.Namespace) -> int:
    config = sync.load_config(args.config)
    error_log = config.get("error_log", sync.DEFAULT_ERROR_LOG)
    if error_log and not os.path.isabs(error_log):
        error_log = os.path.join(sync.SCRIPT_DIR, error_log)
    sync.setup_logging(error_log, verbose=args.verbose)

    db_path = config.get("ezbookkeeping_db")
    if db_path and not os.path.isabs(db_path):
        db_path = os.path.join(sync.SCRIPT_DIR, db_path)

    timeout = int(config.get("request_timeout_seconds", 60))
    tolerance = int(config.get("reconcile_tolerance_cents", 1))
    use_available = bool(config.get("use_available_balance", False))

    expense_parent = config.get(
        "reconcile_expense_parent",
        config.get("default_expense_parent", ""),
    )
    expense_category = config.get(
        "reconcile_expense_category",
        config.get("default_expense_category", "Uncategorized"),
    )
    income_parent = config.get(
        "reconcile_income_parent",
        config.get("default_income_parent", ""),
    )
    income_category = config.get(
        "reconcile_income_category",
        config.get("default_income_category", "Uncategorized"),
    )

    logging.info("Fetching SimpleFIN balances (balances-only)")
    payload = sync.fetch_accounts(
        config["simplefin_access_url"],
        start_ts=None,
        end_ts=None,
        timeout=timeout,
        include_pending=False,
        balances_only=True,
    )

    ez_accounts = load_ez_accounts(db_path)
    tzinfo = sync.resolve_timezone(config.get("timezone"))
    account_map = {str(k): str(v) for k, v in config["account_map"].items()}
    if config.get("reconcile_accounts"):
        allow = {str(x) for x in config["reconcile_accounts"]}
        account_map = {k: v for k, v in account_map.items() if k in allow}

    rows, report = build_adjustment_rows(
        sf_accounts=list(payload.get("accounts") or []),
        account_map=account_map,
        ez_accounts=ez_accounts,
        tolerance_cents=tolerance,
        use_available_balance=use_available,
        expense_parent=str(expense_parent or ""),
        expense_category=str(expense_category),
        income_parent=str(income_parent or ""),
        income_category=str(income_category),
        tzinfo=tzinfo,
    )
    log_report(report)

    if not rows:
        logging.info("No balance adjustments needed.")
        return 0

    keep_csv = config.get("reconcile_keep_csv_path") or config.get("keep_csv_path")
    temp_dir = None
    if keep_csv:
        sync.write_csv(keep_csv, rows)
        csv_path = keep_csv
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="sf_reconcile_")
        csv_path = os.path.join(temp_dir.name, "reconcile.csv")
        sync.write_csv(csv_path, rows)

    try:
        if args.dry_run:
            preview = os.path.join(sync.SCRIPT_DIR, "dry_run_reconcile.csv")
            sync.write_csv(preview, rows)
            logging.info(
                "Dry run: %d adjustment(s); CSV at %s (CLI import skipped)",
                len(rows),
                preview,
            )
            return 0

        sync.import_via_cli(
            cli_path=config.get("ezbookkeeping_cli", "ezbookkeeping"),
            username=config["ezbookkeeping_username"],
            csv_path=csv_path,
            working_directory=config.get("ezbookkeeping_working_directory"),
        )
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    logging.info("Reconcile import complete (%d adjustment(s)).", len(rows))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import Income/Expense rows so ezBookkeeping balances match SimpleFIN."
    )
    parser.add_argument(
        "--config",
        default=sync.DEFAULT_CONFIG_PATH,
        help=f"Path to config.json (default: {sync.DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute adjustments and write dry_run_reconcile.csv; do not import.",
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
        return run_reconcile(args)
    except Exception as exc:  # noqa: BLE001
        sync.setup_logging(sync.DEFAULT_ERROR_LOG, verbose=True)
        logging.exception("Reconcile failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
