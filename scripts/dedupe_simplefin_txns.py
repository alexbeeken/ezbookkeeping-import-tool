#!/usr/bin/env python3
"""One-shot: soft-delete duplicate SimpleFIN imports and reverse account balances."""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import time
from collections import defaultdict

SF_RE = re.compile(r"\[sf:([^\]]+)\]")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Soft-delete duplicate [sf:…] imports and reverse account balances."
    )
    parser.add_argument(
        "db_path",
        help="Path to ezBookkeeping SQLite DB "
        "(example: /path/to/ezbookkeeping/data/ezbookkeeping.db)",
    )
    args = parser.parse_args()
    db_path = args.db_path

    bak = f"{db_path}.bak-dedupe-{time.strftime('%Y%m%d%H%M%S')}"
    shutil.copy2(db_path, bak)
    print("backup", bak)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    rows = cur.execute(
        'SELECT transaction_id, comment FROM "transaction" '
        'WHERE deleted=0 AND comment LIKE "%[sf:%"'
    ).fetchall()

    by_sf: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        m = SF_RE.search(r["comment"] or "")
        if m:
            by_sf[m.group(1)].append(r["transaction_id"])

    dup_groups = {k: v for k, v in by_sf.items() if len(v) > 1}
    print(f"duplicate sf ids: {len(dup_groups)}")

    # DB types: 1=modify, 2=income (+bal), 3=expense (-bal), 4=xfer_out, 5=xfer_in
    balance_delta: dict[int, int] = defaultdict(int)
    deleted: list[int] = []

    for _sf, ids in dup_groups.items():
        ids_sorted = sorted(ids)
        for tid in ids_sorted[:-1]:
            full = cur.execute(
                "SELECT transaction_id, type, account_id, related_account_id, "
                "amount, related_account_amount FROM \"transaction\" "
                "WHERE transaction_id=?",
                (tid,),
            ).fetchone()
            deleted.append(full["transaction_id"])
            t = full["type"]
            aid = full["account_id"]
            rid = full["related_account_id"]
            amt = full["amount"]
            ramt = full["related_account_amount"]
            if t == 2:
                balance_delta[aid] -= amt
            elif t == 3:
                balance_delta[aid] += amt
            elif t == 4:
                balance_delta[aid] += amt
                if rid:
                    balance_delta[rid] -= ramt
            elif t == 5:
                balance_delta[aid] -= amt
                if rid:
                    balance_delta[rid] += ramt
            else:
                print("WARN unhandled type", t, full["transaction_id"])

    print(f"soft-deleting {len(deleted)} rows")
    print("balance adjustments:")
    for aid, delta in sorted(balance_delta.items()):
        name = cur.execute(
            "SELECT name FROM account WHERE account_id=?", (aid,)
        ).fetchone()[0]
        print(f"  {name}: {delta / 100:+.2f}")

    now = int(time.time())
    cur.execute("BEGIN")
    for tid in deleted:
        cur.execute(
            'UPDATE "transaction" SET deleted=1, deleted_unix_time=?, '
            "updated_unix_time=? WHERE transaction_id=?",
            (now, now, tid),
        )
    for aid, delta in balance_delta.items():
        if delta == 0:
            continue
        cur.execute(
            "UPDATE account SET balance = balance + ?, updated_unix_time=? "
            "WHERE account_id=?",
            (delta, now, aid),
        )
    conn.commit()

    left = cur.execute(
        """
        WITH x AS (
          SELECT CASE WHEN instr(comment, '[sf:') > 0
            THEN substr(comment, instr(comment, '[sf:') + 4,
                 length(comment) - instr(comment, '[sf:') - 4)
            ELSE NULL END AS sf
          FROM "transaction"
          WHERE deleted=0 AND comment LIKE '%[sf:%'
        )
        SELECT COUNT(*) FROM (
          SELECT sf FROM x WHERE sf IS NOT NULL GROUP BY sf HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    live = cur.execute(
        'SELECT COUNT(*) FROM "transaction" WHERE deleted=0'
    ).fetchone()[0]
    print(f"remaining dup sf groups: {left}; live txns: {live}")
    print("account balances now:")
    for r in cur.execute(
        "SELECT name, printf('%.2f', balance/100.0) FROM account "
        "WHERE deleted=0 ORDER BY name"
    ):
        print(f"  {r[0]}: {r[1]}")
    conn.close()
    print("done")


if __name__ == "__main__":
    main()
