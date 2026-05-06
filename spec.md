# Zimbra Share Manager — Specification

## Overview

A single-file Flask web application for auditing and managing Zimbra folder share relationships. Runs on **port 8585** as the `zimbra` OS user. Designed for Zimbra 8.8.15 and 10.1 (both fully supported). The UI is a single Bootstrap 5 page served from `templates/index.html`.

---

## Runtime Environment

| Item | Value |
|---|---|
| Language | Python 3.8+ |
| Framework | Flask 1.x or 2.x (compatibility wrapper handles both) |
| Port | 8585 |
| Run as | zimbra OS user |
| Working directory | `/opt/zimbra/share_manager/` |
| Data files | `data/` subdirectory |
| Log files | `log/` subdirectory |

---

## Directory Layout

```
/opt/zimbra/share_manager/
├── share_manager.py
├── templates/
│   └── index.html
├── data/
│   ├── shares.json           # Current manifest (working dataset)
│   ├── shares_vN.json        # Archived prior versions
│   ├── accounts.json         # Account list from last collection
│   └── apply_results.json    # Results from last apply operation
└── log/
    ├── share_manager.log     # Application debug log
    └── audit.log             # Append-only operations audit log
```

---

## Manifest Schema (`shares.json`)

The file uses underscore-prefixed metadata keys at the top of the JSON object (so they appear first as a human-readable header), followed by payload keys.

Top-level structure:

```json
{
  "_version": 4,
  "_last_action": "apply_manifest_state",
  "_last_updated": "2026-05-01T19:11:24+00:00",
  "_version_history": [ ... ],
  "collected_from": "zimbra.hostname.com",
  "collected_at": "2026-04-29T12:00:00+00:00",
  "record_count": 42,
  "records": [ ... ]
}
```

| Field | Description |
|---|---|
| `_version` | Monotonically incrementing integer; bumped on every apply or collect |
| `_last_action` | Last operation: `collect`, `apply_manifest_state`, or `delete_all_shares` |
| `_last_updated` | ISO-8601 UTC timestamp of the last write |
| `_version_history` | Array of version entries; each entry records version, timestamp, action, and operation counters |
| `collected_from` | Zimbra hostname where collection was run |
| `collected_at` | ISO-8601 UTC timestamp of the collection run |
| `record_count` | Total number of records in the `records` array |

Each record:

```json
{
  "id": "<16-char hex SHA256>",
  "source_account": "owner@domain.com",
  "source_folder": "/Inbox",
  "permissions": "rwidx",
  "grantee_account": "grantee@domain.com",
  "grantee_mountpoint": "/Shared Inbox",
  "has_grant": true,
  "has_mount": true,
  "mount_is_inherited": false,
  "source_orphaned": false,
  "grantee_orphaned": false,
  "keep": 1
}
```

| Field | Description |
|---|---|
| `id` | SHA256[:16] of `"source::folder::grantee"` — stable identifier for flagging |
| `source_account` | Account that owns the shared folder |
| `source_folder` | Path of the shared folder in the owner's mailbox |
| `permissions` | Zimbra rights string (e.g. `r`, `rwidx`, `rwidxap`) |
| `grantee_account` | Account that received the share |
| `grantee_mountpoint` | Path of the mount in the grantee's mailbox; `null` if not mounted |
| `has_grant` | `true` if a Zimbra ACL grant exists from `getShareInfo` data |
| `has_mount` | `true` if the grantee has a mountpoint for this share |
| `mount_is_inherited` | `true` if a parent folder of `source_folder` is already mounted by the same grantee — this subfolder grant is covered by the parent mount and does not need its own mountpoint |
| `source_orphaned` | `true` if `source_account` no longer exists in the active account list |
| `grantee_orphaned` | `true` if `grantee_account` no longer exists in the active account list |
| `keep` | `1` = keep/apply; `0` = flag for removal |

### Record Classification

| `has_grant` | `has_mount` | Tab | Notes |
|---|---|---|---|
| `true` | `true` | Audit Shares | Normal healthy share |
| `true` | `false` | Orphaned Shares | Grant exists but never mounted (or mount was deleted) |
| `false` | `true` | Orphaned Shares | Mount exists but grant was removed |
| `false` | `false` | Audit Shares | Historical/imported record with no live state |

---

## Collection Process

