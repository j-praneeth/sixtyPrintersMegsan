"""Clear demo printer_data rows from the hub's LOCAL database (undo the seed).

Default: removes ONLY the demo registrations that seed-demo-data added
(REG-2026-000x) - it will not touch anything synced from Supabase.
Pass "all" (or --all) to wipe the ENTIRE printer_data table instead.

Run via clear-demo-data.bat (double-click), or:
    python clear-demo-data.py           (demo rows only)
    python clear-demo-data.py all       (everything)
"""
import os, sys, sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("HUB_DATA_DIR") or os.path.join(HERE, "data")
DB = os.path.join(DATA, "hub.db")

# The registration numbers seed-demo-data.py inserts.
DEMO_REGS = ["REG-2026-0001", "REG-2026-0002", "REG-2026-0003",
             "REG-2026-0004", "REG-2026-0009"]

def main():
    mode = (sys.argv[1].lstrip("-").lower() if len(sys.argv) > 1 else "demo")
    wipe_all = mode == "all"
    if not os.path.isfile(DB):
        print("No hub database at %s - nothing to clear." % DB)
        return
    print("Hub database: %s" % DB)
    con = sqlite3.connect(DB, timeout=10)
    try:
        con.execute("PRAGMA busy_timeout=8000")
        # printer_data may not exist yet if the hub never started.
        if not con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                           "AND name='printer_data'").fetchone():
            print("printer_data table does not exist yet - nothing to clear.")
            return
        if wipe_all:
            cur = con.execute("DELETE FROM printer_data")
        else:
            qmarks = ",".join("?" * len(DEMO_REGS))
            cur = con.execute(
                "DELETE FROM printer_data WHERE registration_number IN (%s)" % qmarks,
                DEMO_REGS)
        removed = cur.rowcount
        con.commit()
        remaining = con.execute("SELECT COUNT(*) FROM printer_data").fetchone()[0]
    finally:
        con.close()
    what = "ALL printer_data" if wipe_all else "demo (REG-2026-000x)"
    print("Removed %d %s row(s). printer_data now holds %d row(s)." % (removed, what, remaining))
    print("Refresh the dashboard Catalog tab to confirm.")
    if not wipe_all and remaining:
        print("(Rows remain - they are not demo rows. Run  clear-demo-data.bat all  to wipe everything.)")

if __name__ == "__main__":
    main()
