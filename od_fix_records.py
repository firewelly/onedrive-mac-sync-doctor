#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Drop pending change records that can never succeed.

When a folder is gone from disk but its records survive, its change records pile
up: the engine retries them forever, one flood of duplicated queue entries per
retry cycle. Deleting just the change records stops the flood without touching
metadata, so no deletion is ever sent to the cloud. It is the least invasive
option -- but note it does NOT fix the "Syncing N files" counter, because items
stuck there usually have no change records at all. Use od_tombstone.py for that.

    python3 od_fix_records.py Personal --root 1255264            # preview
    python3 od_fix_records.py Personal --root 1255264 --apply    # apply
"""
import argparse
import sys

import od


def main():
    ap = argparse.ArgumentParser(description="Delete impossible change records")
    ap.add_argument("account")
    ap.add_argument("--root", type=int, required=True, help="itemid of the ghost subtree")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--audit", default=None, help="write the itemid list to this file")
    args = ap.parse_args()

    db = od.db_path(args.account)
    if args.apply:
        od.assert_no_processes()

    con = od.connect_rw(db) if args.apply else od.connect_ro(db)
    try:
        ids = [r[0] for r in con.execute(
            """WITH RECURSIVE p(itemid) AS (
                   SELECT ?
                   UNION
                   SELECT r.itemid FROM dbfs_mftrecord r JOIN p ON r.parentid = p.itemid
               ) SELECT itemid FROM p""", (args.root,))]
        marks = ",".join("?" * len(ids))
        total = con.execute("SELECT COUNT(*) FROM dbfs_changerecord").fetchone()[0]
        doomed = con.execute(
            f"SELECT COUNT(*) FROM dbfs_changerecord WHERE itemid IN ({marks})",
            ids).fetchone()[0]

        print(f"subtree itemids : {len(ids)}")
        print(f"change records  : total={total} in_subtree={doomed}")
        if doomed == 0:
            print("nothing to do")
            return 0
        if not args.apply:
            print("\npreview only. re-run with --apply to delete these rows.")
            return 0

        if args.audit:
            with open(args.audit, "w") as fh:
                fh.write("\n".join(str(i) for i in ids) + "\n")

        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("CREATE TEMP TABLE ghost(itemid INTEGER PRIMARY KEY)")
            con.executemany("INSERT INTO ghost(itemid) VALUES(?)",
                            [(i,) for i in ids])
            deleted = con.execute(
                "DELETE FROM dbfs_changerecord "
                "WHERE itemid IN (SELECT itemid FROM ghost)").rowcount
            if deleted != doomed:
                raise RuntimeError(f"deleted {deleted}, expected {doomed}")
            con.execute("DROP TABLE ghost")
            con.execute("COMMIT")
        except Exception as exc:                               # noqa: BLE001
            con.execute("ROLLBACK")
            print(f"rolled back: {exc}")
            return 3

        after = con.execute("SELECT COUNT(*) FROM dbfs_changerecord").fetchone()[0]
        left = con.execute(
            f"SELECT COUNT(*) FROM dbfs_changerecord WHERE itemid IN ({marks})",
            ids).fetchone()[0]
        print(f"deleted rows    : {deleted}")
        print(f"records after   : total={after} in_subtree={left}")
        assert left == 0 and after == total - doomed
        print("integrity       :",
              con.execute("PRAGMA integrity_check").fetchone()[0])
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