Triggered by **Collect Share Info**. Runs in a background thread. Progress reported to the UI via job polling (`/api/job/<id>`).

### Phase 1 — Get all accounts (0–5%)
`zmprov -l gaa` → sorted lowercase list of all active email addresses → written to `accounts.json`.

### Phase 2 — getShareInfo for every account (5–70%)
For each account: `zmprov gsi <account>`

Parses the fixed-width columnar table output. Column boundaries are detected dynamically from the dashes separator line.

Key parsing details:
- The `granteename` column is 15 chars wide — truncated emails are expanded by prefix-matching against the full account list. The `@` check occurs **after** expansion.
- The `path` column is 20 chars wide — truncated paths are resolved in Phase 4 via folder ID lookup.
- The `mid` column (mountpoint ID) is parsed but may be 0 in some Zimbra versions; Phase 4 falls back to cross-reference with Phase 3 mount data.
- Non-user grant types (`gt` not `usr`) are skipped.
- Result: `shares_by_key` dict keyed by `(owner_email, folder_path, grantee_account)`.

### Phase 3 — getAllFolders for all accounts (70–90%)
For each account (grantees + all active accounts): `zmmailbox -z -m <account> getAllFolders`

**Two-pass approach** — required because mount annotations contain the *owner's* folder ID, which can only be resolved using the owner's id_map. The owner's id_map may not be available until after all accounts have been scanned.

**Pass 1 (70–82%):** Build `id_maps[account]` for every account.

- Each id_map is `{folder_id: folder_path}`.
- The path is extracted using `rfind(' /')` *after* stripping any trailing mount annotation, correctly handling multi-word folder names.
- Mount annotation format: `(owner@domain:remote_folder_id)` appended to the path column, e.g.:
  ```
  6448  unkn  0  0  /Netadmin (netadmin@azurestandard.com:1)
  6636  mess  47  48  /Netadmin/Inbox (netadmin@azurestandard.com:2)
  ```
- Raw `getAllFolders` output is saved in `folder_outputs[account]` for Pass 2.

**Pass 2 (82–90%):** Parse mountpoints using the now-complete `all_id_maps`.

- Detect mount lines by the `(owner@domain:remote_folder_id)` annotation at the end of the path.
- Resolve the owner's `remote_folder_id` to a path using `all_id_maps[owner_account]`.
- Each detected mount: `{folder_id, mount_path, owner_account, remote_folder, grantee_account}`.

### Phase 4 — Build manifest records (90–95%)
For each grant in `shares_by_key`:

1. **Resolve full folder path**: look up `folder_id` in `id_maps[owner]` to override the possibly-truncated path from Phase 2.
2. **Detect mount** (in priority order):
   - **Primary**: if `mid` from `gsi` is non-zero, look up `id_maps[grantee][mid]` for the mount path.
   - **Fallback A**: cross-reference `(owner, resolved_folder, grantee)` against `mount_lookup` (built from Pass 2 mounts).
   - **Fallback B** (`_find_parent_mount`): walk ancestors of `norm_folder` from the immediate parent up to root. If a parent path exists in `mount_lookup`, the subfolder grant is **inherited** — the grantee_mountpoint is computed as `parent_mount_path + suffix`, where `suffix = norm_folder` when `parent_folder == '/'` (preserving the leading slash), or `norm_folder[len(parent_folder):]` for deeper parents. `mount_is_inherited` is set `true` only when the parent grant also exists in `shares_by_key` (not just the parent mount).
3. Emit record with `has_grant=true`.

After processing all grants, emit orphaned mount records for any `mount_lookup` entry not matched to a grant (`has_grant=false, has_mount=true`).

### Phase 5 — Write manifest (95–100%)
Archive existing `shares.json` → `shares_vN.json`, increment `_version`, write new manifest atomically via `.tmp` + rename.

---

## Apply Current Manifest State Operation

Triggered by **Apply Current Manifest State**. Runs in a background thread via `POST /api/apply/cleanup`. Replaces the former separate Apply Cleanup and Apply Migration operations.

### Phase 1 — Create/update shares for keep=1 records (8–50%)

For each `keep=1` record:
1. Skip if either `source_account` or `grantee_account` is not in `active_accounts` (logged as skipped).
2. `zmmailbox -z -m <source> modifyFolderGrant <folder> account <grantee> <permissions>`
   - `mail.GRANT_EXISTS` response is treated as success (grant already set).
