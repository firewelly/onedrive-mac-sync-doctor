#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit why OneDrive is stuck: find every item that never finishes syncing.

Read-only. This is the diagnostic you run first.

For each account it takes every record with ph_insync = 0, resolves its real
path on disk, and sorts it into one of five buckets:

    ghost          metadata points at a path that no longer exists
    symlink        symlinks and AppleDouble (._*) files: OneDrive never syncs these
    build-artifact files under ephemeral/, .dart_tool/, venv/, node_modules/, ...
    stubborn       the file is right there and readable, yet a change never lands
    system         OneDrive's internal temp record, always reported, ignore it

    python3 od_audit.py                 # all accounts
    python3 od_audit.py Personal
    python3 od_audit.py --details       # also print item paths
"""
import argparse
import os
import sys

import od

BUILD_MARKERS = (
    "ephemeral", ".dart_tool", "node_modules", "venv", ".conda",
    "/target/", "/dist/", "/build/", "__pycache__",
)


def classify(index, itemid, pending_type, change_types=None):
    """Sort one record into a bucket.

    change_types is the set of change_type values queued for this item. It is
    what separates "stuck" from "in flight":

        type 4 (LC_REPLACE_FILE) that never completes -> stubborn
        type 2/5/260 (create, metadata, upload)       -> pending, healthy

    A freshly created file legitimately sits at ph_insync=0 until it uploads,
    so labelling it stubborn would cry wolf.
    """
    name = od.leaf(index.name.get(itemid, ""))
    if itemid == od.TEMP_ITEMID:
        return "system", None
    if pending_type == 3 or name.startswith("._"):
        return "symlink", index.rel(itemid)
    path = index.item_path(itemid) if pending_type == 1 else index.folder_path(itemid)
    if path is None:
        return "ghost", index.rel(itemid)
    try:
        exists = os.path.lexists(path)
    except OSError:
        exists = False
    if not exists:
        return "ghost", index.rel(itemid)
    rel = index.rel(itemid)
    if any(marker in f"/{rel}" for marker in BUILD_MARKERS):
        return "build-artifact", rel
    types = change_types or set()
    if types and 4 not in types:
        return "pending", rel
    return "stubborn", rel


def audit(account, details=False, quiet=False):
    db = od.db_path(account)
    mount = od.mount_path(account)
    if not os.path.exists(db):
        if not quiet:
            print(f"[{account}] no database at {db}")
        return {}
    if not mount and not quiet:
        print(f"[{account}] cannot find a mounted CloudStorage folder; "
              f"path resolution will show everything as ghost")
    con = od.connect_ro(db)
    index = od.Index(con, mount or "/")
    rows = con.execute(
        "SELECT itemid, itemtype FROM dbfs_mftrecord "
        "WHERE tombstoned=0 AND ph_insync=0").fetchall()
    queued = {}
    for itemid, ctype in con.execute(
            "SELECT itemid, change_type FROM dbfs_changerecord"):
        queued.setdefault(itemid, set()).add(ctype)
    con.close()

    total = len(rows)
    buckets = {}
    ghost_parents = {}
    for itemid, itemtype in rows:
        kind, rel = classify(index, itemid, itemtype, queued.get(itemid))
        buckets.setdefault(kind, []).append(rel)
        if kind == "ghost" and rel:
            parts = rel.split("/")
            key = "/".join(parts[:-1]) if len(parts) > 1 else "<mount root>"
            ghost_parents[key] = ghost_parents.get(key, 0) + 1

    if quiet:
        return {kind: len(items) for kind, items in buckets.items()}

    print(f"\n=== {account} ===")
    print(f"database : {db}")
    print(f"mount    : {mount}")
    print(f"not in sync (ph_insync=0): {total}")
    for kind in ("ghost", "symlink", "build-artifact", "stubborn", "system"):
        items = buckets.get(kind, [])
        if items:
            print(f"  {kind:<15}{len(items):>6}")

    if ghost_parents:
        print("\n  ghost items cluster under these folders "
              "(fix the folder, not the files):")
        for path, n in sorted(ghost_parents.items(), key=lambda kv: -kv[1])[:15]:
            print(f"    {n:>5}  {path}")

    if details:
        for kind in ("stubborn", "symlink", "build-artifact"):
            items = buckets.get(kind, [])
            if not items:
                continue
            print(f"\n  --- {kind} ---")
            for rel in items[:40]:
                print(f"    {rel}")
            if len(items) > 40:
                print(f"    ... and {len(items) - 40} more")


def bucket_counts(account):
    """Category counts only, nothing printed. Used by summary.py."""
    return audit(account, quiet=True)


def main():
    ap = argparse.ArgumentParser(description="Audit OneDrive sync stall (read-only)")
    ap.add_argument("accounts", nargs="*", help="account names (default: all)")
    ap.add_argument("--details", action="store_true", help="list item paths")
    args = ap.parse_args()
    for name in (args.accounts or od.accounts()):
        audit(name, args.details)
    print("\nNext steps:")
    print("  ghost          -> od_tombstone.py --root <itemid>")
    print("  symlink        -> delete the links/directories, they never sync")
    print("  build-artifact -> delete the directory, it gets regenerated")
    print("  stubborn       -> recreate the file (copy, remove, copy back)")
    print("  pending        -> in flight, no action needed")
    print("  system         -> ignore")


if __name__ == "__main__":
    main()
