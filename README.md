# Zimbra Share Manager

> **No Copyright.** This software is released to the public domain and may be freely used, modified, distributed, or incorporated into other works without restriction or attribution.
>
> **Disclaimer:** This software is provided "as is", without warranty of any kind, express or implied. No representation is made that it will function correctly, be fit for any particular purpose, or be free of defects. Use at your own risk.

A Flask-based web application for auditing and managing Zimbra folder shares.
Runs on port **8585** as the `zimbra` OS user.

---

## Prerequisites

- Python 3.8+
- Flask 2.x+
- Zimbra **8.8.15** or **10.1** (both fully supported)
- Must be run as the `zimbra` OS user
- Zimbra binaries present at `/opt/zimbra/bin/`

---

## Installation

```bash
# Create directories
mkdir -p /opt/zimbra/share_manager/templates
mkdir -p /opt/zimbra/share_manager/data
mkdir -p /opt/zimbra/share_manager/log

# Copy files
cp share_manager.py /opt/zimbra/share_manager/
cp templates/index.html /opt/zimbra/share_manager/templates/

# Set ownership
chown -R zimbra:zimbra /opt/zimbra/share_manager
chmod 750 /opt/zimbra/share_manager/share_manager.py

# Install Flask as zimbra user
sudo -u zimbra pip3 install flask --user
# OR system-wide:
pip3 install flask
```

---

## Running

```bash
sudo -u zimbra python3 /opt/zimbra/share_manager/share_manager.py
```

Access the web UI at: **http://server:8585**

The application must run as the `zimbra` OS user so that it has permission to
invoke Zimbra CLI tools such as `zmprov`, `zmmailbox`, `zmcontrol`, and
`zmhostname`.

---

## Single-Server Workflow

1. Open the web UI at `http://server:8585`.
2. On the **Administration** tab, click **Collect Share Info**.
   - The application enumerates all accounts via `zmprov -l gaa` and queries
     each account's shares and folder mounts.
   - A progress bar tracks the job. Collection may take several minutes on
     large deployments.
3. Switch to the **Audit Shares** tab when collection is complete.
4. Review the share records. Each row shows the source account, shared folder,
   permissions, grantee account, and mount path.
   - Rows highlighted in **yellow** indicate an orphaned source or grantee
     account (the account no longer exists in Zimbra).
   - Click **Remove** on any row you want to clean up. The row turns red.
   - Click **Keep** to reverse the decision.
5. Check the **Orphaned Shares** tab for records that have a grant but no
   matching mount, or a mount but no matching grant. Use **Remove All Orphaned**
   to flag all of them for removal in one action.
6. Return to the **Administration** tab and click **Apply Current Manifest State**.
7. Confirm in the modal dialog.
   - **Phase 1** creates or updates grants and mountpoints for all "Keep" records.
     Already-existing shares are silently updated — safe to run multiple times.
   - **Phase 2** revokes grants and deletes mountpoints for all "Remove" records.
     This phase is destructive and irreversible.
   - Progress and results are shown in the progress bar and status message.

---

## Migration Workflow

### On the source server

1. Run a full collection (see Single-Server Workflow steps 1–5).
2. Review and flag records as desired in the Audit and Orphaned Shares tabs.
3. On the **Administration** tab under **Migration Operations**, click
   **Export shares.json**.
4. Save the downloaded `shares.json` and transfer it to the destination server.

### On the destination server

1. Open the web UI at `http://destination-server:8585`.
2. Under **Migration Operations**, use the file input to select the `shares.json`
   exported from the source server and click **Load**.
   - The manifest is loaded into the tool as the current working version.
   - No Zimbra changes happen at this step.
3. Switch to the **Audit Shares** and **Orphaned Shares** tabs to review
   imported records. Flag any records you do not want to recreate as "Remove".
4. Return to the **Administration** tab and click **Apply Current Manifest State**.
5. Confirm in the modal dialog.
   - **Phase 1** iterates over all "Keep" records and, for each record where both
     the source and grantee accounts exist on this server, creates the folder
     grant and mountpoint. Accounts not on this server are skipped automatically.
   - Records with `mount_is_inherited=true` (subfolder grants covered by a parent
     mountpoint) receive the grant but no separate mountpoint — the parent mount
     already exposes those subfolders as virtual children.
   - **Phase 2** revokes grants and deletes mountpoints for all "Remove" records.
6. To re-apply the manifest after a **Delete All Shares** operation, simply click
   **Apply Current Manifest State** again — it is fully idempotent. Already-existing
   grants and mounts are silently updated; `mail.ALREADY_EXISTS` responses from
   Zimbra are treated as success.

---

## UI Tabs

### Administration

**Server Information** — hostname, OS version, Zimbra version, and the current
manifest status (version number, record counts, keep/remove/orphaned breakdown).

**Single Server Operations**:
- *Collect Share Info* — scans all accounts and builds a fresh manifest.
  Warns if a manifest already exists and offers overwrite.
