"""Seed demo printer_data rows into the hub's LOCAL database (local-mode testing).

This is a LOCAL-MODE test helper. It writes straight into hub\\data\\hub.db so the
dashboard Catalog + the printer prompt dropdowns have something to show WITHOUT a
Supabase connection. It does NOT use the hub's HTTP API and needs no enroll key
(the enroll key is only for enrolling a printer). Safe to run while the hub
service is running (WAL + busy_timeout handle the concurrent write); the rows
appear on the next dashboard refresh.

Once you connect Supabase (Settings tab), the real printer_data sync REPLACES
whatever this seeded - so these demo rows are transient by design.

Run it via seed-demo-data.bat (double-click) or:  python seed-demo-data.py
"""
import os, sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("HUB_DATA_DIR") or os.path.join(HERE, "data")
DB = os.path.join(DATA, "hub.db")

# (registration_number, department_name, equipment_name, status, test_method, test_parameter)
DEMO = [
    # Chemistry / GCMS  - two methods, multiple parameters (cascade demo)
    ("REG-2026-0001", "Chemistry", "GCMS", "open", "GC-HS-001",  "Residual Solvents"),
    ("REG-2026-0001", "Chemistry", "GCMS", "open", "GC-HS-001",  "Water Content"),
    ("REG-2026-0001", "Chemistry", "GCMS", "open", "GC-FID-002", "Assay"),
    ("REG-2026-0001", "Chemistry", "GCMS", "open", "GC-FID-002", "Purity"),
    ("REG-2026-0004", "Chemistry", "GCMS", "open", "GC-HS-001",  "Residual Solvents"),
    # Chemistry / LCMS
    ("REG-2026-0002", "Chemistry", "LCMS", "open", "LC-MS-010",  "Related Substances"),
    ("REG-2026-0002", "Chemistry", "LCMS", "open", "LC-MS-010",  "Assay"),
    # Microbiology / GCMS  (same equipment, different department)
    ("REG-2026-0003", "Microbiology", "GCMS", "open", "GC-BIO-005", "Sterility"),
    # A CLOSED registration - proves closed rows are hidden from the prompt
    ("REG-2026-0009", "Chemistry", "GCMS", "closed", "GC-HS-001", "Residual Solvents"),
]

DDL = """
CREATE TABLE IF NOT EXISTS printer_data (
    registration_number TEXT,
    department_name     TEXT,
    equipment_name      TEXT,
    status              TEXT,
    test_method         TEXT,
    test_parameter      TEXT,
    UNIQUE (registration_number, department_name, test_method, test_parameter)
);
"""

def main():
    if not os.path.isdir(DATA):
        os.makedirs(DATA, exist_ok=True)
    print("Hub database: %s" % DB)
    con = sqlite3.connect(DB, timeout=10)
    try:
        con.execute("PRAGMA busy_timeout=8000")
        con.executescript(DDL)
        con.executemany(
            "INSERT OR IGNORE INTO printer_data (registration_number, department_name, "
            "equipment_name, status, test_method, test_parameter) VALUES (?,?,?,?,?,?)", DEMO)
        con.commit()
        total = con.execute("SELECT COUNT(*) FROM printer_data").fetchone()[0]
        pairs = con.execute("SELECT DISTINCT department_name, equipment_name FROM printer_data "
                            "WHERE status='open' ORDER BY 1,2").fetchall()
    finally:
        con.close()
    print("Seeded %d demo rows (INSERT OR IGNORE - safe to re-run). printer_data now holds %d rows.\n"
          % (len(DEMO), total))
    print("Department / Equipment pairs to pick when you install a printer:")
    for d, e in pairs:
        print("   - %s / %s" % (d, e))
    print("\nOpen the dashboard -> Catalog tab and Refresh to see the rows.")
    print("Then install a printer for e.g. Chemistry / GCMS and print - you should get")
    print("REG-2026-0001 with methods GC-HS-001 and GC-FID-002 in the dropdowns.")
    print("(REG-2026-0009 is 'closed' and will NOT appear - that is expected.)")

if __name__ == "__main__":
    main()
