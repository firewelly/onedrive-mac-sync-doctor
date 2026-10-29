#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mark a subtree as deleted in OneDrive's metadata (bottom-up tombstone).

Use when a folder was removed from disk but its records are still marked as
present, which makes the sync engine retry them forever.

    # 1. find the folder's itemid
    sqlite3 <dbfs.db> "SELECT itemid, name FROM dbfs_mftrecord
                       WHERE name LIKE '%//_trash' AND tombstoned=0;"

    # 2. preview
    python3 od_tombstone.py Personal --root 1293203

    # 3. apply (OneDrive must be stopped)
    python3 od_tombstone.py Personal --root 1293203 --apply

Why bottom-up: the trigger enforce_constraint_update_tombstone refuses to
tombstone a record that still has a non-tombstoned child, so children must be
processed first. This script computes depth and updates deepest-first inside a
single transaction.

What it does NOT do: it does not insert any change record, so it never asks the
cloud to delete anything. It only makes local metadata agree with the disk.
"""
import argparse
import sys

import od


def subtree(con, root):
    return [r[0] for r in con.execute(
        """WITH RECURSIVE p(itemid) AS (
               SELECT ?
               UNION
               SELECT r.itemid FROM dbfs_mftrecord r JOIN p ON r.parentid = p.itemid
           ) SELECT itemid FROM p""", (root,))]


def depth_of(con, itemid, cache):
    if itemid in cache:
        return cache[itemid]
    row = con.execute("SELECT parentid FROM dbfs_mftrecord WHERE itemid=?",
                      (itemid,)).fetchone()
    if row is None or row[0] == itemid:
        cache[itemid] = 0
    else:
        cache[itemid] = depth_of(con, row[0], cache) + 1
    return cache[itemid]


def main():
    ap = argparse.ArgumentParser(description="Tombstone a ghost subtree")
    ap.add_argument("account", help="account name, e.g. Personal")
    ap.add_argument("--root", type=int, required=True, help="itemid of the subtree root")
    ap.add_argument("--apply", action="store_true", help="actually modify the database")
    args = ap.parse_args()

    db = od.db_path(args.account)
    if args.apply:
        od.assert_no_processes()

    con = od.connect_rw(db) if args.apply else od.connect_ro(db)
    try:
        ids = subtree(con, args.root)
        live = [r[0] for r in con.execute(
            "SELECT itemid FROM dbfs_mftrecord WHERE tombstoned=0 AND itemid IN (%s)"
            % ",".join("?" * len(ids)), ids)]
        pending = con.execute(
            "SELECT COUNT(*) FROM dbfs_changerecord WHERE itemid IN (%s)"
            % ",".join("?" * len(ids)), ids).fetchone()[0]

        print(f"subtree records : {len(ids)}")
        print(f"still alive     : {len(live)}")
        print(f"change records  : {pending}  (left untouched)")

        if not live:
            print("nothing to do")
            return 0
        if not args.apply:
            print("\npreview only. re-run with --apply to modify.")
            return 0

        cache = {}
        order = sorted(live, key=lambda i: -depth_of(con, i, cache))
        print(f"max depth       : {max(cache.values()) if cache else 0}")

        con.execute("BEGIN IMMEDIATE")
        try:
            for itemid in order:
                con.execute("UPDATE dbfs_mftrecord SET tombstoned=1 WHERE itemid=?",
                            (itemid,))
            con.execute("COMMIT")
        except Exception as exc:                               # noqa: BLE001
            con.execute("ROLLBACK")
            print(f"rolled back: {exc}")
            return 3

        left = con.execute(
            "SELECT COUNT(*) FROM dbfs_mftrecord WHERE tombstoned=0 AND itemid IN (%s)"
            % ",".join("?" * len(ids)), ids).fetchone()[0]
        print(f"tombstoned      : {len(order)}")
        print(f"remaining alive : {left}")
        print("integrity       :",
              con.execute("PRAGMA integrity_check").fetchone()[0])
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