- *Apply Current Manifest State* — two-phase operation: Phase 1 creates/updates
  grants and mounts for all "Keep" records; Phase 2 removes grants and mounts
  for all "Remove" records. Idempotent for Phase 1; destructive for Phase 2.
- *Delete All Shares* — removes **every** share grant and mountpoint on this
  server. Requires typing `Delete All Shares` exactly to unlock the confirm
  button. Does not modify the manifest. After running, use
  *Apply Current Manifest State* to recreate shares from the manifest.

**Migration Operations**:
- *Export shares.json* — downloads the current manifest for transfer to the
  destination server.
- *Load Imported Manifest* — uploads a `shares.json` from a source server and
  makes it the current working manifest. Review records in the Audit and
  Orphaned Shares tabs before applying.

**Recent Operations** — last 15 entries from the audit log.

**Last Apply Results** — summary from the most recent apply job with
expandable skipped / partial-failure / error detail sections.

**Manifest Version History** — lists `shares.json` and all `shares_vN.json`
archives with version numbers, timestamps, record counts, and download links.

### Audit Shares

Displays all **non-orphaned** records from the manifest — those where both
`has_grant` and `has_mount` are consistent (both true, or both false).

**Layout**:
- Source accounts are paginated (10 per page) with Prev/Next/numbered controls.
- Within each source account block, records are grouped by grantee account.
  Each grantee sub-group has a collapsible header with **Remove All** /
  **Keep All** bulk buttons.
- When a `source_folder = "/"` exists for a source/grantee pair, it is rendered
  as an expandable parent row. All non-root folders for that pair are hidden
  child rows (indented), revealed by a ▶ toggle. Flagging the root row for
  removal cascades to all child rows.
- **Inherited subfolder grants** (`mount_is_inherited=true`) are hidden from the
  table entirely. The parent row shows a cyan `+N subfolder grants` badge.
  Keep/Remove on the parent automatically cascades to its inherited children.

**Filter** — text input filters across source account, folder, grantee, and
mount path. Pagination is suspended while a filter is active.

### Orphaned Shares

Displays records where:
- `has_grant=true` AND `has_mount=false` — a grant exists but the grantee has
  no corresponding mountpoint, **or**
- `has_grant=false` AND `has_mount=true` — a mountpoint exists but there is no
  corresponding grant on the owner's side.

These records are **excluded from the Audit Shares tab**.

**Remove All Orphaned** button — opens a confirmation dialog asking
*"Do you want to remove all orphaned grant/mounts?"* On confirm, every orphaned
record in the manifest is flagged for removal (`keep=0`) in a single operation.
Use *Apply Current Manifest State* afterwards to execute the removals in Zimbra.

Per-row Keep/Remove buttons and grantee sub-group collapse/expand work
identically to the Audit Shares tab.

---

## Notes

- **Zimbra 10.1 compatible.** All CLI command formats (`gsi`, `getAllFolders`,
  `modifyFolderGrant`, `createMountpoint`, `deleteFolder`) are identical between
  Zimbra 8.8.15 and 10.1. Mount detection uses the `(owner@domain:folder_id)`
  annotation appended to the path column in `getAllFolders` output.
- **Apply Current Manifest State is idempotent.** `mail.ALREADY_EXISTS` from
  `createMountpoint` and `mail.GRANT_EXISTS` from `modifyFolderGrant` are both
  treated as success. Re-running after *Delete All Shares* is safe and expected.
- **Inherited subfolder mounts.** When a root `/` share is mounted, all subfolder
  grants for the same source/grantee pair are flagged `mount_is_inherited=true`.
  Phase 1 sets the grant but skips `createMountpoint` for these records (the
  parent mount already exposes the subfolders). Phase 2 skips `deleteFolder`
  for inherited records — the virtual subfolders disappear automatically when the
  parent mount is removed.
- **Parsers are best-effort.** Individual account failures during collection are
  logged as warnings and do not abort the job.
- **Log output** is written to `log/share_manager.log` and `log/audit.log`
  inside the application directory.
- **Port 8585** is hard-coded. No authentication is implemented; the application
  relies on OS-level firewall rules to restrict access.
- **Running as zimbra user** is required. Zimbra CLI tools fail or return
  incomplete results when run as any other user.
- **Orphaned accounts** (`source_orphaned` / `grantee_orphaned`) are accounts
  that appear in share data but are no longer returned by `zmprov -l gaa`.
  They are highlighted in the Audit tab but are never acted upon destructively
  unless you explicitly flag them for removal.
- **Private Network Access (PNA).** Brave and Chrome enforce PNA for POST
  requests to private-network servers. The application handles `OPTIONS`
  preflight requests and stamps `Access-Control-Allow-Private-Network: true`
  on all responses so these browsers work without additional configuration.

---

## Directory Layout