3. If `grantee_mountpoint` is set and `mount_is_inherited=false`:
   `zmmailbox -z -m <grantee> createMountpoint <mount_path> <source> <folder>`
   - `mail.ALREADY_EXISTS` / `already exists` response is treated as success (mount already present). No rollback.
   - Any other mount failure: attempt grant rollback via `modifyFolderGrant ... none`. Log whether rollback succeeded or failed (double-failure requires manual cleanup).
4. If `mount_is_inherited=true`: grant is set, `createMountpoint` is **skipped** — the virtual subfolder already exists under the parent mount.

### Phase 2 — Remove shares for keep=0 records (52–95%)

For each `keep=0` record:
1. If `has_grant=true` and `source_orphaned=false`:
   `zmmailbox -z -m <source> modifyFolderGrant <folder> account <grantee> none`
2. If `has_mount=true` and `mount_path` is set and `grantee_orphaned=false` and `mount_is_inherited=false`:
   `zmmailbox -z -m <grantee> deleteFolder <mount_path>`
   - Inherited records skip `deleteFolder` entirely — virtual subfolders disappear automatically when their parent mount is removed.

Both phases use `zimbra_cmd_with_retry` (up to 2 retries with 2s backoff).

Results are written to `apply_results.json`. Manifest is versioned and updated with `_last_action: apply_manifest_state`.

---

## Delete All Shares Operation

Triggered by **Delete All Shares** (requires typing the exact phrase to confirm). Runs in a background thread via `POST /api/apply/delete-all-shares`.

1. Phase 1: collect all outgoing grants via `gsi` for every account.
2. Phase 2: collect all mountpoints via `getAllFolders` (two-pass, same as collection).
3. Phase 3: `modifyFolderGrant ... none` for every grant found.
4. Phase 4: `deleteFolder` for every mountpoint found.

**The manifest is NOT modified.** After this operation, run **Apply Current Manifest State** to recreate shares from the manifest. The operation is fully idempotent with Apply Current Manifest State because `mail.GRANT_EXISTS` and `mail.ALREADY_EXISTS` are handled as success.

---

## UI — Three Tabs

### Administration Tab

**Server Information card**: hostname, OS, Zimbra version, manifest status (version, `_last_action`, record counts, keep/remove/orphaned breakdown).

**Single Server Operations card**:
- *Collect Share Info*: triggers collection. Warns if manifest already exists (shows record count, offers overwrite).
- *Apply Current Manifest State*: two-phase operation — Phase 1 creates/updates shares for all `keep=1` records; Phase 2 removes shares for all `keep=0` records. Idempotent for Phase 1. Confirmation modal shows the keep count and remove count before proceeding.
- *Delete All Shares*: wipes every grant and mount from this server. Requires typing `Delete All Shares` exactly to unlock the confirm button. Does not touch the manifest.

**Migration Operations card**:
- *Export shares.json*: downloads current manifest as a file attachment.
- *Load Imported Manifest*: uploads a `shares.json` from a source server and replaces the current manifest. No Zimbra operations happen at load time — review records in the Audit/Orphaned tabs first, then run Apply Current Manifest State.

**Job progress bar**: shown during any running job; polls `/api/job/<id>` at 1.5s intervals. Stops polling on 404 (server restart clears in-memory jobs).

**Recent Operations**: last 15 entries from `audit.log` (newest first).

**Last Apply Results**: summary from `apply_results.json` with expandable skipped / partial-failure / error detail sections.

**Manifest Version History**: lists `shares.json` + all `shares_vN.json` archives with version number, last action badge, timestamp, record count, and download link.

### Audit Shares Tab

Loaded automatically when the tab is selected. Displays all **non-orphaned** records — those where `has_grant` and `has_mount` are consistent (both true or both false). Orphaned records are excluded and shown in the Orphaned Shares tab.

**Layout**:
- Source accounts are paginated (10 per page) with Prev/Next/numbered controls.
- Within each source account block, records are grouped by **grantee account** (collapsible sub-group header with Remove All / Keep All bulk buttons).
- When `source_folder = "/"` exists for a `(source, grantee)` pair, it is rendered as an expandable parent. All other folders for that pair are hidden `child-row` elements (indented via CSS), revealed by a ▶/▼ toggle. Flagging the root row for removal cascades to all child rows (client-side for visible children; server-side via the `api_shares_flag` cascade for `mount_is_inherited` children).
- **Inherited subfolder grants** (`mount_is_inherited=true`) are filtered out of the table entirely. The parent row shows a cyan `+N subfolder grants` badge. Keep/Remove on the parent cascades to all inherited children on the server side.

