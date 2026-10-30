# OneDrive for Mac: what "Syncing N files" is actually stuck on — and how to fix it

Diagnose and repair the OneDrive-for-macOS sync stall, without resetting the
account.

> Measured on a real machine, 2026-10-30. Two accounts: one stuck at 307 files,
> one at 1. I spent a night convinced it was a client version regression and
> wasted it on a downgrade and a rollback. The real cause was a batch of
> metadata records pointing at paths that had been deleted months earlier.

**Contents**

- [The symptom](#the-symptom)
- [Where the state actually lives](#where-the-state-actually-lives)
- [What N really is](#what-n-really-is)
- [The five kinds of stuck item](#the-five-kinds-of-stuck-item)
- [Fixing it, cheapest first](#fixing-it-cheapest-first)
- [Four traps](#four-traps)
- [Why it comes back](#why-it-comes-back)
- [Prevention and a 1-minute health check](#prevention-and-a-1-minute-health-check)
- [Acceptance criteria](#acceptance-criteria)
- [The tools in this repo](#the-tools-in-this-repo)

Chinese version of this write-up: [`docs/onedrive-mac-sync-stuck.zh-CN.md`](docs/onedrive-mac-sync-stuck.zh-CN.md)

---

## The symptom

OneDrive for Mac spins in the menu bar and reports *Syncing N files*. N does not
move. Not up, not down, for hours.

Alongside it:

- Edits do not upload; brand-new files never leave the machine
- Both `OneDrive File Provider` processes burn 70–90% CPU; the fan comes on
- `~/Library/Logs/OneDrive/<account>/` fills at a rate of hundreds of log files
  per day
- macOS writes a diagnostic report: *2,147 MB of file-backed memory dirtied over
  39 seconds*
- Restarting OneDrive does nothing. Restarting the machine does nothing.

It is tempting to blame the client version, especially if it updated recently.
**In this case it was not the version.**

---

## Where the state actually lives

Three layers, and the failure lives in the first one.

### 1. The metadata database

```
~/Library/Group Containers/UBF8T346G9.OneDriveStandaloneSuite/
├── .Dbfs.Dbfs_<account>.noindex/dbfs.db     # one per account, ~1 GB
└── FileProviderLogs/
```

A SQLite database with a million-plus rows (schema `version=7`). Two tables
carry the whole story:

**`dbfs_mftrecord`** — "what I believe exists on the filesystem".

| Column | Meaning |
|---|---|
| `itemid` / `parentid` | Self-referencing tree; the root is `itemid=1, parentid=1` |
| `name` | Name, sometimes prefixed with OneDrive's internal `//v0/<34>//` |
| `tombstoned` | 0 = present, 1 = deleted |
| `materialized` | 1 = content is local, 0 = placeholder (content is in the cloud) |
| **`ph_insync`** | **0 = not synced — this is what the UI counts** |
| `content_hash` / `size` | Used to diff against the cloud |

**`dbfs_changerecord`** — "local changes still waiting to be uploaded".

The relationship between them is the entire bug:

> A change record says *"this file needs uploading"*. The metadata says *"this
> file lives here"*. Once the metadata describes a path that no longer exists on
> disk, that change can never complete.

### 2. The File Provider domain

The system-side mount under `~/Library/CloudStorage/`, managed by macOS. It also
records *which version of OneDrive created or upgraded it* — which becomes a trap
later.

### 3. The sync engine logs

```
~/Library/Logs/OneDrive/<account>/SyncEngine-*.odl    # binary; read with strings
```

A stalled account loops like this:

```
ProcessLocalChange LC_REPLACE_FILE 3
UnexpectedFailure!
PostponedChanges.cpp  SetPostponedStatus
PostponedChanges.cpp  PostponeChange
Diagnostics.cpp       RunDiagnostics
Diagnostics.cpp       RetryPostponedChanges     ← repeat forever
```

A few lines further up is the actual error source:

```
DbfsMacboxGetUserVisibleItemPath
Errno::2 (0x2) [NotFound] [source:0x049d02d2]
...
OdfsDbfs::-615838174 (0xdb4b0e22) [Permanent]
FileForRead.cpp       Read
ReplaceFile.cpp       CompareDiskFileAgainstDB
```

`DbfsMacboxGetUserVisibleItemPath → NotFound` means: **the itemid in the database
cannot be mapped back to a path on disk.**

---

## What N really is

The single most useful fact in this write-up:

> **The UI's "Syncing N files" ≈ `SELECT COUNT(*) FROM dbfs_mftrecord WHERE tombstoned = 0 AND ph_insync = 0`**

Measured:

| Account | `ph_insync = 0` | UI showed |
|---|---|---|
| A | 346 | 307 |
| B | 9 | 1 |

Not exactly equal — the UI counts the subset it believes has real work to do —
but the same order of magnitude and the same trend, which is enough to locate
and verify a fix.

**A counter-intuitive corollary:** the number of rows in `dbfs_changerecord` is
**not** a progress indicator. The stuck account had 25,020 change records while
the UI showed 307. The other 24,000-odd were already reconciled with the cloud
and only needed a metadata epilogue. I watched that number for a long time and
learned nothing.

---

## The five kinds of stuck item

Resolve every `ph_insync = 0` record to its real path on disk and the pile splits
into five kinds. Distribution of the 346 records above:

| Kind | Count | What it is | Fix |
|---|---|---|---|
| **ghost** | 305 | metadata points at a path that no longer exists | tombstone the folder |
| **symlink** | 9 | symlinks, and `._*` AppleDouble files | delete; never syncs |
| **`__MACOSX/`** | 25 files | zip-extraction leftovers | delete the directory |
| **build-artifact** | 4 | `ephemeral/`, `.dart_tool/`, `.lock` | delete; regenerated |
| **stubborn** | 2 | file is present and readable, yet the replace never lands | recreate the file |
| **pending** | — | just created or modified, queued for upload (change type 2/5/260) | nothing, wait |
| *system* | 1 | OneDrive's own `temp` record | ignore |

### Ghost records

The path is gone; the database still says it is there.

In this case all 305 pointed at a single `_trash` folder inside an archive
directory. The folder had been emptied months earlier, but the database still
held **7,136 records** for it, every one `tombstoned = 0`. On every rescan the
engine tried to resolve those 7,136 paths, failed, and parked the queue.

Usually caused by deleting or renaming a directory while sync was not working
properly, so the deletion was never fully recorded.

### Symlinks

OneDrive does not sync symlinks, but the links still land in the database and then
never clear. Common sources: Flutter's `macos|linux/flutter/ephemeral/` (rebuilt
by every `flutter pub get`), Python's `venv/` and `.conda/` (`bin/python*`),
`node_modules/.bin/`.

### Extraction leftovers

`__MACOSX/`, created when extracting a zip that was made on macOS. It contains
**only** `._*` AppleDouble files, which OneDrive will never upload. One skill
directory had 25 of them, 100 KB of pure noise.

### Build artifacts and lock files

`.dart_tool/`, `target/`, `dist/`, assorted `.lock` files. Every rebuild creates a
new record and invalidates the old one, so this kind keeps regenerating.

### Stubborn single files

The file is right there, reads fine, `shasum` returns instantly — and the
`LC_REPLACE_FILE` change for it still never completes. Very few of these (2 in
this case). Typical signature: `materialized = 1`, one `change_type = 4` record,
`ph_insync = 0`.

Worth stressing: the log's `Errno::2 NotFound` makes it look like the file was
deleted. In this case all 24,777 "not found" files were present and fully
readable. Verify with `shasum`, do not trust the log.

---

## Fixing it, cheapest first

### Tier 1 — drop impossible change records

For a folder that no longer exists but still has tens of thousands of queued
entries. Delete only `dbfs_changerecord`; leave `dbfs_mftrecord` alone, so nothing
is ever deleted in the cloud.

```sql
DELETE FROM dbfs_changerecord WHERE itemid IN (
  WITH RECURSIVE p(itemid) AS (
    SELECT <ghost subtree root>
    UNION
    SELECT r.itemid FROM dbfs_mftrecord r JOIN p ON r.parentid = p.itemid
  ) SELECT itemid FROM p
);
```

In this case one renamed-away directory tree had 2,419 files queued ~32 times
each — **76,797 records, 75.7% of the queue**. Dropping them took
`change_type=4` from 76,770 to 83.

**This does not fix the UI counter.** Stuck items usually carry no change record
at all. See tier 3.

### Tier 2 — delete what should never have been in OneDrive

No database work needed:

| Target | Why |
|---|---|
| `__MACOSX/` | extraction leftovers, only `._*` files |
| `macos|linux/flutter/ephemeral` | plugin symlink farm, rebuilt on demand |
| `.dart_tool/` | build cache, contains lock files |
| `venv/`, `.conda/` | `bin/python*` are symlinks |
| `node_modules/` | large, and usually contains symlinks |

### Tier 3 — tombstone the ghost folder

This is the real fix for the UI counter. OneDrive's schema enforces the order:

```sql
CREATE TRIGGER enforce_constraint_update_tombstone
BEFORE UPDATE OF tombstoned ON dbfs_mftrecord FOR EACH ROW
WHEN (NEW.tombstoned = 1) BEGIN
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM dbfs_mftrecord WHERE parentid = NEW.itemid AND tombstoned = 0
  ) THEN RAISE(ABORT, 'record has at least one non-tombstoned child') END;
END;
```

so children must be processed before their parent — deepest first. Result here:

```
subtree records : 7136
tombstoned      : 7136
remaining alive : 0
integrity       : ok
```

`ph_insync = 0` went **346 → 41** immediately.

Then clear the leftovers: after deleting 25 `__MACOSX` files, 4 lock files and a
few already-missing files, the remaining 40 records had no path on disk either,
and tombstoning them took it to **41 → 1**.

### Tier 4 — reset the domain (not recommended)

The official "unlink this Mac and sign in again", and the shipped script:

```
/Applications/OneDrive.app/Contents/Resources/ResetOneDriveAppStandalone.command
```

It removes the entire group container — **every account's `dbfs.db` plus tens of
GB of content cache** — deletes `~/Library/Application Support/OneDrive` and the
logs, runs `OneDrive /removedomains` to drop the File Provider domains, and
clears the keychain credentials.

Fine for a clean account. But if the queue contains **local copies that were
never uploaded** (24,900 files / 1.5 GB on this machine), that is data loss.

And it does not last: it tends to come back in about three months, because none
of the root causes are addressed.

---

## Four traps

### 1. The system `temp` record can never be tombstoned

`itemid 9223372036854775708` has `parentid` pointing at itself:

```
id=9223372036854775708 type=2 name=temp
    child: 9223372036854775708 temp tombstoned=0    ← itself
```

The trigger always sees a live child and aborts. The record is permanently
`ph_insync = 0`. **When your count settles at 1, that 1 is this record — you are
done.** Do not chase it.

### 2. The queue grows right after a tombstone

After tombstoning 7,136 records, `dbfs_changerecord` went from 25,057 to 30,467 —
7,156 new `change_type=260` records, all pointing at the records just tombstoned,
with extension CPU back up to 95%.

That is reconciliation with the cloud, not a regression. Judge by `ph_insync = 0`
and the UI counter, never by the queue length.

### 3. Downgrading the client is a one-way door

`OneDrive.pkg`'s `Scripts/preinstall` refuses downgrades:

```bash
if [ ${should_continue_preinstall} != 0 ]; then
    od_logging::log_info "A higher version of OneDrive is already installed on this machine. macOS Installer will skip file copy."
    exit 0
fi
```

`exit 0` means **success reported, zero files copied**. To downgrade at all you
must first `mv` `/Applications/OneDrive.app` out of the way.

Worse: once installed, the older client cannot take over. The File Provider
domain was upgraded by the newer client and carries a version stamp, so the older
one starts with a modal dialog that has exactly one button:

> **需要 OneDrive 更新**
> 你的 OneDrive 文件由较新版本的 OneDrive 管理。请更新到最新版本以访问你的文件。未丢失任何数据。

> ("OneDrive needs to be updated. Your OneDrive files are managed by a newer
> version of OneDrive. Please update to the latest version to access your files.
> No data has been lost.")

It cannot be dismissed — only "Quit OneDrive". The mount also enumerates
incompletely while in that state (one directory had 100 entries in the database
and showed 79 on disk).

On top of that, the newer client's own updater will pull the newer build back.
**Do not use downgrading to solve this class of problem.**

### 4. `Errno::2 NotFound` does not mean the file is gone

The metadata→path mapping layer reports `NotFound` when it cannot resolve an
itemid, which is not the same as the filesystem saying ENOENT. All 24,777 files
involved here were present and fully readable (`shasum` returned in milliseconds).

Verify with `shasum` or `head -c 1`. Do not conclude data loss from the log.

---

## Why it comes back

Nothing in the system prevents you from putting unsyncable things into OneDrive:

- Symlinks — OneDrive does not handle them, but does record them
- Build artifacts — every rebuild leaves an orphaned record behind
- Deleting or renaming directories — the cloud converges, local metadata does not,
  and the mismatch surfaces months later during a rescan
- Heavy multi-device sync — conflict copies (`name-<Machine Name> (2).md`) are
  additional metadata noise

So treat it as **periodic maintenance**, not a one-time repair.

---

## Prevention and a 1-minute health check

1. Never put build/environment directories in OneDrive: `node_modules`, `venv`,
   `.conda`, `.dart_tool`, `ephemeral`, `target`, `dist`.
2. Delete or rename directories **while sync is healthy**, and give OneDrive time
   to finish before shutting the machine down. The dangerous case is "deleted or
   renamed while sync was already broken".
3. Health check:

```bash
DB=~/Library/Group\ Containers/UBF8T346G9.OneDriveStandaloneSuite/.Dbfs.Dbfs_Personal.noindex/dbfs.db
sqlite3 "$DB" "SELECT COUNT(*) FROM dbfs_mftrecord WHERE tombstoned=0 AND ph_insync=0;"
```

A single digit is healthy. Tens or more means it is time to look.

4. Before touching the database, back it up, with OneDrive fully stopped:

```bash
lsof "$DB"                                    # expect no output
cp -p "$DB" "$DB-wal" "$DB-shm" ~/od-db-backup-$(date +%Y%m%d)/
```

5. After renaming or moving a large directory, wait a few minutes and confirm
   `ph_insync = 0` comes back down. If it does not, you have left ghosts.

---

## Acceptance criteria

Do not settle for "the spinner stopped". All of these should hold:

- `ph_insync = 0` is back to a single digit (that 1 is the system record)
- `PRAGMA integrity_check` returns `ok`
- A newly created test file uploads within a minute
- The File Provider extension PIDs stop churning every 30–60 seconds
- CPU drops from 80%+ to single digits

After the repair here: `ph_insync` 346 → 1, `integrity_check = ok`, main process
CPU 129% → 10%, extension down from 95%.

---

## The tools in this repo

Python 3.9+, standard library only, nothing to install.

```bash
git clone https://github.com/firewelly/onedrive-mac-sync-doctor.git
cd onedrive-mac-sync-doctor

python3 od_state.py                    # version, processes, per-account counters
python3 od_audit.py                    # what is stuck, sorted into the five kinds
python3 od_audit.py --details          # ... plus the item paths
```

Accounts and mount points are discovered from OneDrive's own settings, so there
is nothing to configure and nothing hardcoded.

**Repair**

```bash
# ghost folder -> tombstone the whole subtree (metadata only, no cloud deletion)
python3 od_tombstone.py Personal --root 1293203            # preview
python3 od_tombstone.py Personal --root 1293203 --apply

# retry flood -> drop change records that can never succeed
python3 od_fix_records.py Personal --root 1255264            # preview
python3 od_fix_records.py Personal --root 1255264 --apply

# before anything destructive -> list files that have no cloud copy
python3 od_backup_list.py Personal --out /Volumes/Backup/od-personal
```

| Script | Purpose |
|---|---|
| `od.py` | Shared library: account discovery, mount resolution, path reconstruction |
| `od_state.py` | Snapshot of version, processes, counters and integrity |
| `od_audit.py` | Classify every stuck record into the five kinds (plus `pending` for in-flight) |
| `summary.py` | One-screen health verdict per account |
| `od_tombstone.py` | Bottom-up tombstone of a ghost subtree |
| `od_fix_records.py` | Delete change records that can never complete |
| `od_backup_list.py` | Export local-only files as a list for `tar` |

Write tools default to a preview, call `assert_no_processes()` first, run inside
a transaction, and roll back when the affected row count differs from the
prediction.

---

## Requirements and warnings

- macOS with the **standalone** OneDrive build (the App Store build has a
  different layout).
- Python 3.9+.
- These tools touch OneDrive's private, undocumented database. Schema versions
  change without notice. Read the code before running it, and keep the backups.
- No warranty. This operates on data you cannot easily regenerate.

## License

MIT.
