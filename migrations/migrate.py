#!/usr/bin/env python3
"""
Database Migration Runner
=========================
Applies numbered SQL migration scripts in order.
Tracks applied migrations in a _migrations table so each script runs only once.

Usage:
    python migrate.py                # apply all pending migrations
    python migrate.py --status       # show which migrations have been applied
    python migrate.py --reset        # drop the tracking table (does NOT undo migrations)
"""

import os
import sys
import glob
import psycopg2
from dotenv import dotenv_values

# Resolve paths relative to this script's location
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
ENV_PATH = os.path.join(PROJECT_DIR, ".env")


def get_connection():
    config = dotenv_values(ENV_PATH)
    return psycopg2.connect(
        host=config["POSTGRES_HOST"],
        port=config["POSTGRES_PORT"],
        user=config["POSTGRES_USERNAME"],
        password=config["POSTGRES_PASSWORD"],
        dbname=config["POSTGRES_DATABASE"],
    )


def ensure_tracking_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            id SERIAL PRIMARY KEY,
            filename VARCHAR(255) NOT NULL UNIQUE,
            applied_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
    """)


def get_applied(cur):
    cur.execute("SELECT filename FROM _migrations ORDER BY filename;")
    return {row[0] for row in cur.fetchall()}


def get_pending_scripts(applied):
    pattern = os.path.join(SCRIPT_DIR, "[0-9]*.sql")
    all_scripts = sorted(glob.glob(pattern))
    pending = []
    for path in all_scripts:
        name = os.path.basename(path)
        if name not in applied:
            pending.append((name, path))
    return pending


def apply_migration(conn, cur, name, path):
    with open(path, "r", encoding="utf-8") as f:
        sql = f.read()
    cur.execute(sql)
    cur.execute(
        "INSERT INTO _migrations (filename) VALUES (%s);",
        (name,),
    )
    conn.commit()
    print(f"  applied: {name}")


def cmd_migrate():
    conn = get_connection()
    cur = conn.cursor()
    ensure_tracking_table(cur)
    conn.commit()

    applied = get_applied(cur)
    pending = get_pending_scripts(applied)

    if not pending:
        print("All migrations are up to date.")
    else:
        print(f"Applying {len(pending)} pending migration(s)...")
        for name, path in pending:
            apply_migration(conn, cur, name, path)
        print("Done.")

    cur.close()
    conn.close()


def cmd_status():
    conn = get_connection()
    cur = conn.cursor()
    ensure_tracking_table(cur)
    conn.commit()

    applied = get_applied(cur)
    pending = get_pending_scripts(applied)

    # Show applied
    cur.execute("SELECT filename, applied_at FROM _migrations ORDER BY filename;")
    rows = cur.fetchall()
    if rows:
        print("Applied migrations:")
        for name, ts in rows:
            print(f"  {name:45s}  {ts}")
    else:
        print("No migrations applied yet.")

    # Show pending
    if pending:
        print(f"\nPending migrations ({len(pending)}):")
        for name, _ in pending:
            print(f"  {name}")
    else:
        print("\nNo pending migrations.")

    cur.close()
    conn.close()


def cmd_reset():
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS _migrations;")
    print("Tracking table _migrations dropped.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    flag = sys.argv[1] if len(sys.argv) > 1 else None
    if flag == "--status":
        cmd_status()
    elif flag == "--reset":
        cmd_reset()
    else:
        cmd_migrate()