```
/opt/zimbra/share_manager/
├── share_manager.py          # Flask application
├── templates/
│   └── index.html            # Single-page Bootstrap 5 UI
├── data/
│   ├── shares.json           # Current manifest (working dataset)
│   ├── shares_vN.json        # Archived prior manifest versions
│   ├── accounts.json         # Account list from last collection
│   └── apply_results.json    # Results from the last apply operation
└── log/
    ├── share_manager.log     # Application debug log
    └── audit.log             # Append-only operations audit log
```

---

## `shares.json` Schema

The manifest file uses underscore-prefixed metadata keys at the top of the
JSON object (so they appear first and act as a human-readable header), followed
by the payload keys.

Top-level structure:

```json
{
  "_version": 4,
  "_last_action": "apply_manifest_state",
  "_last_updated": "2026-05-01T19:11:24+00:00",
  "_version_history": [ ... ],
  "collected_from": "mail.example.com",
  "collected_at": "2026-04-29T12:00:00+00:00",
  "record_count": 42,
  "records": [ ... ]
}
```

| Top-level field      | Description |
|----------------------|-------------|
| `_version`           | Monotonically incrementing integer; bumped on every apply or collect |
| `_last_action`       | Last operation: `collect`, `apply_manifest_state`, or `delete_all_shares` |
| `_last_updated`      | ISO-8601 UTC timestamp of the last write |
| `_version_history`   | Array of version entries (version number, timestamp, action, counters) |
| `collected_from`     | Hostname of the server where collection was run |
| `collected_at`       | ISO-8601 UTC timestamp of the collection run |
| `record_count`       | Total number of records in the `records` array |

Each record:

```json
{
  "id": "a1b2c3d4e5f6a7b8",
  "source_account": "alice@example.com",
  "source_folder": "/Inbox",
  "permissions": "rwidx",
  "grantee_account": "bob@example.com",
  "grantee_mountpoint": "/Alice Inbox",
  "has_grant": true,
  "has_mount": true,
  "mount_is_inherited": false,
  "source_orphaned": false,
  "grantee_orphaned": false,
  "keep": 1
}
```

| Field                | Description |
|----------------------|-------------|
| `id`                 | First 16 hex chars of SHA-256 of `source::folder::grantee` — stable identifier used for Keep/Remove flagging |
| `source_account`     | Email of the account that owns the shared folder |
| `source_folder`      | Path of the shared folder in the owner's mailbox |
| `permissions`        | Zimbra rights string (e.g. `r`, `rwidx`, `rwidxap`) |
| `grantee_account`    | Email of the account that has been granted access |
| `grantee_mountpoint` | Path of the mountpoint in the grantee's mailbox, or `null` if not mounted |
| `has_grant`          | `true` if a live ACL grant was found via `getShareInfo` |
| `has_mount`          | `true` if the grantee has a mountpoint for this share |
| `mount_is_inherited` | `true` if a parent folder of `source_folder` is already mounted by the same grantee — the subfolder grant is covered by the parent mount and does not need its own mountpoint |
| `source_orphaned`    | `true` if `source_account` is not in the current account list |
| `grantee_orphaned`   | `true` if `grantee_account` is not in the current account list |
| `keep`               | `1` = keep / apply; `0` = flag for removal |

### Record Classification

| `has_grant` | `has_mount` | Tab shown     | Notes |
|-------------|-------------|---------------|-------|
| `true`      | `true`      | Audit Shares  | Normal healthy share |
| `true`      | `false`     | Orphaned Shares | Grant exists but grantee never mounted it |
| `false`     | `true`      | Orphaned Shares | Mount exists but grant was removed |
| `false`     | `false`     | Audit Shares  | Historical record; no live state |

---

## `accounts.json` Schema

Written alongside `shares.json` during every collection run:

```json
{
  "collected_at": "2026-04-29T12:00:00+00:00",
  "count": 500,
  "accounts": [
    "alice@example.com",
    "bob@example.com"
  ]
}
```

This file is informational and is regenerated on every Collect Share Info run.

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serve the UI |
| GET | `/api/info` | Server info, Zimbra version, manifest summary |
| GET | `/api/manifest` | Full manifest JSON |
| POST | `/api/manifest/delete` | Delete `shares.json` |
| GET | `/api/manifest/versions` | List all manifest versions (current + archives) |
| GET | `/api/manifest/download/<file>` | Download an archived `shares_vN.json` |
| GET | `/api/export` | Download current `shares.json` as a file attachment |
| POST | `/api/import` | Upload and replace the manifest |
| POST | `/api/collect` | Start a collection job (`force` param bypasses 409 guard) |
| POST | `/api/shares/flag` | Set `keep` for one record by ID (cascades to inherited children) |
| POST | `/api/shares/flag-all-orphaned` | Flag all orphaned records for removal |
| GET | `/api/job/<id>` | Poll a background job (404 if job not in memory) |
| POST | `/api/apply/cleanup` | Start Apply Current Manifest State job |
| POST | `/api/apply/delete-all-shares` | Start Delete All Shares job |
| GET | `/api/apply/results` | Last apply operation results |
| GET | `/api/audit/log` | Recent audit log entries (newest first, max 100) |
