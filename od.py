#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for inspecting an OneDrive-for-macOS local sync database.

No third-party dependencies. Everything is discovered from the system, so no
paths or account names are hardcoded.

Layout this relies on (OneDrive for Mac, standalone build):

    ~/Library/Group Containers/UBF8T346G9.OneDriveStandaloneSuite/
        .Dbfs.Dbfs_<Account>.noindex/dbfs.db      <- metadata database
        FileProviderLogs/                          <- extension logs

    ~/Library/Application Support/OneDrive/settings/<Account>/*.ini
                                                   <- records the mount path

The mount point itself lives under ~/Library/CloudStorage/OneDrive-<something>.
Its exact name depends on the account, so we read it out of the .ini above.
"""
from __future__ import annotations

import glob
import os
import re
import sqlite3

GROUP_CONTAINER = os.path.expanduser(
    "~/Library/Group Containers/UBF8T346G9.OneDriveStandaloneSuite")
SETTINGS_DIR = os.path.expanduser(
    "~/Library/Application Support/OneDrive/settings")

# OneDrive stores item names as "<prefix><display name>" for some entries.
NAME_PREFIX = "//v0/<34>//"

# Special system records that are exempt from normal handling.
ROOT_ITEMID = 1
TEMP_ITEMID = 9223372036854775708


def leaf(name: str) -> str:
    """Strip OneDrive's internal name prefix."""
    return name[len(NAME_PREFIX):] if name and name.startswith(NAME_PREFIX) else name


def accounts() -> list[str]:
    """Namespace names for every local OneDrive account, e.g. ['Business1', 'Personal']."""
    out = []
    for path in glob.glob(os.path.join(GROUP_CONTAINER, ".Dbfs.Dbfs_*.noindex")):
        base = os.path.basename(path)
        out.append(base[len(".Dbfs.Dbfs_"):-len(".noindex")])
    return sorted(out)


def db_path(account: str) -> str:
    return os.path.join(GROUP_CONTAINER, f".Dbfs.Dbfs_{account}.noindex", "dbfs.db")


def mount_path(account: str) -> str | None:
    """Find the CloudStorage mount for an account by reading its settings .ini."""
    candidates = set()
    for ini in glob.glob(os.path.join(SETTINGS_DIR, account, "*.ini")):
        try:
            with open(ini, errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        for raw in re.findall(r'"(~/Library/CloudStorage/[^"]+)"', text):
            candidates.add(os.path.expanduser(raw))
    for cand in sorted(candidates):
        if os.path.isdir(cand):
            return cand
    return None


def connect_ro(path: str) -> sqlite3.Connection:
    """Open read-only without taking write locks."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=1")
    return con


def connect_rw(path: str) -> sqlite3.Connection:
    """Open for modification. Caller must ensure OneDrive is fully stopped."""
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA foreign_keys=OFF")
    return con


def assert_no_processes() -> None:
    """Refuse to continue if any OneDrive process is alive."""
    import subprocess
    patterns = ["OneDrive.app/Contents/MacOS/OneDrive", "OneDrive File Provider"]
    alive = []
    for pat in patterns:
        r = subprocess.run(["/usr/bin/pgrep", "-f", pat],
                           capture_output=True, text=True)
        if r.stdout.strip():
            alive.append(pat)
    if alive:
        raise SystemExit(
            "OneDrive is still running (%s). Quit it first:\n"
            "  osascript -e 'tell application \"OneDrive\" to quit'\n"
            "  pkill -f 'OneDrive File Provider'" % ", ".join(alive))


class Index:
    """In-memory view of the live (non-tombstoned) metadata tree."""

    def __init__(self, con: sqlite3.Connection, mount: str):
        self.mount = mount
        self.parent: dict[int, int] = {}
        self.type: dict[int, int] = {}
        self.name: dict[int, str] = {}
        self.materialized: dict[int, int] = {}
        rows = con.execute(
            "SELECT itemid, parentid, itemtype, name, materialized "
            "FROM dbfs_mftrecord WHERE tombstoned=0")
        for itemid, parentid, itemtype, name, materialized in rows:
            self.parent[itemid] = parentid
            self.type[itemid] = itemtype
            self.name[itemid] = name
            self.materialized[itemid] = materialized
        self._folder_cache: dict[int, str | None] = {ROOT_ITEMID: mount}

    def _chain(self, itemid: int) -> list[int]:
        chain, cur, seen = [], itemid, set()
        while cur not in self._folder_cache and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            if cur == ROOT_ITEMID:
                break
            nxt = self.parent.get(cur)
            if nxt is None or nxt == cur:
                break
            cur = nxt
        return chain

    def folder_path(self, itemid: int) -> str | None:
        """Absolute path of a folder itemid, or None if it does not exist on disk."""
        if itemid in self._folder_cache:
            return self._folder_cache[itemid]
        chain = self._chain(itemid)
        base = self._folder_cache.get(chain[-1] if chain else ROOT_ITEMID, self.mount)
        for cid in reversed(chain):
            if base is None:
                self._folder_cache[cid] = None
                continue
            path = os.path.join(base, leaf(self.name.get(cid, "")))
            try:
                ok = os.path.isdir(path)
            except OSError:
                ok = False
            base = path if ok else None
            self._folder_cache[cid] = base
        return self._folder_cache[itemid]

    def item_path(self, itemid: int) -> str | None:
        """Absolute path of a file itemid, or None if its parent is missing."""
        parent = self.folder_path(self.parent.get(itemid, ROOT_ITEMID))
        if parent is None:
            return None
        return os.path.join(parent, leaf(self.name.get(itemid, "")))

    def rel(self, itemid: int) -> str:
        path = self.item_path(itemid)
        if path is None:
            return f"<ghost>/{leaf(self.name.get(itemid, ''))}"
        return path[len(self.mount) + 1:] if path.startswith(self.mount) else path
