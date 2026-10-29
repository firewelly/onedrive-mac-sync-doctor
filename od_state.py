#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Snapshot the state of every local OneDrive account (read-only).

Outputs JSON with client version, running processes, and per-account counters,
including the number that drives the "Syncing N files" UI indicator.

    python3 od_state.py
    python3 od_state.py Personal --pretty
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

import od

APP = "/Applications/OneDrive.app"


def sh(cmd, timeout=120):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception as exc:                                  # noqa: BLE001
        return f"<error: {exc}>"


def client_version():
    plist = os.path.join(APP, "Contents", "Info.plist")
    if not os.path.exists(plist):
        return None
    return sh(["/usr/bin/plutil", "-extract", "CFBundleShortVersionString",
               "raw", plist]) or None


def processes():
    out = sh(["/bin/ps", "-axo", "pid,pcpu,etime,comm"])
    return [ln.strip() for ln in out.splitlines() if "OneDrive" in ln]


def q(con, sql, default=None):
    try:
        row = con.execute(sql).fetchone()
        return row[0] if row else default
    except Exception:                                          # noqa: BLE001
        return default


def account_state(name):
    db = od.db_path(name)
    info = {
        "db_path": db,
        "mount": od.mount_path(name),
        "db_exists": os.path.exists(db),
    }
    if not info["db_exists"]:
        return info
    info["db_size_mb"] = round(os.path.getsize(db) / 1e6, 1)
    con = od.connect_ro(db)
    try:
        info.update({
            # The count the UI shows as "Syncing N files".
            "not_in_sync": q(con, "SELECT COUNT(*) FROM dbfs_mftrecord "
                                  "WHERE tombstoned=0 AND ph_insync=0"),
            "change_records": q(con, "SELECT COUNT(*) FROM dbfs_changerecord"),
            "pending_items": q(con, "SELECT COUNT(DISTINCT itemid) FROM dbfs_changerecord"),
            "live_records": q(con, "SELECT COUNT(*) FROM dbfs_mftrecord WHERE tombstoned=0"),
            "tombstoned_records": q(con, "SELECT COUNT(*) FROM dbfs_mftrecord WHERE tombstoned=1"),
            "integrity": q(con, "PRAGMA integrity_check"),
            "schema_version": q(con, "SELECT value FROM __oddbm_schema WHERE key='version'")
                              if _has_table(con, "__oddbm_schema") else None,
        })
        info["not_in_sync_by_type"] = {
            str(t): n for t, n in con.execute(
                "SELECT itemtype, COUNT(*) FROM dbfs_mftrecord "
                "WHERE tombstoned=0 AND ph_insync=0 GROUP BY 1")
        }
    finally:
        con.close()
    return info


def _has_table(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("accounts", nargs="*", help="account names (default: all)")
    ap.add_argument("--pretty", action="store_true", help="indent the JSON")
    args = ap.parse_args()

    targets = args.accounts or od.accounts()
    if not targets:
        raise SystemExit("No OneDrive accounts found under:\n  " + od.GROUP_CONTAINER)

    state = {
        "collected_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "onedrive_version": client_version(),
        "pkg_info": sh(["/usr/sbin/pkgutil", "--pkg-info", "com.microsoft.OneDrive"],
                       timeout=60) or None,
        "processes": processes(),
        "load": sh(["/usr/bin/uptime"], timeout=30),
        "accounts": {name: account_state(name) for name in targets},
    }
    json.dump(state, sys.stdout,
              ensure_ascii=False,
              indent=2 if args.pretty else None)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
