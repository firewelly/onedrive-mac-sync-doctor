# onedrive-mac-sync-doctor

Diagnose and repair the "OneDrive is stuck syncing N files" problem on macOS.

If OneDrive for Mac sits at *Syncing N files* forever, burns 70–90% CPU in
`OneDrive File Provider`, and restarting the app or the machine changes nothing,
the cause is almost never the network, the account, or the client version. It is
usually a batch of records in the local metadata database that point at paths
which no longer exist on disk. The sync engine resolves them on every rescan,
fails, and parks the queue.

These scripts find those records, tell you which of five kinds they are, and
repair the safe ones without deleting anything in the cloud.

Read-only by default. Every write tool requires a dry run first, refuses to run
while OneDrive is alive, and rolls back if the number of affected rows does not
match what it predicted.

> Background, the reasoning and the four traps:
> [`docs/onedrive-mac-sync-stuck.zh-CN.md`](docs/onedrive-mac-sync-stuck.zh-CN.md) (Chinese)

---

## The one query that matters

The number in the UI is (approximately) this count:

```sql
SELECT COUNT(*) FROM dbfs_mftrecord WHERE tombstoned = 0 AND ph_insync = 0;
```

in

```
~/Library/Group Containers/UBF8T346G9.OneDriveStandaloneSuite/.Dbfs.Dbfs_<Account>.noindex/dbfs.db
```

Measured on a real machine: 346 records ↔ UI showed 307; another account had
9 ↔ UI showed 1. Close enough to locate and verify a fix.

Note that the number of rows in `dbfs_changerecord` is **not** a progress
indicator. The stuck account had 25,020 change records but only 307 items
actually pending.

---

## Usage

Python 3.9+, standard library only. No installation.

```bash
git clone https://github.com/firewelly/onedrive-mac-sync-doctor.git
cd onedrive-mac-sync-doctor

python3 od_state.py                                  # snapshot: version, processes, counters
python3 od_audit.py                                  # what is stuck, and why
python3 od_audit.py --details                        # ... plus item paths
```

Accounts are discovered automatically from OneDrive's own settings; mount points
are read out of them. Nothing is hardcoded.

### Repair

**1. Ghost folder: mark it deleted in the metadata (the real fix)**

```bash
# find the folder's itemid
sqlite3 "$DB" "SELECT itemid, name FROM dbfs_mftrecord
               WHERE name LIKE '%//_trash' AND tombstoned=0;"

python3 od_tombstone.py Personal --root 1293203            # preview
# quit OneDrive, then:
python3 od_tombstone.py Personal --root 1293203 --apply
```

Children are processed deepest-first because a database trigger refuses to
tombstone a record that still has live children. No change record is created, so
nothing is ever deleted in the cloud — this only makes local metadata agree with
the disk.

**2. Retry flood: drop impossible change records**

```bash
python3 od_fix_records.py Personal --root 1255264            # preview
python3 od_fix_records.py Personal --root 1255264 --apply
```

Use when a deleted folder still has tens of thousands of queued entries. This
removes the duplicate queue entries but does **not** fix the UI counter — stuck
items usually carry no change record at all.

**3. Back up local-only files before anything destructive**

```bash
python3 od_backup_list.py Personal --out /Volumes/Backup/od-personal
cd "/path/to/mount" && tar -c --null -T /Volumes/Backup/od-personal.nul -f ../od.tar
```

Files that were never uploaded have no cloud copy. A domain reset or an account
re-link takes them with it.

---

## The five kinds of stuck item

| Kind | What it is | Fix |
|---|---|---|
| **ghost** | metadata points at a path that no longer exists | `od_tombstone.py` on the folder |
| **symlink** | symlinks, plus `._*` AppleDouble files | delete; OneDrive never syncs them |
| **`__MACOSX/`** | leftover from extracting a macOS-made zip | delete the directory |
| **build-artifact** | `ephemeral/`, `.dart_tool/`, `venv/`, `node_modules/`, `.lock` | delete; regenerated on build |
| **stubborn** | file is present and readable, yet the replace never lands | recreate: copy, remove, copy back |
| *system* | OneDrive's `temp` record, `itemid 9223372036854775708` | ignore, it is always there |

A real audit, 346 stuck records:

```
=== Personal ===
not in sync (ph_insync=0): 346
  ghost               305      <- one deleted _trash folder, 7,136 records
  symlink               9
  build-artifact        4
  ghost (loose files)   3
  system                1

  ghost items cluster under these folders (fix the folder, not the files):
      305  Documents/Archive/_trash
```

After tombstoning that one folder: `ph_insync=0` went 346 → 1, and the `1` is
the system record that can never be cleared.

---

## Traps worth knowing

**The `temp` record cannot be tombstoned.** `itemid 9223372036854775708` has
`parentid` pointing at itself, so the trigger always sees a live child. It is
permanently `ph_insync=0`. When your count settles at 1, you are done.

**The queue grows after a tombstone.** Reconciling with the cloud creates a
change record per tombstoned item (7,156 in the case above) and CPU spikes for a
while. Judge by `ph_insync=0`, not by `dbfs_changerecord`.

**Downgrading the client is a one-way door.** The package's `preinstall` script
bails out with `exit 0` when a newer version is installed (reports success,
copies nothing), and once the File Provider domain has been upgraded by a newer
client, the older one refuses to take it over — it shows a modal that can only
quit OneDrive, and the mount enumerates incompletely. Don't go there.

**`Errno::2 NotFound` does not mean the file is gone.** In the case above all
24,777 "not found" files were present and fully readable. That error comes from
the metadata→path mapping layer, not from the filesystem. Verify with `shasum`.

**`ResetOneDriveAppStandalone.command` deletes a lot.** It removes the whole
group container — every account's `dbfs.db` plus tens of GB of content cache —
drops the File Provider domains, and clears keychain credentials. It also tends
to come back in a few months, because none of the underlying causes are
addressed.

---

## Requirements and warnings

- macOS with the standalone OneDrive build (not the App Store build).
- Python 3.9+.
- **Back up `dbfs.db`, `dbfs.db-wal` and `dbfs.db-shm` before any write**, and
  confirm OneDrive is stopped (`lsof <dbfs.db>` prints nothing).
- These tools touch OneDrive's private, undocumented database. Schema versions
  change. Read the code before running it, and keep the backups.

## License

MIT. No warranty — this operates on data you cannot easily regenerate.