**Per-record columns**: Action (Keep/Remove button), Source Folder / Perms, Destination Account, Mount Path, Status badges.

**Status badges**: `OK`, `No Grant`, `Not Mounted`, `Src Orphaned`, `Dst Orphaned`.

**Row highlighting**: red tint = flagged for removal; amber tint = orphaned account.

**Filter**: text input filters across source account, source folder, grantee, and mount path. While a filter is active, pagination is suspended and all matching groups are shown.

### Orphaned Shares Tab

Displays records where `(has_grant=true AND has_mount=false) OR (has_grant=false AND has_mount=true)`. These are excluded from the Audit Shares tab.

The tab link shows an orange count badge when orphaned records exist.

**Remove All Orphaned** button: opens a confirmation modal asking *"Do you want to remove all orphaned grant/mounts?"* On confirm, calls `POST /api/shares/flag-all-orphaned`, which sets `keep=0` on every orphaned record in the manifest. Use Apply Current Manifest State to execute the removals in Zimbra.

Layout mirrors the Audit Shares tab: source-account groups with collapsible grantee sub-groups, Remove All / Keep All bulk buttons, root `/` nesting, child-row toggle, and pagination.

**Status badges**: `No Grant`, `Not Mounted`, `Src Orphaned`, `Dst Orphaned`.

---

## Zimbra CLI Commands Used

| Command | Purpose |
|---|---|
| `zmprov -l gaa` | List all accounts |
| `zmprov gsi <account>` | Get outgoing share grants for an account |
| `zmmailbox -z -m <account> getAllFolders` | Get full folder tree with flags, IDs, and mount annotations |
| `zmmailbox -z -m <source> modifyFolderGrant <folder> account <grantee> <rights\|none>` | Create, update, or revoke a grant |
| `zmmailbox -z -m <grantee> createMountpoint <path> <source> <folder>` | Create a mountpoint |
| `zmmailbox -z -m <grantee> deleteFolder <path>` | Delete a mountpoint or folder |
| `zmhostname` | Get the server's Zimbra hostname |
| `zmcontrol -v` | Get Zimbra version string |

All commands run via `subprocess` with a configurable timeout. Mutating commands use `zimbra_cmd_with_retry` (up to 2 retries, 2s backoff) to handle transient mailbox lock errors.

---

## Private Network Access (PNA) Handling

Brave and Chrome enforce PNA for POST requests with `Content-Type: application/json` to private-network servers. The application handles this with two Flask hooks:

- `@app.before_request`: intercepts `OPTIONS` preflight requests and returns 204 with the required CORS and PNA headers before Flask routing can return 405.
- `@app.after_request`: stamps `Access-Control-Allow-Private-Network: true` on every response.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | Serve UI |
| GET | `/api/info` | Server info, Zimbra version, manifest summary |
| GET | `/api/manifest` | Full manifest JSON (includes `exists` flag) |
| POST | `/api/manifest/delete` | Delete `shares.json` |
| GET | `/api/manifest/versions` | List all manifest versions (current + archives) |
| GET | `/api/manifest/download/<filename>` | Download an archived `shares_vN.json` |
| GET | `/api/export` | Download current `shares.json` as a file attachment |
| POST | `/api/import` | Upload and replace manifest; ensures all records have a `keep` field |
| POST | `/api/collect` | Start collection job (`force` param bypasses 409 guard) |
| POST | `/api/shares/flag` | Set `keep` for one record by ID; cascades to `mount_is_inherited` children |
| POST | `/api/shares/flag-all-orphaned` | Set `keep=0` on all orphaned records |
| GET | `/api/job/<id>` | Poll job status (404 if job no longer in memory) |
| POST | `/api/apply/cleanup` | Start Apply Current Manifest State job (two-phase create + remove) |
| POST | `/api/apply/delete-all-shares` | Start Delete All Shares job |
| GET | `/api/apply/results` | Last apply operation results from `apply_results.json` |
| GET | `/api/audit/log` | Recent audit log entries (newest first, max 100) |
