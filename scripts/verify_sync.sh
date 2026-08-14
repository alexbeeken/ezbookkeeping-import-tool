#!/bin/bash
# Verify sync/reconcile health on the host that runs the import tool.
#
# Example:
#   TOOL_DIR=/path/to/ezbookkeeping-import-tool \
#   EZ_DB=/path/to/ezbookkeeping/data/ezbookkeeping.db \
#   ./scripts/verify_sync.sh
set -euo pipefail

TOOL="${TOOL_DIR:?Set TOOL_DIR to the import tool directory}"
DB="${EZ_DB:?Set EZ_DB to the ezBookkeeping SQLite DB path}"

echo "=== cron ==="
crontab -l 2>/dev/null | grep -E 'simplefin|reconcile' || echo "(no sync/reconcile cron entries)"

echo
echo "=== last log lines ==="
for f in /var/log/simplefin_sync.log /var/log/simplefin_reconcile.log /var/log/reconcile_balances.log "$TOOL/bridge_error.log"; do
  if [[ -f "$f" ]]; then
    echo "--- $f (tail) ---"
    tail -5 "$f"
  fi
done

echo
echo "=== ez account balances ==="
python3 - "$DB" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
for name, cat, bal, cur in conn.execute(
    "SELECT name, category, balance/100.0, currency FROM account WHERE deleted=0 ORDER BY name"
):
    kind = "liability" if cat in (3, 5) else "asset"
    print(f"  {name!r:20} {bal:>14.2f} {cur} ({kind})")
PY

echo
echo "=== [sf-reconcile] transactions ==="
python3 - "$DB" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
rows = list(conn.execute("""
  SELECT t.deleted, a.name, t.type, t.amount/100.0, t.comment
  FROM "transaction" t JOIN account a ON a.account_id=t.account_id
  WHERE t.comment LIKE '%[sf-reconcile]%'
  ORDER BY t.deleted, a.name, t.transaction_time DESC
"""))
if not rows:
    print("  (none)")
for r in rows:
    print(f"  deleted={r[0]} {r[1]!r} type={r[2]} amt={r[3]:.2f} {r[4][:90]}")
PY

echo
echo "=== dry-run sync ==="
cd "$TOOL" && python3 simplefin_sync.py --dry-run 2>&1 | tail -8

echo
echo "=== dry-run reconcile ==="
cd "$TOOL" && python3 reconcile_balances.py --dry-run 2>&1 | tail -20
