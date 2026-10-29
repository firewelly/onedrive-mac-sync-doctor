#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export the list of files that exist only locally (never uploaded).

Use this before any destructive action (domain reset, reinstall, unlink account):
those files have no cloud copy, so they vanish with the local cache.

Outputs, written next to --out:
    <out>.nul   NUL-separated relative paths, for `tar -c --null -T <out>.nul`
    <out>.tsv   human-readable "bytes<TAB>path"

    python3 od_backup_list.py Personal --out /Volumes/Backup/od-personal
    cd "<mount>" && tar -c --null -T /Volumes/Backup/od-personal.nul -f ../od.tar

Files reported as materialized=0 are placeholders: their content lives in the
cloud and cannot be read locally, so they are counted separately rather than
silently skipped.
"""
import argparse
import os
import sys

import od


def main():
    ap = argparse.ArgumentParser(description="List local-only files")
    ap.add_argument("account")
    ap.add_argument("--out", required=True, help="output path prefix")
    args = ap.parse_args()

    db = od.db_path(args.account)
    mount = od.mount_path(args.account)
    if not mount:
        raise SystemExit("cannot find the CloudStorage mount for " + args.account)

    con = od.connect_ro(db)
    index = od.Index(con, mount)
    rows = con.execute(
        "SELECT DISTINCT itemid FROM dbfs_changerecord").fetchall()
    con.close()

    files, dataless, missing = [], 0, 0
    for (itemid,) in rows:
        if index.type.get(itemid) != 1:
            continue
        path = index.item_path(itemid)
        if path is None or not os.path.isfile(path):
            missing += 1
            continue
        if index.materialized.get(itemid) == 0:
            dataless += 1
            continue
        files.append((os.path.relpath(path, mount), os.path.getsize(path)))

    files.sort()
    total = sum(size for _, size in files)

    with open(args.out + ".nul", "wb") as fh:
        for rel, _ in files:
            fh.write(rel.encode("utf-8") + b"\0")
    with open(args.out + ".tsv", "w", encoding="utf-8") as fh:
        for rel, size in files:
            fh.write(f"{size}\t{rel}\n")

    print(f"pending items      : {len(rows)}")
    print(f"files read locally : {len(files)}  ({total / 1e9:.2f} GB)")
    print(f"placeholders skipped (content in cloud, unreadable locally): {dataless}")
    print(f"missing on disk    : {missing}  (stale metadata, nothing to back up)")
    print(f"written            : {args.out}.nul / {args.out}.tsv")


if __name__ == "__main__":
    sys.exit(main())
