#!/usr/bin/env python3
"""
Zimbra Share Manager
Flask web application for auditing and managing Zimbra folder shares.
Runs on port 8585 as the zimbra OS user.

No Copyright. This software is released to the public domain and may be freely
used, modified, distributed, or incorporated into other works without restriction
or attribution.

DISCLAIMER: This software is provided "as is", without warranty of any kind,
express or implied. No representation is made that it will function correctly,
be fit for any particular purpose, or be free of defects. Use at your own risk.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import flask
from flask import Flask, jsonify, render_template, request, send_file

# ---------------------------------------------------------------------------
# Flask version compatibility
# ---------------------------------------------------------------------------

def _send_attachment(path: str, filename: str, mimetype: str = 'application/json'):
    """send_file wrapper compatible with Flask 1.x and 2.x."""
    _flask_major = int(flask.__version__.split('.')[0])
    if _flask_major >= 2:
        return send_file(path, as_attachment=True, download_name=filename, mimetype=mimetype)
    return send_file(path, as_attachment=True, attachment_filename=filename, mimetype=mimetype)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR  = BASE_DIR / 'data'
LOG_DIR   = BASE_DIR / 'log'
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

SHARES_FILE   = DATA_DIR / 'shares.json'
ACCOUNTS_FILE = DATA_DIR / 'accounts.json'
RESULTS_FILE  = DATA_DIR / 'apply_results.json'
AUDIT_FILE    = LOG_DIR  / 'audit.log'
ERRORS_FILE   = LOG_DIR  / 'errors.log'
LOG_FILE      = LOG_DIR  / 'share_manager.log'
ZIMBRA_BIN    = Path('/opt/zimbra/bin')
PORT = 8585

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log_fmt = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')

_handlers: list = [logging.StreamHandler()]
_handlers[0].setFormatter(_log_fmt)

try:
    _fh = logging.FileHandler(LOG_FILE, encoding='utf-8')
    _fh.setFormatter(_log_fmt)
    _handlers.append(_fh)
except Exception as _log_exc:
    # Don't crash on permission errors — log to stdout only and warn later
    _LOG_FILE_ERROR = str(_log_exc)
else:
    _LOG_FILE_ERROR = None

logging.basicConfig(level=logging.DEBUG, handlers=_handlers)
log = logging.getLogger('share_manager')

if _LOG_FILE_ERROR:
    log.warning("Could not open log file %s: %s — logging to stdout only", LOG_FILE, _LOG_FILE_ERROR)

# ---------------------------------------------------------------------------
# Audit log  (one JSON line per user-initiated action)
# ---------------------------------------------------------------------------

_audit_lock = threading.Lock()


def write_audit(action: str, detail: str, status: str = 'ok'):
    """Append a structured entry to audit.log. Never raises."""
    entry = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'action': action,
        'detail': detail,
        'status': status,
    }
    try:
        with _audit_lock:
            with open(AUDIT_FILE, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(entry) + '\n')
    except Exception as exc:
        log.warning("write_audit failed: %s", exc)

# ---------------------------------------------------------------------------
# Errors log  (one JSON line per per-account/per-record operational failure)
# ---------------------------------------------------------------------------

_errors_lock = threading.Lock()


def write_error_log(job: str, context: str, detail: str, level: str = 'warning'):
    """Append a structured entry to errors.log. Never raises."""
    entry = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'level': level,
        'job': job,
        'context': context,
        'detail': detail,
    }
    try:
        with _errors_lock:
            with open(ERRORS_FILE, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(entry) + '\n')
    except Exception as exc:
        log.warning("write_error_log failed: %s", exc)

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.before_request
def _handle_options_preflight():
    """Intercept CORS/PNA preflight requests before Flask routing rejects them.
    Brave and Chrome send OPTIONS with Access-Control-Request-Private-Network: true
    before any non-simple POST to a private-network server. Without this handler
    Flask returns 405, Brave blocks the follow-up request, and fetch() raises
    TypeError: Failed to fetch."""
    if request.method == 'OPTIONS':
        resp = app.make_response('')
        resp.status_code = 204
        resp.headers['Access-Control-Allow-Origin']          = request.headers.get('Origin', '*')
        resp.headers['Access-Control-Allow-Methods']         = 'GET, POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers']         = 'Content-Type'
        resp.headers['Access-Control-Allow-Private-Network'] = 'true'
        return resp


@app.after_request
def _add_private_network_header(response):
    """Stamp Access-Control-Allow-Private-Network on every response so Brave
    accepts the follow-up request after the preflight succeeds."""
    response.headers['Access-Control-Allow-Private-Network'] = 'true'
    if 'Access-Control-Allow-Origin' not in response.headers:
        response.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
    return response


# ---------------------------------------------------------------------------
# In-memory job tracking
# ---------------------------------------------------------------------------

_jobs: dict = {}
_jobs_lock = threading.Lock()


def create_job(job_type: str) -> str:
    """Create a new job entry and return its job_id."""
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            'id': job_id,
            'type': job_type,
            'status': 'running',
            'progress': 0,
            'message': 'Starting...',
            'error': None,
            'started_at': datetime.now(timezone.utc).isoformat(),
        }
    return job_id


def update_job(job_id: str, **kwargs):
    """Update fields on an existing job dict. Auto-stamps completed_at on terminal status."""
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)
            if kwargs.get('status') in ('done', 'error'):
                _jobs[job_id].setdefault(
                    'completed_at', datetime.now(timezone.utc).isoformat()
                )


def get_job(job_id: str) -> dict | None:
    """Return a copy of the job dict, or None if not found."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


# ---------------------------------------------------------------------------
# Manifest I/O
# ---------------------------------------------------------------------------

_manifest_lock = threading.Lock()


