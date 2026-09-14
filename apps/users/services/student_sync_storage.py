"""Private, worker-shared temporary previews. Tokens contain metadata, never rows."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from contextlib import contextmanager

from django.conf import settings
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.utils.crypto import salted_hmac

logger = logging.getLogger("apps.users.student_sync")
SALT = "student-data-sync-v1"
MAX_AGE = 1800
FILE_ID = re.compile(r"[0-9a-f]{64}\Z")


def storage_directory():
    app_id = hashlib.sha256(str(settings.BASE_DIR).encode()).hexdigest()[:16]
    directory = Path(getattr(settings, "STUDENT_SYNC_TEMP_DIR", Path(tempfile.gettempdir()) / f"student-sync-{app_id}"))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise OSError("Student sync storage must be a private directory, not a symlink.")
    # POSIX mode protects the data from other local users. Windows uses the
    # configured directory's ACL (the default is the service user's temp folder).
    if os.name == "posix" and (directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077):
        raise OSError("Student sync storage must be owned by the service user with mode 0700.")
    return directory


def _session_digest(session_key):
    return salted_hmac(SALT, session_key or "").hexdigest()


def _path(identifier, suffix=".json"):
    if not isinstance(identifier, str) or not FILE_ID.fullmatch(identifier):
        raise signing.BadSignature("Invalid preview identifier.")
    return storage_directory() / (identifier + suffix)


def create_preview(payload, actor, session_key):
    cleanup_expired()
    identifier = secrets.token_hex(32)
    created = time.time()
    data = json.dumps({"created": created, "payload": payload}, separators=(",", ":")).encode("utf-8")
    path = _path(identifier)
    # Exclusive creation, private permissions, and no client-supplied paths.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {"id": identifier, "actor": actor, "session": _session_digest(session_key),
            "created": created, "digest": hashlib.sha256(data).hexdigest()}


def preview_token(metadata, options):
    return signing.dumps({**metadata, "options": options}, salt=SALT)


def decode_token(token, actor, session_key):
    metadata = signing.loads(token, salt=SALT, max_age=MAX_AGE)
    if not isinstance(metadata, dict):
        raise signing.BadSignature("Invalid preview metadata.")
    if metadata.get("actor") != actor or metadata.get("session") != _session_digest(session_key):
        raise PermissionDenied
    _path(metadata.get("id"))
    created = metadata.get("created")
    if not isinstance(created, (int, float)) or not 0 <= time.time() - created <= MAX_AGE:
        raise signing.SignatureExpired("Preview expired.")
    return metadata


def read_preview(metadata, path=None):
    path = path or _path(metadata["id"])
    try:
        if path.is_symlink():
            raise signing.BadSignature("Invalid preview file.")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read()
        if hashlib.sha256(data).hexdigest() != metadata.get("digest"):
            raise signing.BadSignature("Preview data changed.")
        envelope = json.loads(data)
        if envelope["created"] != metadata["created"] or time.time() - envelope["created"] > MAX_AGE:
            raise signing.SignatureExpired("Preview expired.")
        return envelope["payload"]
    except (FileNotFoundError, ValueError, KeyError) as exc:
        raise signing.BadSignature("Preview expired, already applied, or unavailable.") from exc


@contextmanager
def claim_preview(metadata):
    """Atomic rename serializes use across workers; failed applies can be retried."""
    source = _path(metadata["id"])
    claimed = _path(metadata["id"], ".applying")
    try:
        source.rename(claimed)
    except FileNotFoundError as exc:
        raise signing.BadSignature("Preview already claimed or unavailable.") from exc
    try:
        yield read_preview(metadata, claimed)
    except BaseException:
        claimed.rename(source)
        raise
    else:
        try:
            claimed.unlink()
        except OSError:
            # A committed sync must not be reported as rolled back if disk cleanup
            # fails. Keep the claim (not reusable) for the stale-file cleanup job.
            logger.exception("Student sync committed; temporary claim needs cleanup id=%s", metadata["id"])


def cleanup_expired():
    """Nonrecursive cleanup of only our expired random-ID files; no symlink traversal."""
    removed = 0
    now = time.time()
    for path in storage_directory().iterdir():
        if path.suffix not in {".json", ".applying"} or not FILE_ID.fullmatch(path.stem) or path.is_symlink():
            continue
        # An abandoned applying claim gets an extra day so cleanup cannot interfere
        # with a normal in-flight sync. Its token still expires at 30 minutes.
        lifetime = MAX_AGE + (86400 if path.suffix == ".applying" else 0)
        try:
            if path.is_file() and now - path.stat().st_mtime > lifetime:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            pass  # Another worker claimed or cleaned it concurrently.
    return removed