def read_manifest() -> dict | None:
    """Read and parse shares.json. Returns None if file does not exist or is invalid."""
    with _manifest_lock:
        if not SHARES_FILE.exists():
            return None
        try:
            with open(SHARES_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as exc:
            log.error("Failed to read manifest: %s", exc)
            return None


def write_manifest(data: dict):
    """
    Write data to shares.json atomically (thread-safe).
    Writes to a .tmp file first, then replaces — prevents corruption on crash.
    """
    with _manifest_lock:
        tmp = SHARES_FILE.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        tmp.replace(SHARES_FILE)  # atomic on POSIX; best-effort on Windows


# ---------------------------------------------------------------------------
# Manifest versioning
# ---------------------------------------------------------------------------

def archive_manifest_version() -> int:
    """
    Copy shares.json to shares_vN.json, where N is its current _version value.
    Version numbers are monotonically increasing — never reset, so archives
    never conflict.  If shares_vN.json somehow already exists, N is incremented
    until a free name is found (logged as a warning).

    Returns the version number used for the archive, or 0 if nothing to archive.
    """
    if not SHARES_FILE.exists():
        return 0
    manifest_data = read_manifest()
    if not manifest_data:
        return 0

    ver = manifest_data.get('_version', 1)
    archive_path = DATA_DIR / f'shares_v{ver}.json'

    if archive_path.exists():
        orig = ver
        while archive_path.exists():
            ver += 1
            archive_path = DATA_DIR / f'shares_v{ver}.json'
        log.warning(
            "Archive conflict: shares_v%d.json already existed; using shares_v%d.json",
            orig, ver,
        )

    shutil.copy2(str(SHARES_FILE), str(archive_path))
    log.info("Manifest archived as %s (was version %d)", archive_path.name,
             manifest_data.get('_version', 1))
    return ver


def inject_version_metadata(data: dict, version: int, action: str, **history_fields) -> dict:
    """
    Return a new dict that has version metadata as its FIRST keys, followed by
    all non-underscore keys from *data*.  The _version_history list is preserved
    and the new entry is appended.

    Because json.dump preserves insertion order (CPython 3.7+), the metadata
    block appears at the top of every shares.json file — acting as a human-
    readable header without requiring non-standard JSON comment syntax.
    """
    now = datetime.now(timezone.utc).isoformat()

    history_entry: dict = {
        'version': version,
        'timestamp': now,
        'action': action,
        **history_fields,
    }

    prev_history: list = data.get('_version_history', [])
    new_history = list(prev_history) + [history_entry]

    # Build result with metadata first, then data payload
    result: dict = {
        '_version': version,
        '_last_action': action,
        '_last_updated': now,
        '_version_history': new_history,
    }
    for k, v in data.items():
        if not k.startswith('_'):
            result[k] = v

    return result


# ---------------------------------------------------------------------------
# Zimbra interface
# ---------------------------------------------------------------------------

def zimbra_cmd(args: list, timeout: int = 60) -> str:
    """
    Run a zimbra binary from ZIMBRA_BIN and return stdout.
    Raises RuntimeError on non-zero exit or timeout.
    """
    binary = ZIMBRA_BIN / args[0]
    cmd = [str(binary)] + args[1:]
    log.debug("zimbra_cmd: %s", ' '.join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Command {args[0]} failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        return result.stdout
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command {args[0]} timed out after {timeout}s")


def zimbra_cmd_with_retry(args: list, timeout: int = 60, retries: int = 2, delay: float = 2.0) -> str:
    """
    Run a zimbra command with up to `retries` automatic retries on failure.
    Waits `delay` seconds between attempts. Use for mutating operations
    that may hit transient mailbox lock or connectivity errors.
    """
    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(retries + 1):
        try:
            return zimbra_cmd(args, timeout=timeout)
        except RuntimeError as exc:
            last_exc = exc
            if attempt < retries:
                log.warning(
                    "zimbra_cmd retry %d/%d for %s: %s",
                    attempt + 1, retries, args[0], exc,
                )
                time.sleep(delay)
    raise last_exc


# ---------------------------------------------------------------------------
# Server info
# ---------------------------------------------------------------------------

def get_server_info() -> dict:
    """Gather server/Zimbra information and manifest summary."""
    # OS from /etc/os-release
    os_name = 'Unknown'
    try:
        with open('/etc/os-release', 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('PRETTY_NAME='):
                    os_name = line.split('=', 1)[1].strip().strip('"')
                    break
    except Exception as exc:
        log.warning("Could not read /etc/os-release: %s", exc)

    # Zimbra version — try 'zmcontrol -v', fall back to zmlocalconfig
    zimbra_version = 'Unknown'
    try:
        zimbra_version = zimbra_cmd(['zmcontrol', '-v'], timeout=30).strip()
    except Exception:
        try:
            zimbra_version = zimbra_cmd(['zmlocalconfig', 'zimbra_version'], timeout=30).strip()
        except Exception as exc:
            log.warning("Could not determine zimbra version: %s", exc)

    # Hostname
    hostname = 'Unknown'
    try:
        hostname = zimbra_cmd(['zmhostname'], timeout=15).strip()
    except Exception as exc:
        log.warning("zmhostname failed: %s", exc)

    # Zimbra status — rc=1 is normal when any service is stopped; capture stdout anyway
    zimbra_status = 'Unknown'
    try:
        binary = str(ZIMBRA_BIN / 'zmcontrol')
        _r = subprocess.run(
            [binary, 'status'],
            capture_output=True, text=True, timeout=60,
        )
        zimbra_status = (_r.stdout or _r.stderr or 'Unknown').strip()
    except Exception as exc:
        log.warning("zmcontrol status failed: %s", exc)

    # Manifest summary
    manifest_summary: dict = {
        'exists': False,
        'version': None,
        'last_action': None,
        'last_updated': None,
        'record_count': 0,
        'keep_count': 0,
        'remove_count': 0,
        'orphan_count': 0,
        'collected_from': None,
        'collected_at': None,
    }
    manifest = read_manifest()
    if manifest:
        records = manifest.get('records', [])
        manifest_summary['exists'] = True
        manifest_summary['version'] = manifest.get('_version')
        manifest_summary['last_action'] = manifest.get('_last_action')
        manifest_summary['last_updated'] = manifest.get('_last_updated')
        manifest_summary['record_count'] = len(records)
        manifest_summary['keep_count'] = sum(1 for r in records if r.get('keep', 1) == 1)
        manifest_summary['remove_count'] = sum(1 for r in records if r.get('keep', 1) == 0)
        manifest_summary['orphan_count'] = sum(
            1 for r in records
            if r.get('source_orphaned') or r.get('grantee_orphaned')
        )
        manifest_summary['collected_from'] = manifest.get('collected_from')
        manifest_summary['collected_at'] = manifest.get('collected_at')

    return {
        'hostname': hostname,
        'os': os_name,
        'zimbra_version': zimbra_version,
        'zimbra_status': zimbra_status,
        'manifest': manifest_summary,
    }


# ---------------------------------------------------------------------------
# Account helpers
# ---------------------------------------------------------------------------

def get_all_accounts() -> list:
    """Run zmprov -l gaa and return sorted lowercase list of email addresses."""
    output = zimbra_cmd(['zmprov', '-l', 'gaa'], timeout=120)
    accounts = []
    for line in output.splitlines():
        line = line.strip().lower()
        if line and '@' in line:
            accounts.append(line)
    return sorted(set(accounts))


# ---------------------------------------------------------------------------
# Share info parsers
# ---------------------------------------------------------------------------

def parse_getshareinfo(output: str, owner_account: str, all_accounts: set = None) -> list:
    """
    Parse zmprov gsi (getShareInfo) fixed-width columnar table output.
    Column widths are determined at runtime from the dashes separator line,
    so this is resilient to future Zimbra layout changes.

    Grantee email addresses are truncated by zmprov to fit the column width.
    all_accounts (the full account list from Phase 1) is used to expand them
    by prefix-matching against the truncated value.

    Returns list of dicts: {owner_email, folder_id, folder_path, rights,
                             mountpoint_id, grantee_account}.
    folder_id is included so Phase 4 can resolve the full path via id_maps.
    """
    log.debug("parse_getshareinfo raw output for %s:\n%s", owner_account, output)
    shares = []
    try:
        lines = output.splitlines()

        # Locate the separator line: a line consisting solely of dashes and spaces
        sep_idx = None
        for i, line in enumerate(lines):
            s = line.strip()
            if s and re.match(r'^[-\s]+$', s) and '-' in s:
                sep_idx = i
                break

        if sep_idx is None or sep_idx == 0:
            # No table found — account has no outgoing grants
            return shares

        sep_line = lines[sep_idx]
        data_lines = lines[sep_idx + 1:]

        if not any(ln.strip() for ln in data_lines):
            return shares  # header present but no data rows

        # Build (start, end) column spans from the separator line
        col_spans: list = []
        i = 0
        while i < len(sep_line):
            if sep_line[i] == '-':
                start = i
                while i < len(sep_line) and sep_line[i] == '-':
                    i += 1
                col_spans.append((start, i))
            else:
                i += 1

        if not col_spans:
            return shares

        # Map column names from the header line above the separator
        header_line = lines[sep_idx - 1]
        headers = []
        for start, end in col_spans:
            h = header_line[start:min(end, len(header_line))].strip().lower().replace(' ', '')
            headers.append(h)

        col_idx = {name: idx for idx, name in enumerate(headers)}

        def col(vals: list, *names: str) -> str:
            for n in names:
                i2 = col_idx.get(n)
                if i2 is not None and i2 < len(vals):
                    v = vals[i2].strip()
                    if v:
                        return v
            return ''

        # Parse each data row using fixed column positions
        for line in data_lines:
            if not line.strip():
                continue

            vals = []
            for start, end in col_spans:
                if start >= len(line):
                    vals.append('')
                else:
                    vals.append(line[start:min(end, len(line))])

            folder_path   = col(vals, 'path', 'folderpath')
            folder_id     = col(vals, 'id', 'folderid')
            rights        = col(vals, 'rights')
            grantee_raw   = col(vals, 'granteename', 'grantee').lower()
            gt            = col(vals, 'gt').lower()
            mid_raw       = col(vals, 'mid')
            mountpoint_id = mid_raw if (mid_raw and mid_raw != '0') else None

            if not folder_path or not grantee_raw:
                continue

            # Group/domain/public grants have no individual grantee to act on directly,
            # but distribution-list members may have the folder mounted. Track these
            # grants so their members' mounts are not misidentified as orphaned.
            # The grantee field holds the DL/domain email — possibly truncated since DLs
            # are not in the user accounts list and can't be expanded — stored as-is.
            if gt and gt not in ('usr', ''):
                shares.append({
                    'owner_email':     owner_account,
                    'folder_id':       folder_id,
                    'folder_path':     folder_path,
                    'rights':          rights or 'r',
                    'mountpoint_id':   None,
                    'grantee_account': grantee_raw.strip(),
                    'grant_type':      'group',
                    'gt':              gt,
                })
                continue

            # Expand truncated email via prefix-match against the known account list.
            # IMPORTANT: do this BEFORE the '@' check — the granteename column is only
            # 15 chars wide, so usernames ≥ 15 chars are truncated without an '@'.
            # e.g. "stephen.hainline@..." becomes "stephen.hainlin" (no '@').
            grantee = grantee_raw
            if all_accounts:
                matches = [a for a in all_accounts if a.startswith(grantee_raw)]
                if len(matches) == 1:
                    grantee = matches[0]
                elif len(matches) > 1:
                    # Multiple accounts share the same prefix — use shortest (most likely exact)
                    matches.sort(key=len)
                    grantee = matches[0]
                    log.debug("parse_getshareinfo: ambiguous grantee prefix %r (%d matches), using %r",
                              grantee_raw, len(matches), grantee)
                # len == 0: keep truncated value as-is (external grantee or unknown)

            # Skip if we still have no valid email (truly unresolvable grantee)
            if '@' not in grantee:
                log.debug("parse_getshareinfo: skipping unresolvable grantee %r for %s/%s",
                          grantee_raw, owner_account, folder_path)
                continue

            shares.append({
                'owner_email':     owner_account,
                'folder_id':       folder_id,
                'folder_path':     folder_path,
                'rights':          rights or 'r',
                'mountpoint_id':   mountpoint_id,
                'grantee_account': grantee,
                'grant_type':      'user',
                'gt':              'usr',
            })

    except Exception as exc:
        log.warning("parse_getshareinfo failed for %s: %s", owner_account, exc)
        return []
    return shares


def parse_getallfolders_id_map(output: str) -> dict:
    """
    Extract folder_id -> folder_path mapping from zmmailbox getAllFolders output.
    Returns dict of {str(id): path}.

    Format: Id  View  Unread  MsgCount  Path
    Mount entries append (owner@domain:remote_folder_id) to the path, e.g.:
        6448  unkn  0  0  /AP2 (ap2@azurestandard.com:1)
    The local path stored is the name BEFORE the annotation.
    """
    id_map: dict = {}
    try:
        for line in output.splitlines():
            # Line must start with a numeric folder ID
            m = re.match(r'^\s*(\d+)\s', line)
            if not m:
                continue
            folder_id = m.group(1)

            # Strip mount annotation (owner@domain:folder_id) at end of path only;
            # preserve folder names that contain regular parentheses.
            clean = re.sub(r'\s*\([\w.+-]+@[\w.-]+:\d+\)\s*$', '', line).rstrip()

            # The path is everything from the rightmost ' /' to end of cleaned line
            sep = clean.rfind(' /')
            if sep == -1:
                continue
            path = clean[sep + 1:].strip()
            if path.startswith('/'):
                id_map[folder_id] = path
    except Exception as exc:
        log.warning("parse_getallfolders_id_map failed: %s", exc)
    return id_map


def parse_getallfolders_mounts(output: str, account: str, all_id_maps: dict = None) -> list:
    """
    Extract mountpoint folders from zmmailbox getAllFolders output.
    Returns list of {folder_id, mount_path, owner_account, remote_folder, grantee_account}.

    Zimbra encodes mount info directly in the path column:
        6448  unkn  0  0  /AP2 (ap2@azurestandard.com:1)
        6636  mess  47  48  /shared-ap2-Inbox (ap2@azurestandard.com:2)

    The annotation (owner@domain:remote_folder_id) at the end of the path
    identifies both the share owner and which of their folders is mounted.

    all_id_maps: complete {account -> {folder_id -> path}} dict built during
    Phase 3 pass 1. Used to resolve the owner's remote_folder_id to a path.
    Must be fully populated before calling this function.
    """
    mounts = []
    try:
        for line in output.splitlines():
            # Line must start with a numeric folder ID
            line_m = re.match(r'^\s*(\d+)\s', line)
            if not line_m:
                continue
            folder_id = line_m.group(1)

            # Mount entries end with (owner@domain:remote_folder_id)
            ann = re.search(r'\(([\w.+-]+@[\w.-]+):(\d+)\)\s*$', line)
            if not ann:
                continue

            owner_account  = ann.group(1).lower()
            remote_id      = ann.group(2)

            # Local mount path: strip annotation, then take everything from rightmost ' /'
            clean = re.sub(r'\s*\([\w.+-]+@[\w.-]+:\d+\)\s*$', '', line).rstrip()
            sep = clean.rfind(' /')
            if sep == -1:
                continue
            mount_path = clean[sep + 1:].strip()
            if not mount_path.startswith('/'):
                continue

            # Resolve the owner's remote folder ID to its path
            remote_folder = None
            if all_id_maps and owner_account in all_id_maps:
                remote_folder = all_id_maps[owner_account].get(remote_id)
                log.debug(
                    "parse_getallfolders_mounts: %s -> %s:%s resolved to %r",
                    account, owner_account, remote_id, remote_folder,
                )

            mounts.append({
                'folder_id':       folder_id,
                'mount_path':      mount_path,
                'owner_account':   owner_account,
                'remote_id':       remote_id,    # exact untruncated ID from the annotation
                'remote_folder':   remote_folder,
                'grantee_account': account,
            })
    except Exception as exc:
        log.warning("parse_getallfolders_mounts failed for %s: %s", account, exc)
        return []
    return mounts


# ---------------------------------------------------------------------------
# Record ID helper
# ---------------------------------------------------------------------------

def make_record_id(source_account: str, source_folder: str, grantee_account: str) -> str:
    """Return first 16 chars of SHA256 of 'source::folder::grantee'."""
    raw = f"{source_account}::{source_folder}::{grantee_account}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def _find_parent_mount(owner: str, norm_folder: str, grantee: str, mount_lookup: dict):
    """
    Walk norm_folder's ancestors from immediate parent up to root, returning
    the first mount_lookup entry found and the ancestor path it matched.
    Returns (mount_entry, ancestor_path) or (None, None).

    This covers subfolder shares: if /Inbox is mounted, a grant on
    /Inbox/Work is reachable through that same mountpoint.
    """
    parts = norm_folder.split('/')
    for depth in range(len(parts) - 1, 0, -1):
        prefix = '/'.join(parts[:depth]) or '/'
        key = (owner, prefix, grantee)
        if key in mount_lookup:
            return mount_lookup[key], prefix
    return None, None


def normalize_folder(path: str) -> str:
    """Normalize folder paths for consistent matching. Root must always be exactly '/'."""
    if not path:
        return ''
    path = str(path).strip()
    if path in ('', '/', '/ ', '\\', ':/', ': /', ':1', '1'):
        return '/'
    if not path.startswith('/'):
        path = '/' + path
    normalized = path.rstrip('/')
    return normalized if normalized else '/'


# ---------------------------------------------------------------------------
# Background job: collect shares
# ---------------------------------------------------------------------------

def collect_shares_job(job_id: str):
    """Background thread: collect all share data and write manifest."""
    try:
        # Phase 1 (0-5%): Get all accounts
        update_job(job_id, progress=0, message='Fetching all accounts...')
        log.info("[collect] Phase 1: fetching accounts")
        accounts = get_all_accounts()
        active_set = set(accounts)

        accounts_data = {
            'collected_at': datetime.now(timezone.utc).isoformat(),
            'count': len(accounts),
            'accounts': accounts,
        }
        with open(ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(accounts_data, f, indent=2)

        update_job(job_id, progress=5, message=f'Found {len(accounts)} accounts. Collecting share info...')
        log.info("[collect] Phase 1 done: %d accounts", len(accounts))

        # Phase 2 (5-70%): getShareInfo for each account
        # Key by folder_id (when available) rather than folder_path to avoid truncation-based
        # key collisions: gsi's path column is ~20 chars wide, so two different grants whose
        # paths share a long common prefix (e.g. "/Inbox/Zimbra Virus" and
        # "/Inbox/Zimbra Virus False Positive Bypass/…") both truncate to the same string and
        # one would silently overwrite the other in a path-keyed dict.
        shares_by_key: dict = {}    # (owner_email, folder_id-or-path, grantee_account) -> share dict (user grants only)
        group_grants_raw: list = []  # group/DL/domain grants — no individual grantee to act on
        total = len(accounts)
        phase2_range = 65  # 5 to 70

        for idx, account in enumerate(accounts):
            pct = 5 + int((idx / max(total, 1)) * phase2_range)
            update_job(job_id, progress=pct, message=f'getShareInfo: {account} ({idx+1}/{total})')
            try:
                output = zimbra_cmd(
                    ['zmprov', 'gsi', account],
                    timeout=60,
                )
                shares = parse_getshareinfo(output, account, active_set)
                for share in shares:
                    if share.get('grant_type') == 'group':
                        group_grants_raw.append(share)
                    else:
                        fid = share.get('folder_id', '')
                        if fid and fid != '0':
                            key = (share['owner_email'], f'id:{fid}', share['grantee_account'])
                        else:
                            key = (share['owner_email'], share['folder_path'], share['grantee_account'])
                        shares_by_key[key] = share
            except Exception as exc:
                log.warning("[collect] getShareInfo failed for %s: %s", account, exc)
                write_error_log('collect', account, f'getShareInfo failed: {exc}')

        update_job(job_id, progress=70, message=f'Share info collected ({len(shares_by_key)} entries). Collecting folder info...')
        log.info("[collect] Phase 2 done: %d share entries", len(shares_by_key))

        # Phase 3 (70-90%): getAllFolders for all accounts — two passes.
        # Pass 1 builds the complete id_maps so Pass 2 can resolve an owner's
        # remote folder ID using the owner's own id_map (not the grantee's).
        grantee_accounts = set(k[2] for k in shares_by_key.keys())
        scan_accounts = grantee_accounts | active_set
        id_maps: dict = {}        # account -> {folder_id -> folder_path}
        folder_outputs: dict = {} # account -> raw getAllFolders output (held for pass 2)
        all_mounts: list = []
        total3 = len(scan_accounts)
        scan_list = sorted(scan_accounts)

        # Pass 1 (70-82%): build id_maps for every account
        for idx, account in enumerate(scan_list):
            pct = 70 + int((idx / max(total3, 1)) * 12)
            update_job(job_id, progress=pct, message=f'getAllFolders (ids): {account} ({idx+1}/{total3})')
            try:
                output = zimbra_cmd(
                    ['zmmailbox', '-z', '-m', account, 'getAllFolders'],
                    timeout=60,
                )
                id_maps[account] = parse_getallfolders_id_map(output)
                folder_outputs[account] = output
            except Exception as exc:
                log.warning("[collect] getAllFolders failed for %s: %s", account, exc)
                write_error_log('collect', account, f'getAllFolders failed: {exc}')
                id_maps[account] = {}
                folder_outputs[account] = ''

        # Pass 2 (82-90%): parse mounts now that all owner id_maps are available
        update_job(job_id, progress=82, message='Parsing mountpoints...')
        for account, output in folder_outputs.items():
            if not output:
                continue
            try:
                mounts = parse_getallfolders_mounts(output, account, all_id_maps=id_maps)
                all_mounts.extend(mounts)
            except Exception as exc:
                log.warning("[collect] Mount parsing failed for %s: %s", account, exc)
                write_error_log('collect', account, f'Mount parsing failed: {exc}')

        update_job(job_id, progress=90, message='Building manifest records...')
        log.info("[collect] Phase 3 done: %d mounts found", len(all_mounts))

        # Phase 4 (90-95%): Build manifest records
        records = []

        # Build mount lookup keyed by (owner, normalized_remote_folder, grantee)
        mount_lookup: dict = {}
        for mount in all_mounts:
            m_owner   = mount.get('owner_account')
            m_remote  = normalize_folder(mount.get('remote_folder'))
            m_grantee = mount.get('grantee_account')
            if m_owner and m_remote and m_grantee:
                mount_lookup[(m_owner, m_remote, m_grantee)] = mount

        log.debug("Phase 4: built mount_lookup with %d entries (root shares should now match)", len(mount_lookup))

        # Build a targeted lookup for truncated folder_id resolution.
        # Keyed by (owner, grantee) -> list of (remote_id, resolved_remote_folder).
        # The remote_id comes directly from the mount annotation in getAllFolders and is
        # never truncated, so it can be used to recover the full ID that gsi cut to 5 chars.
        mount_remote_ids: dict = {}
        for _m in all_mounts:
            _mown = _m.get('owner_account')
            _mgra = _m.get('grantee_account')
            _mrid = _m.get('remote_id')
            _mrfp = _m.get('remote_folder')
            if _mown and _mgra and _mrid and _mrfp:
                mount_remote_ids.setdefault((_mown, _mgra), []).append((_mrid, _mrfp))

        def _resolve_folder_id(folder_id: str, owner: str, grantee: str) -> str | None:
            """Exact id_map lookup, then targeted mount-based prefix fallback."""
            if not folder_id or folder_id == '0':
                return None
            resolved = id_maps.get(owner, {}).get(str(folder_id))
            if not resolved and len(str(folder_id)) == 5:
                # gsi id column is 5 chars — a 6-digit ID is truncated to 5.
                # Check the grantee's actual mounts for this owner: the mount annotation
                # contains the exact untruncated remote_id, so prefix-matching here is
                # safe — it's bounded to mounts between this specific (owner, grantee) pair.
                pfx = str(folder_id)
                cands = [rfp for rid, rfp in mount_remote_ids.get((owner, grantee), [])
                         if str(rid).startswith(pfx) and len(str(rid)) > 5]
                # Deduplicate by path: the same source folder can be mounted twice under
                # different local names, producing multiple cands with the same remote_id
                # and the same resolved path — that is still unambiguous.
                unique_cands = list(dict.fromkeys(cands))
                if len(unique_cands) == 1:
                    resolved = unique_cands[0]
                    log.debug("Phase 4: folder_id %r resolved via mount remote_id prefix to %r (%s->%s)",
                              folder_id, resolved, owner, grantee)
                elif unique_cands:
                    log.debug("Phase 4: folder_id %r ambiguous (%d distinct mount paths) for %s->%s",
                              folder_id, len(unique_cands), owner, grantee)
            return resolved

        # Resolve group/DL grant folder paths and build a lookup keyed by (owner, norm_folder).
        # For group grants we don't know the individual grantee, so the fallback searches all
        # mounts from that owner (across all grantees) rather than the grantee-specific set.
        owner_mount_remote_ids: dict = {}
        for _m in all_mounts:
            _mown = _m.get('owner_account')
            _mrid = _m.get('remote_id')
            _mrfp = _m.get('remote_folder')
            if _mown and _mrid and _mrfp:
                owner_mount_remote_ids.setdefault(_mown, []).append((_mrid, _mrfp))

        group_grant_folder_set: dict = {}  # (owner, norm_folder) -> {'dl': str, 'rights': str}
        for _gg in group_grants_raw:
            _own = _gg['owner_email']
            _fp  = _gg['folder_path']
            _fid = _gg.get('folder_id', '')
            _res = id_maps.get(_own, {}).get(str(_fid)) if (_fid and _fid != '0') else None
            if not _res and _fid and len(str(_fid)) == 5:
                _pfx = str(_fid)
                _oc = list(dict.fromkeys(
                    rfp for rid, rfp in owner_mount_remote_ids.get(_own, [])
                    if str(rid).startswith(_pfx) and len(str(rid)) > 5
                ))
                if len(_oc) == 1:
                    _res = _oc[0]
            if _res:
                _fp = _res
            _norm = normalize_folder(_fp)
            if _norm and _own and (_own, _norm) not in group_grant_folder_set:
                group_grant_folder_set[(_own, _norm)] = {
                    'dl': _gg.get('grantee_account', ''),
                    'rights': _gg.get('rights', ''),
                }
        log.info("[collect] Phase 4: %d group-grant-covered folder(s) found", len(group_grant_folder_set))

        # Pre-pass: resolve every grant's folder path (via id_maps) into a normalized set so
        # that mount_is_inherited checks use resolved paths, not the raw truncated gsi paths
        # that are now used as keys in shares_by_key.
        resolved_norm_grant_set: set = set()
        for _, _pre_share in shares_by_key.items():
            _own = _pre_share['owner_email']
            _fp  = _pre_share['folder_path']
            _res = _resolve_folder_id(_pre_share.get('folder_id', ''), _own, _pre_share['grantee_account'])
            if _res:
                _fp = _res
            resolved_norm_grant_set.add((_own, normalize_folder(_fp), _pre_share['grantee_account']))

        matched_mount_keys: set = set()

        # Records from shares_by_key (grants)
        for key, share in shares_by_key.items():
            owner_email = share['owner_email']
            folder_path = share['folder_path']
            grantee     = share['grantee_account']
            mountpoint_id = share.get('mountpoint_id')

            # Resolve full folder path via id_maps (with truncated-ID fallback).
            # Both the 'id' column (5 chars) and 'path' column (20 chars) in zmprov gsi
            # are too narrow for deep folder paths, so either can be truncated.
            # _resolve_folder_id handles exact lookup and, when that fails, matches the
            # 5-char truncated ID against the grantee's mount remote_ids for this owner —
            # a targeted check bounded to this specific (owner, grantee) pair.
            folder_id = share.get('folder_id', '')
            resolved = _resolve_folder_id(folder_id, owner_email, grantee)
            if resolved:
                folder_path = resolved

            # --- Detect mount ---
            # Primary: use mid from gsi (populated in some Zimbra versions)
            grantee_mountpoint = None
            has_mount = False
            mount_is_inherited = False  # True = covered by parent mount, not a real mountpoint entry
            if mountpoint_id and grantee in id_maps:
                grantee_mountpoint = id_maps[grantee].get(str(mountpoint_id))
                if grantee_mountpoint:
                    has_mount = True

            # Fallback: cross-reference with all_mounts using the resolved folder_path.
            # The old code used unresolved (truncated) paths as the key, causing every
            # real mount to appear as "orphaned" instead of being merged here.
            if not has_mount:
                norm_folder = normalize_folder(folder_path)
                mount_key = (owner_email, norm_folder, grantee)
                if mount_key in mount_lookup:
                    mount_entry = mount_lookup[mount_key]
                    # Prefer id_maps lookup for full path; fall back to parsed mount_path
                    mid = mount_entry.get('folder_id', '')
                    if mid and grantee in id_maps:
                        grantee_mountpoint = id_maps[grantee].get(str(mid)) or mount_entry.get('mount_path')
                    else:
                        grantee_mountpoint = mount_entry.get('mount_path')
                    has_mount = True
                    matched_mount_keys.add(mount_key)
                else:
                    # Subfolder shares are covered by a parent mountpoint — check ancestors
                    parent_entry, parent_folder = _find_parent_mount(
                        owner_email, norm_folder, grantee, mount_lookup
                    )
                    if parent_entry:
                        parent_mount_path = parent_entry.get('mount_path') or ''
                        # Root parent ('/') has len 1, so slicing would strip the leading slash
                        # from norm_folder (e.g. '/ALERTS'[1:] = 'ALERTS').  For root, keep the
                        # full norm_folder as the suffix; for deeper parents, slicing is correct
                        # because it lands on the '/' separator (e.g. '/Inbox/Work'[6:] = '/Work').
                        suffix = norm_folder if parent_folder == '/' else norm_folder[len(parent_folder):]
                        grantee_mountpoint = (parent_mount_path + suffix) if parent_mount_path else None
                        has_mount = True
                        # Only suppress as inherited when the parent grant itself is also in the
                        # manifest — if only the parent mount is present (orphaned), keep this
                        # record visible as a standalone share.
                        # Use resolved_norm_grant_set (not shares_by_key) because keys in
                        # shares_by_key are now id-based to avoid truncation collisions.
                        parent_grant_key = (owner_email, parent_folder, grantee)
                        mount_is_inherited = parent_grant_key in resolved_norm_grant_set

            records.append({
                'id': make_record_id(owner_email, folder_path, grantee),
                'source_account': owner_email,
                'source_folder': folder_path,
                'permissions': share.get('rights', ''),
                'grantee_account': grantee,
                'grantee_mountpoint': grantee_mountpoint,
                'has_grant': True,
                'has_mount': has_mount,
                'mount_is_inherited': mount_is_inherited,
                'grant_via_group': None,
                'source_orphaned': owner_email not in active_set,
                'grantee_orphaned': grantee not in active_set,
                'keep': 1,
            })

        # Orphaned mounts: mounts that were NOT matched to any grant above
        for mount in all_mounts:
            owner   = mount.get('owner_account')
            remote  = mount.get('remote_folder')
            grantee = mount.get('grantee_account')
            if not (owner and remote and grantee):
                continue
            norm_folder = normalize_folder(remote)
            mount_key = (owner, norm_folder, grantee)
            if mount_key not in matched_mount_keys:
                # Check if this mount is backed by a group/DL grant on the same folder.
                # If so, the mount is legitimate even though no individual grant exists.
                group_grant = group_grant_folder_set.get((owner, norm_folder))
                records.append({
                    'id': make_record_id(owner, remote, grantee),
                    'source_account': owner,
                    'source_folder': remote,
                    'permissions': group_grant['rights'] if group_grant else '',
                    'grantee_account': grantee,
                    'grantee_mountpoint': mount.get('mount_path'),
                    'has_grant': group_grant is not None,
                    'has_mount': True,
                    'mount_is_inherited': False,
                    'grant_via_group': group_grant['dl'] if group_grant else None,
                    'source_orphaned': owner not in active_set,
                    'grantee_orphaned': grantee not in active_set,
                    'keep': 1,
                })

        update_job(job_id, progress=95, message='Sorting and writing manifest...')
        log.info("[collect] Phase 4 done: %d records", len(records))

        # Phase 5 (95-100%): Sort and write manifest
        records.sort(key=lambda r: (r['source_account'], r['source_folder'], r['grantee_account']))

        hostname = 'unknown'
        try:
            hostname = zimbra_cmd(['zmhostname'], timeout=15).strip()
        except Exception:
            pass

        # Archive the old manifest (if any) and determine the new version number
        archived_ver = archive_manifest_version()
        new_version = archived_ver + 1 if archived_ver > 0 else 1

        # Build the payload without metadata first, then inject metadata as top keys
        payload: dict = {
            'collected_from': hostname,
            'collected_at': datetime.now(timezone.utc).isoformat(),
            'record_count': len(records),
            'records': records,
        }
        manifest = inject_version_metadata(
            payload,
            version=new_version,
            action='collect',
            collected_from=hostname,
            record_count=len(records),
        )
        write_manifest(manifest)

        summary = f'Collection complete — {len(records)} records written to manifest (v{new_version}).'
        write_audit('collect_shares', summary, status='ok')
        update_job(job_id, status='done', progress=100, message=summary)
        log.info("[collect] %s", summary)

    except Exception as exc:
        log.exception("[collect] Unhandled error in collect_shares_job")
        summary = f'Error: {exc}'
        write_audit('collect_shares', summary, status='error')
        update_job(job_id, status='error', error=str(exc), message=summary)

    finally:
        job = get_job(job_id)
        if job and job.get('status') == 'running':
            update_job(job_id, status='error', error='Job ended unexpectedly',
                       message='Error: job ended unexpectedly')


# ---------------------------------------------------------------------------
# Background job: apply cleanup
# ---------------------------------------------------------------------------

def apply_cleanup_job(job_id: str):
    """Background thread: apply the full manifest state.

    Phase 1 — create grants and mounts for every keep=1 record.
    Phase 2 — revoke grants and delete mounts for every keep=0 record.
    """
    created_grants = 0
    created_mounts = 0
    existing_mounts = 0   # mounts that were already present (treated as success)
    removed_grants = 0
    removed_mounts = 0
    skipped_details: list = []
    partial_failures: list = []
    error_details: list = []

    try:
        update_job(job_id, progress=5, message='Loading manifest and accounts...')
        manifest = read_manifest()
        if not manifest:
            raise RuntimeError("No manifest found")

        try:
            active_accounts = set(get_all_accounts())
        except Exception as exc:
            raise RuntimeError(f"Failed to get accounts: {exc}")

        records = manifest.get('records', [])
        to_create = [r for r in records if r.get('keep', 1) == 1]
        to_remove = [r for r in records if r.get('keep', 1) == 0]
        log.info("[apply] Phase 1: %d keep=1 records to create; Phase 2: %d keep=0 records to remove",
                 len(to_create), len(to_remove))

        # ------------------------------------------------------------------
        # Phase 1 — create / re-create shares for keep=1 records
        # ------------------------------------------------------------------
        total_create = len(to_create)
        update_job(job_id, progress=8, message=f'Phase 1: creating {total_create} keep records...')

        for idx, record in enumerate(to_create):
            pct = 8 + int((idx / max(total_create, 1)) * 42)
            update_job(job_id, progress=pct, message=f'Creating {idx+1}/{total_create}...')

            source      = record.get('source_account', '')
            folder      = record.get('source_folder', '')
            grantee     = record.get('grantee_account', '')
            mount_path  = record.get('grantee_mountpoint')
            permissions = record.get('permissions', 'r') or 'r'

            missing = []
            if source not in active_accounts:
                missing.append(f'source {source!r} not on this server')
            if grantee not in active_accounts:
                missing.append(f'grantee {grantee!r} not on this server')
            if missing:
                reason = '; '.join(missing)
                log.info("[apply] Skipping %s/%s->%s: %s", source, folder, grantee, reason)
                skipped_details.append({'source': source, 'folder': folder,
                                        'grantee': grantee, 'reason': reason})
                continue

            # Create / update grant
            grant_ok = False
            try:
                zimbra_cmd_with_retry(
                    ['zmmailbox', '-z', '-m', source,
                     'modifyFolderGrant', folder, 'account', grantee, permissions],
                    timeout=60,
                )
                grant_ok = True
                created_grants += 1
                log.info("[apply] Set grant: %s/%s -> %s (%s)", source, folder, grantee, permissions)
            except Exception as exc:
                if 'grant_exists' in str(exc).lower():
                    # Grant already exists for this grantee — treat as success
                    grant_ok = True
                    created_grants += 1
                    log.info("[apply] Grant already exists (ok): %s/%s -> %s", source, folder, grantee)
                else:
                    err = f"Grant failed {source}/{folder} -> {grantee}: {exc}"
                    log.error("[apply] %s", err)
                    write_error_log('apply', f'{source} → {grantee}', f'Grant creation failed on {folder}: {exc}', level='error')
                    error_details.append({'type': 'grant_creation', 'source': source,
                                          'folder': folder, 'grantee': grantee, 'error': str(exc)})
                    continue

            # Create mountpoint — skip inherited subfolders (parent mount covers them)
            if mount_path and grant_ok and not record.get('mount_is_inherited'):
                try:
                    zimbra_cmd_with_retry(
                        ['zmmailbox', '-z', '-m', grantee,
                         'createMountpoint', mount_path, source, folder],
                        timeout=60,
                    )
                    created_mounts += 1
                    log.info("[apply] Created mount: %s:%s -> %s:%s",
                             grantee, mount_path, source, folder)
                except RuntimeError as mount_exc:
                    if 'already exists' in str(mount_exc).lower():
                        # Mount is already present — grant is set, nothing more to do
                        existing_mounts += 1
                        log.info("[apply] Mount already exists (ok): %s:%s", grantee, mount_path)
                    else:
                        # Real mount failure — roll back the grant we just created
                        log.warning("[apply] Mount failed %s:%s, attempting grant rollback: %s",
                                    grantee, mount_path, mount_exc)
                        try:
                            zimbra_cmd(
                                ['zmmailbox', '-z', '-m', source,
                                 'modifyFolderGrant', folder, 'account', grantee, 'none'],
                                timeout=30,
                            )
                            created_grants -= 1
                            log.info("[apply] Rolled back grant after mount failure: %s/%s -> %s",
                                     source, folder, grantee)
                            partial_failures.append({
                                'source': source, 'folder': folder, 'grantee': grantee,
                                'mount_path': mount_path,
                                'note': 'mount failed — grant rolled back',
                                'mount_error': str(mount_exc),
                            })
                        except Exception as rollback_exc:
                            log.error(
                                "[apply] GRANT ROLLBACK FAILED %s/%s -> %s "
                                "(mount_err=%s, rollback_err=%s) — MANUAL CLEANUP REQUIRED",
                                source, folder, grantee, mount_exc, rollback_exc,
                            )
                            partial_failures.append({
                                'source': source, 'folder': folder, 'grantee': grantee,
                                'mount_path': mount_path,
                                'note': 'mount failed AND rollback failed — grant exists without mount',
                                'mount_error': str(mount_exc),
                                'rollback_error': str(rollback_exc),
                            })

        # ------------------------------------------------------------------
        # Phase 2 — remove shares for keep=0 records
        # ------------------------------------------------------------------
        total_remove = len(to_remove)
        update_job(job_id, progress=52, message=f'Phase 2: removing {total_remove} flagged records...')
        log.info("[apply] Phase 2: %d records to remove", total_remove)

        for idx, record in enumerate(to_remove):
            pct = 52 + int((idx / max(total_remove, 1)) * 43)
            update_job(job_id, progress=pct, message=f'Removing {idx+1}/{total_remove}...')

            source     = record.get('source_account', '')
            folder     = record.get('source_folder', '')
            grantee    = record.get('grantee_account', '')
            mount_path = record.get('grantee_mountpoint')

            if record.get('has_grant') and not record.get('source_orphaned'):
                try:
                    zimbra_cmd_with_retry(
                        ['zmmailbox', '-z', '-m', source,
                         'modifyFolderGrant', folder, 'account', grantee, 'none'],
                        timeout=60,
                    )
                    removed_grants += 1
                    log.info("[apply] Removed grant: %s/%s -> %s", source, folder, grantee)
                except Exception as exc:
                    msg = f"Grant removal failed {source}/{folder} -> {grantee}: {exc}"
                    log.error("[apply] %s", msg)
                    write_error_log('apply', f'{source} → {grantee}', f'Grant removal failed on {folder}: {exc}', level='error')
                    error_details.append({'type': 'grant_removal', 'source': source,
                                          'folder': folder, 'grantee': grantee, 'error': str(exc)})

            if (record.get('has_mount') and mount_path
                    and not record.get('grantee_orphaned')
                    and not record.get('mount_is_inherited')):
                try:
                    zimbra_cmd_with_retry(
                        ['zmmailbox', '-z', '-m', grantee, 'deleteFolder', mount_path],
                        timeout=60,
                    )
                    removed_mounts += 1
                    log.info("[apply] Deleted mount: %s:%s", grantee, mount_path)
                except Exception as exc:
                    msg = f"Mount deletion failed {grantee}/{mount_path}: {exc}"
                    log.error("[apply] %s", msg)
                    write_error_log('apply', grantee, f'Mount deletion failed on {mount_path}: {exc}', level='error')
                    error_details.append({'type': 'mount_deletion', 'grantee': grantee,
                                          'mount_path': mount_path, 'error': str(exc)})

        summary = (
            f'Manifest state applied — '
            f'grants set: {created_grants}, mounts created: {created_mounts}'
            + (f', mounts already existed: {existing_mounts}' if existing_mounts else '')
            + f', grants removed: {removed_grants}, mounts deleted: {removed_mounts}'
            + (f', skipped: {len(skipped_details)}' if skipped_details else '')
            + (f', partial failures: {len(partial_failures)}' if partial_failures else '')
            + f', errors: {len(error_details)}.'
        )

        archived_ver = archive_manifest_version()
        new_version = archived_ver + 1 if archived_ver > 0 else 1
        manifest = inject_version_metadata(
            manifest,
            version=new_version,
            action='apply_manifest_state',
            grants_created=created_grants,
            mounts_created=created_mounts,
            grants_removed=removed_grants,
            mounts_removed=removed_mounts,
            errors=len(error_details),
            detail=summary,
        )
        write_manifest(manifest)
        log.info("[apply] Manifest updated to v%d", new_version)

        results = {
            'operation': 'apply_manifest_state',
            'manifest_version': new_version,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'manifest_source': manifest.get('collected_from'),
            'created_grants': created_grants,
            'created_mounts': created_mounts,
            'existing_mounts': existing_mounts,
            'removed_grants': removed_grants,
            'removed_mounts': removed_mounts,
            'skipped': skipped_details,
            'partial_failures': partial_failures,
            'errors': error_details,
        }
        try:
            with open(RESULTS_FILE, 'w', encoding='utf-8') as fh:
                json.dump(results, fh, indent=2)
        except Exception as exc:
            log.warning("[apply] Could not write results file: %s", exc)

        overall_status = 'ok' if not error_details and not partial_failures else 'partial'
        write_audit('apply_manifest_state', summary, status=overall_status)
        update_job(job_id, status='done', progress=100, message=summary)
        log.info("[apply] %s", summary)

    except Exception as exc:
        log.exception("[apply] Unhandled error")
        summary = f'Error: {exc}'
        write_audit('apply_manifest_state', summary, status='error')
        update_job(job_id, status='error', error=str(exc), message=summary)

    finally:
        job = get_job(job_id)
        if job and job.get('status') == 'running':
            update_job(job_id, status='error', error='Job ended unexpectedly',
                       message='Error: job ended unexpectedly')


# ---------------------------------------------------------------------------
# Background job: apply migration
# ---------------------------------------------------------------------------

def apply_migration_job(job_id: str):
    """Background thread: recreate grants and mounts for keep==1 records."""
    created_grants = 0
    created_mounts = 0
    skipped_details: list = []
    partial_failures: list = []
    error_details: list = []

    try:
        update_job(job_id, progress=5, message='Loading manifest and accounts...')
        manifest = read_manifest()
        if not manifest:
            raise RuntimeError("No manifest found")

        try:
            active_accounts = set(get_all_accounts())
        except Exception as exc:
            raise RuntimeError(f"Failed to get accounts: {exc}")

        records = manifest.get('records', [])
        to_migrate = [r for r in records if r.get('keep', 1) == 1]
        total = len(to_migrate)
        update_job(job_id, progress=10, message=f'{total} records to migrate...')
        log.info("[migration] %d records to process, %d active accounts on this server",
                 total, len(active_accounts))

        for idx, record in enumerate(to_migrate):
            pct = 10 + int((idx / max(total, 1)) * 85)
            update_job(job_id, progress=pct, message=f'Processing {idx+1}/{total}...')

            source = record.get('source_account', '')
            folder = record.get('source_folder', '')
            grantee = record.get('grantee_account', '')
            mount_path = record.get('grantee_mountpoint')
            permissions = record.get('permissions', 'r') or 'r'

            # Skip if either account is absent on this server — log detail
            missing = []
            if source not in active_accounts:
                missing.append(f'source {source!r} not on this server')
            if grantee not in active_accounts:
                missing.append(f'grantee {grantee!r} not on this server')
            if missing:
                reason = '; '.join(missing)
                log.info("[migration] Skipping %s/%s->%s: %s", source, folder, grantee, reason)
                skipped_details.append({'source': source, 'folder': folder,
                                        'grantee': grantee, 'reason': reason})
                continue

            # Create grant (with retry)
            grant_created = False
            try:
                zimbra_cmd_with_retry(
                    ['zmmailbox', '-z', '-m', source,
                     'modifyFolderGrant', folder, 'account', grantee, permissions],
                    timeout=60,
                )
                grant_created = True
                created_grants += 1
                log.info("[migration] Created grant: %s/%s -> %s (%s)", source, folder, grantee, permissions)
            except Exception as exc:
                err = f"Grant failed {source}/{folder} -> {grantee}: {exc}"
                log.error("[migration] %s", err)
                write_error_log('migration', f'{source} → {grantee}', f'Grant creation failed on {folder}: {exc}', level='error')
                error_details.append({'type': 'grant_creation', 'source': source,
                                      'folder': folder, 'grantee': grantee, 'error': str(exc)})
                continue

            # Create mount (with retry); roll back grant on failure.
            # Inherited records (subfolder grants covered by a parent mountpoint) get the grant
            # recreated but not a new mountpoint — that path already exists as a virtual child
            # of the parent mount and createMountpoint would fail with "folder already exists".
            if mount_path and grant_created and not record.get('mount_is_inherited'):
                try:
                    zimbra_cmd_with_retry(
                        ['zmmailbox', '-z', '-m', grantee,
                         'createMountpoint', mount_path, source, folder],
                        timeout=60,
                    )
                    created_mounts += 1
                    log.info("[migration] Created mount: %s:%s -> %s:%s",
                             grantee, mount_path, source, folder)
                except Exception as mount_exc:
                    log.warning(
                        "[migration] Mount failed %s:%s (attempting grant rollback): %s",
                        grantee, mount_path, mount_exc,
                    )
                    # Attempt rollback of the grant we just created
                    try:
                        zimbra_cmd(
                            ['zmmailbox', '-z', '-m', source,
                             'modifyFolderGrant', folder, 'account', grantee, 'none'],
                            timeout=30,
                        )
                        created_grants -= 1
                        log.info("[migration] Rolled back grant after mount failure: %s/%s -> %s",
                                 source, folder, grantee)
                        partial_failures.append({
                            'source': source, 'folder': folder, 'grantee': grantee,
                            'mount_path': mount_path,
                            'note': 'mount failed — grant rolled back',
                            'mount_error': str(mount_exc),
                        })
                    except Exception as rollback_exc:
                        log.error(
                            "[migration] GRANT ROLLBACK FAILED %s/%s -> %s "
                            "(mount_err=%s, rollback_err=%s) — MANUAL CLEANUP REQUIRED",
                            source, folder, grantee, mount_exc, rollback_exc,
                        )
                        partial_failures.append({
                            'source': source, 'folder': folder, 'grantee': grantee,
                            'mount_path': mount_path,
                            'note': 'mount failed AND rollback failed — grant exists without mount',
                            'mount_error': str(mount_exc),
                            'rollback_error': str(rollback_exc),
                        })

        summary = (
            f'Migration complete — grants created: {created_grants}, '
            f'mounts created: {created_mounts}, '
            f'skipped: {len(skipped_details)}, '
            f'partial failures: {len(partial_failures)}, '
            f'errors: {len(error_details)}.'
        )

        # Archive current manifest and write back with incremented version + change record
        archived_ver = archive_manifest_version()
        new_version = archived_ver + 1 if archived_ver > 0 else 1
        manifest = inject_version_metadata(
            manifest,
            version=new_version,
            action='apply_migration',
            grants_created=created_grants,
            mounts_created=created_mounts,
            skipped=len(skipped_details),
            partial_failures=len(partial_failures),
            errors=len(error_details),
            detail=summary,
        )
        write_manifest(manifest)
        log.info("[migration] Manifest updated to v%d", new_version)

        # Persist full results for review
        results = {
            'operation': 'migration',
            'manifest_version': new_version,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'manifest_source': manifest.get('collected_from'),
            'created_grants': created_grants,
            'created_mounts': created_mounts,
            'skipped': skipped_details,
            'partial_failures': partial_failures,
            'errors': error_details,
        }
        try:
            with open(RESULTS_FILE, 'w', encoding='utf-8') as fh:
                json.dump(results, fh, indent=2)
        except Exception as exc:
            log.warning("[migration] Could not write results file: %s", exc)

        overall_status = 'ok' if not error_details and not partial_failures else 'partial'
        write_audit('apply_migration', summary, status=overall_status)
        update_job(job_id, status='done', progress=100, message=summary)
        log.info("[migration] %s", summary)

    except Exception as exc:
        log.exception("[migration] Unhandled error")
        summary = f'Error: {exc}'
        write_audit('apply_migration', summary, status='error')
        update_job(job_id, status='error', error=str(exc), message=summary)

    finally:
        # Safety net: ensure job is never left permanently in 'running' state
        job = get_job(job_id)
        if job and job.get('status') == 'running':
            update_job(job_id, status='error', error='Job ended unexpectedly',
                       message='Error: job ended unexpectedly')


# ---------------------------------------------------------------------------
# Background job: delete all shares
# ---------------------------------------------------------------------------

def delete_all_shares_job(job_id: str):
    """
    Background thread: remove every outgoing grant and every mountpoint from
    all accounts on this server. The manifest is NOT read or modified — this
    purely changes Zimbra server state. Shares can be recreated afterwards via
    Apply Migration.
    """
    removed_grants = 0
    removed_mounts = 0
    error_details: list = []

    try:
        update_job(job_id, progress=2, message='Fetching all accounts...')
        accounts = get_all_accounts()
        active_set = set(accounts)
        total = len(accounts)
        log.info("[delete-all] Starting: %d accounts to scan", total)

        # gsi 'gt' value -> zmmailbox modifyFolderGrant grantee-type parameter
        _GT_TO_ZMBOX = {
            'usr': 'account', 'grp': 'group', 'dom': 'domain',
            'pub': 'public',  'cos': 'cos',   'all': 'all', '': 'account',
        }

        # Phase 1 (5-35%): collect every outgoing grant via getShareInfo.
        # Store folder_id so Phase 2.5 can resolve truncated paths via id_maps.
        all_grants: list = []
        for idx, account in enumerate(accounts):
            pct = 5 + int((idx / max(total, 1)) * 30)
            if idx % 20 == 0:
                update_job(job_id, progress=pct,
                           message=f'Scanning grants: {account} ({idx+1}/{total})')
            try:
                output = zimbra_cmd(['zmprov', 'gsi', account], timeout=60)
                shares = parse_getshareinfo(output, account, active_set)
                for share in shares:
                    all_grants.append({
                        'owner':     share['owner_email'],
                        'folder':    share['folder_path'],
                        'folder_id': share.get('folder_id', ''),
                        'grantee':   share['grantee_account'],
                        'gt':        share.get('gt', 'usr'),
                    })
            except Exception as exc:
                log.warning("[delete-all] gsi failed for %s: %s", account, exc)

        log.info("[delete-all] Found %d grants", len(all_grants))

        # Phase 2 (35-55%): collect every mountpoint via getAllFolders (two-pass)
        id_maps: dict = {}
        folder_outputs: dict = {}
        for idx, account in enumerate(accounts):
            pct = 35 + int((idx / max(total, 1)) * 12)
            if idx % 20 == 0:
                update_job(job_id, progress=pct,
                           message=f'Scanning folders: {account} ({idx+1}/{total})')
            try:
                output = zimbra_cmd(
                    ['zmmailbox', '-z', '-m', account, 'getAllFolders'], timeout=60)
                id_maps[account] = parse_getallfolders_id_map(output)
                folder_outputs[account] = output
            except Exception as exc:
                log.warning("[delete-all] getAllFolders failed for %s: %s", account, exc)
                id_maps[account] = {}
                folder_outputs[account] = ''

        all_mounts_raw: list = []
        for account, output in folder_outputs.items():
            if not output:
                continue
            try:
                mounts = parse_getallfolders_mounts(output, account, all_id_maps=id_maps)
                all_mounts_raw.extend(mounts)
            except Exception as exc:
                log.warning("[delete-all] Mount parse failed for %s: %s", account, exc)

        log.info("[delete-all] Found %d mounts", len(all_mounts_raw))

        # Phase 2.5: resolve truncated grant folder paths via id_maps.
        # gsi's id column is 5 chars wide — 6-digit folder IDs are truncated — and
        # the path column is 20 chars wide. Use the same two-stage resolution as the
        # collect job: exact id_map lookup, then prefix-match against mount remote_ids.
        _del_owner_mounts: dict = {}
        for _m in all_mounts_raw:
            _mown = _m.get('owner_account')
            _mrid = _m.get('remote_id')
            _mrfp = _m.get('remote_folder')
            if _mown and _mrid and _mrfp:
                _del_owner_mounts.setdefault(_mown, []).append((_mrid, _mrfp))

        for grant in all_grants:
            fid = grant.get('folder_id', '')
            if not fid or fid == '0':
                continue
            owner = grant['owner']
            resolved = id_maps.get(owner, {}).get(str(fid))
            if not resolved and len(str(fid)) == 5:
                pfx = str(fid)
                cands = list(dict.fromkeys(
                    rfp for rid, rfp in _del_owner_mounts.get(owner, [])
                    if str(rid).startswith(pfx) and len(str(rid)) > 5
                ))
                if len(cands) == 1:
                    resolved = cands[0]
            if resolved:
                grant['folder'] = resolved

        log.info("[delete-all] Grant path resolution complete")

        # Phase 3 (55-78%): remove all grants using resolved paths and correct grantee type
        grant_total = len(all_grants)
        update_job(job_id, progress=55, message=f'Removing {grant_total} grants...')
        for idx, grant in enumerate(all_grants):
            if idx % 50 == 0:
                pct = 55 + int((idx / max(grant_total, 1)) * 23)
                update_job(job_id, progress=pct,
                           message=f'Removing grants: {idx+1}/{grant_total}...')
            zmbox_type = _GT_TO_ZMBOX.get(grant.get('gt', ''), 'account')
            try:
                zimbra_cmd_with_retry(
                    ['zmmailbox', '-z', '-m', grant['owner'],
                     'modifyFolderGrant', grant['folder'], zmbox_type, grant['grantee'], 'none'],
                    timeout=60,
                )
                removed_grants += 1
            except Exception as exc:
                log.error("[delete-all] Grant removal failed %s/%s->%s: %s",
                          grant['owner'], grant['folder'], grant['grantee'], exc)
                write_error_log('delete_all', f"{grant['owner']} → {grant['grantee']}", f"Grant removal failed on {grant['folder']}: {exc}", level='error')
                error_details.append({
                    'type': 'grant_removal', 'owner': grant['owner'],
                    'folder': grant['folder'], 'grantee': grant['grantee'], 'error': str(exc),
                })

        # Phase 4 (78-98%): delete all mounts
        mount_total = len(all_mounts_raw)
        update_job(job_id, progress=78, message=f'Deleting {mount_total} mounts...')
        for idx, mount in enumerate(all_mounts_raw):
            if idx % 50 == 0:
                pct = 78 + int((idx / max(mount_total, 1)) * 20)
                update_job(job_id, progress=pct,
                           message=f'Deleting mounts: {idx+1}/{mount_total}...')
            try:
                zimbra_cmd_with_retry(
                    ['zmmailbox', '-z', '-m', mount['grantee_account'],
                     'deleteFolder', mount['mount_path']],
                    timeout=60,
                )
                removed_mounts += 1
            except Exception as exc:
                log.error("[delete-all] Mount deletion failed %s:%s: %s",
                          mount['grantee_account'], mount['mount_path'], exc)
                write_error_log('delete_all', mount['grantee_account'], f"Mount deletion failed on {mount['mount_path']}: {exc}", level='error')
                error_details.append({
                    'type': 'mount_deletion', 'grantee': mount['grantee_account'],
                    'mount_path': mount['mount_path'], 'error': str(exc),
                })

        summary = (
            f'Delete all shares complete — grants removed: {removed_grants}, '
            f'mounts deleted: {removed_mounts}, errors: {len(error_details)}.'
        )
        results = {
            'operation': 'delete_all_shares',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'removed_grants': removed_grants,
            'removed_mounts': removed_mounts,
            'errors': error_details,
        }
        try:
            with open(RESULTS_FILE, 'w', encoding='utf-8') as fh:
                json.dump(results, fh, indent=2)
        except Exception as exc:
            log.warning("[delete-all] Could not write results file: %s", exc)

        write_audit('delete_all_shares', summary,
                    status='ok' if not error_details else 'partial')
        update_job(job_id, status='done', progress=100, message=summary)
        log.info("[delete-all] %s", summary)

    except Exception as exc:
        log.exception("[delete-all] Unhandled error")
        summary = f'Error: {exc}'
        write_audit('delete_all_shares', summary, status='error')
        update_job(job_id, status='error', error=str(exc), message=summary)

    finally:
        job = get_job(job_id)
        if job and job.get('status') == 'running':
            update_job(job_id, status='error', error='Job ended unexpectedly',
                       message='Error: job ended unexpectedly')


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/info')
def api_info():
    return jsonify(get_server_info())


@app.route('/api/manifest')
def api_manifest():
    manifest = read_manifest()
    if not manifest:
        return jsonify({'exists': False, 'records': []})
    result = dict(manifest)
    result['exists'] = True
    return jsonify(result)


@app.route('/api/collect', methods=['POST'])
def api_collect():
    body = request.get_json(silent=True) or {}
    force = bool(body.get('force', False))

    if not force and SHARES_FILE.exists():
        manifest = read_manifest()
        record_count = len(manifest.get('records', [])) if manifest else 0
        return jsonify({'exists': True, 'record_count': record_count}), 409

    job_id = create_job('collect')
    t = threading.Thread(target=collect_shares_job, args=(job_id,), daemon=True)
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/api/manifest/delete', methods=['POST'])
def api_manifest_delete():
    if SHARES_FILE.exists():
        SHARES_FILE.unlink()
        log.info("Manifest deleted")
    return jsonify({'success': True})


@app.route('/api/shares/flag', methods=['POST'])
def api_shares_flag():
    body = request.get_json(silent=True) or {}
    record_id = body.get('id')
    keep = body.get('keep')

    if record_id is None or keep is None:
        return jsonify({'error': 'Missing id or keep'}), 400

    manifest = read_manifest()
    if not manifest:
        return jsonify({'error': 'No manifest'}), 404

    records = manifest.get('records', [])
    for record in records:
        if record.get('id') == record_id:
            record['keep'] = int(keep)
            # Cascade keep/remove to inherited subfolder grants so they stay in sync
            if not record.get('mount_is_inherited'):
                source = record.get('source_account')
                grantee = record.get('grantee_account')
                folder_prefix = (record.get('source_folder') or '').rstrip('/') + '/'
                for other in records:
                    if (other.get('mount_is_inherited') and
                            other.get('source_account') == source and
                            other.get('grantee_account') == grantee and
                            (other.get('source_folder') or '').startswith(folder_prefix)):
                        other['keep'] = int(keep)
            write_manifest(manifest)
            return jsonify({'success': True})

    return jsonify({'error': 'Record not found'}), 404


@app.route('/api/shares/flag-all-orphaned', methods=['POST'])
def api_flag_all_orphaned():
    manifest = read_manifest()
    if not manifest:
        return jsonify({'error': 'No manifest'}), 404
    records = manifest.get('records', [])
    flagged = 0
    for record in records:
        has_grant = record.get('has_grant', False)
        has_mount = record.get('has_mount', False)
        if (has_grant and not has_mount) or (not has_grant and has_mount):
            record['keep'] = 0
            flagged += 1
    write_manifest(manifest)
    return jsonify({'success': True, 'flagged': flagged})


@app.route('/api/job/<job_id>')
def api_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


@app.route('/api/export')
def api_export():
    if not SHARES_FILE.exists():
        return jsonify({'error': 'No manifest to export'}), 404
    return _send_attachment(str(SHARES_FILE), 'shares.json')


@app.route('/api/import', methods=['POST'])
def api_import():
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': 'No file provided'}), 400

    file = request.files['file']
    try:
        content = file.read().decode('utf-8')
        data = json.loads(content)
    except Exception as exc:
        return jsonify({'success': False, 'message': f'Invalid JSON: {exc}'}), 400

    if 'records' not in data:
        return jsonify({'success': False, 'message': 'Missing "records" key in JSON'}), 400

    # Ensure all records have a keep field
    for record in data['records']:
        if 'keep' not in record:
            record['keep'] = 1

    write_manifest(data)
    record_count = len(data['records'])
    log.info("Manifest imported: %d records", record_count)
    return jsonify({
        'success': True,
        'record_count': record_count,
        'message': f'Imported {record_count} records successfully.',
    })


@app.route('/api/apply/cleanup', methods=['POST'])
def api_apply_cleanup():
    job_id = create_job('cleanup')
    t = threading.Thread(target=apply_cleanup_job, args=(job_id,), daemon=True)
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/api/apply/migration', methods=['POST'])
def api_apply_migration():
    job_id = create_job('migration')
    t = threading.Thread(target=apply_migration_job, args=(job_id,), daemon=True)
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/api/apply/delete-all-shares', methods=['POST'])
def api_apply_delete_all_shares():
    job_id = create_job('delete_all_shares')
    t = threading.Thread(target=delete_all_shares_job, args=(job_id,), daemon=True)
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/api/manifest/versions')
def api_manifest_versions():
    """List all available manifest versions (current + archived shares_vN.json files)."""
    versions = []

    # Current shares.json
    if SHARES_FILE.exists():
        m = read_manifest()
        versions.append({
            'filename': 'shares.json',
            'version': m.get('_version') if m else None,
            'last_action': m.get('_last_action') if m else None,
            'last_updated': m.get('_last_updated') if m else None,
            'record_count': len(m.get('records', [])) if m else 0,
            'current': True,
        })

    # Archived shares_vN.json files
    archive_files = sorted(
        DATA_DIR.glob('shares_v*.json'),
        key=lambda p: int(p.stem.split('_v')[1]) if p.stem.split('_v')[1].isdigit() else 0,
        reverse=True,
    )
    for path in archive_files:
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                m = json.load(fh)
            versions.append({
                'filename': path.name,
                'version': m.get('_version'),
                'last_action': m.get('_last_action'),
                'last_updated': m.get('_last_updated'),
                'record_count': len(m.get('records', [])),
                'current': False,
            })
        except Exception as exc:
            versions.append({'filename': path.name, 'error': str(exc), 'current': False})

    return jsonify({'versions': versions})


@app.route('/api/manifest/download/<filename>')
def api_manifest_download(filename: str):
    """Download a specific archived manifest version (shares_vN.json)."""
    # Validate: only allow shares_vN.json filenames to prevent path traversal
    import re as _re
    if not _re.fullmatch(r'shares_v\d+\.json', filename):
        return jsonify({'error': 'Invalid filename'}), 400
    path = DATA_DIR / filename
    if not path.exists():
        return jsonify({'error': 'File not found'}), 404
    return _send_attachment(str(path), filename)


@app.route('/api/apply/results')
def api_apply_results():
    """Return the results of the most recent apply operation."""
    if not RESULTS_FILE.exists():
        return jsonify({'exists': False})
    try:
        with open(RESULTS_FILE, 'r', encoding='utf-8') as fh:
            return jsonify(json.load(fh))
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/audit/log')
def api_audit_log():
    """Return the most recent audit log entries (newest first, max 100)."""
    if not AUDIT_FILE.exists():
        return jsonify({'entries': []})
    try:
        with open(AUDIT_FILE, 'r', encoding='utf-8') as fh:
            lines = fh.readlines()
        entries = []
        for line in lines:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return jsonify({'entries': list(reversed(entries[-100:]))})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/errors/log')
def api_errors_log():
    """Return the most recent errors.log entries (newest first, max 200)."""
    if not ERRORS_FILE.exists():
        return jsonify({'entries': []})
    try:
        with open(ERRORS_FILE, 'r', encoding='utf-8') as fh:
            lines = fh.readlines()
        entries = []
        for line in lines:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return jsonify({'entries': list(reversed(entries[-200:]))})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/errors/clear', methods=['POST'])
def api_errors_clear():
    """Truncate errors.log."""
    try:
        with _errors_lock:
            with open(ERRORS_FILE, 'w', encoding='utf-8') as fh:
                fh.truncate(0)
        log.info("errors.log cleared")
        return jsonify({'success': True})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys
    log.info("Zimbra Share Manager starting — Python %s, Flask %s", sys.version.split()[0], flask.__version__)
    log.info("Listening on http://0.0.0.0:%d  —  open http://<server-ip>:%d in your browser", PORT, PORT)
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
