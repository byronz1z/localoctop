"""Out-of-band file transfer between the user's machine and the cloud
(experiment branch).

Why this exists: an MCP tool result is fed to the model verbatim, so the
bytes of a transferred file — binary or large ones in particular — must not
travel inside one. Here the MCP tool only *starts* a transfer and reports
where the file ended up; the bytes move over plain HTTP between the bridge
and this adapter. (Text files are different: `read_file` returns them to the
model line-bounded and capped, by design.)

    upload   (local -> cloud)   bridge PUTs fixed-size chunks, each one is
                                acknowledged; a dropped connection resumes
                                from the last acknowledged offset.
    download (cloud -> local)   bridge GETs with a Range header and resumes
                                the same way.

Both directions are verified with SHA-256 before the file is put in place
(written to a temporary name first, then renamed), so a half-transferred
file is never visible under its real name.

Landing layout (Octop per-user folder model): every user has exactly one
folder on the cloud machine, {LOCALOCTOP_USERS_DIR}/<user_id>/, and that
user's agent sees that folder as "/". The user_id of the token IS the folder
name, verbatim (Chinese names included). Uploads land at

    {LOCALOCTOP_USERS_DIR}/<user_id>/{LOCALOCTOP_LANDING_SUBDIR}/<rel>

and the agent sees them as "/{LOCALOCTOP_LANDING_SUBDIR}/<rel>". Downloads
can only read paths inside the user's own folder — the folder of the token
owner, nobody else's. The user folder must already exist (it belongs to the
Octop platform); this module never creates it.

Threat model (why every path operation goes through a dir_fd): the user's
own AI can create, delete, rename and symlink anything inside its folder at
any moment, including between our checks and our writes. A path we verified
a millisecond ago can point somewhere else now. So every path operation this
module performs inside a user folder is anchored to directory handles that
were opened without following links (os.open with O_DIRECTORY | O_NOFOLLOW,
one component at a time from the user root), and every create / read /
rename / unlink happens relative to such a handle (dir_fd=…). Once we hold
the handle of the landing directory, swapping names around it cannot move
our reads or writes anywhere else.

Upload temp files are anonymous (O_TMPFILE): they have no name at all, so
nothing the user does to their folder can touch them. On completion the fd
is given its final name directly (a /proc/self/fd linkat relative to the
verified landing directory handle), so the process never leaves a visible
intermediate name behind. A name that is already taken is moved aside first
— atomically and never by overwriting (renameat2 with RENAME_NOREPLACE) —
so cloud-side work under the landing name is never destroyed.
"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat as statmod
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .errors import (
    BridgeProtocolError,
    CODE_INVALID_PARAMS,
    CODE_NOT_ALLOWED,
    CODE_NOT_FOUND,
    CODE_TOO_LARGE,
)
from .tokens import KIND_BRIDGE

logger = logging.getLogger("localoctop.transfers")

CHUNK_BYTES = 4 * 1024 * 1024
MAX_CHUNK_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_TRANSFER_BYTES = 1024 * 1024 * 1024  # 1024 MiB
# Keep this much disk free after a transfer lands, so one big file cannot
# fill the volume the rest of the platform lives on.
DISK_RESERVE_BYTES = 1024 * 1024 * 1024
# A transfer with no byte movement for this long is declared dead (the
# machine went offline / the bridge was closed).
STALL_SECONDS = float(os.environ.get("LOCALOCTOP_TRANSFER_STALL_SECONDS", 420))
# How often the background task scans for stalled transfers and expired
# records. The scan also runs on transfer_status polls; this task is what
# releases the handles of a transfer nobody ever asks about again.
SCAN_INTERVAL_SECONDS = float(os.environ.get("LOCALOCTOP_TRANSFER_SCAN_INTERVAL", 30))
# Finished transfer records are kept this long for transfer_status lookups.
RECORD_TTL_SECONDS = 3600.0

_INDEX_NAME = ".localoctop-index.json"
_INDEX_TMP_PREFIX = ".localoctop-index.json.tmp-"

# A user name (== the token's user_id == the Octop folder name) must not
# contain anything that could break the one-folder-per-user confinement.
# Includes C0, DEL and the C1 block (U+0080-U+009F).
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

_O_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_dir(parent_fd: int, name: str) -> int:
    """Open one directory component relative to a parent handle without
    following links. A symlink (or anything that is not a directory) raises
    NotADirectoryError; a missing name raises FileNotFoundError."""
    return os.open(name, _O_DIR_FLAGS, dir_fd=parent_fd)


def _check_user_name(user_id: str) -> str:
    """Validate that user_id is safe to use as a folder name, verbatim.

    Allowed: any non-blank name without path separators, control characters,
    or a leading "." (which would collide with hidden directories such as
    the per-user index) — Chinese names such as '马威' are fine and kept
    as-is.
    """
    if not user_id or not user_id.strip():
        raise BridgeProtocolError(
            CODE_INVALID_PARAMS,
            "the token's user name is empty or all whitespace; it cannot be used "
            "as a folder name on the cloud machine")
    if user_id in (".", ".."):
        raise BridgeProtocolError(
            CODE_INVALID_PARAMS,
            f"the token's user name {user_id!r} is not a valid folder name "
            "(it would escape the per-user folder)")
    if user_id.startswith("."):
        raise BridgeProtocolError(
            CODE_INVALID_PARAMS,
            f"the token's user name {user_id!r} starts with a dot; "
            "a user name must not look like a hidden directory")
    if "/" in user_id or "\\" in user_id:
        raise BridgeProtocolError(
            CODE_INVALID_PARAMS,
            f"the token's user name {user_id!r} contains a path separator; "
            "a user name must be a single folder name")
    if _CTRL.search(user_id):
        raise BridgeProtocolError(
            CODE_INVALID_PARAMS,
            f"the token's user name {user_id!r} contains control characters; "
            "a user name must be plain text")
    return user_id


def _stat_name(dir_fd: int, name: str) -> os.stat_result | None:
    """lstat of one name relative to a dir handle; None when absent."""
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return None


class _NotAFolder(Exception):
    """A path component that must be a directory is a symlink or file."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name

    def refuse(self) -> BridgeProtocolError:
        # A symlink (or a file) sits where a directory must be: following it
        # could land outside the user's folder.
        return BridgeProtocolError(
            CODE_NOT_ALLOWED,
            f"the landing path {self.name!r} is a link or not a folder; "
            "the transfer was not started (it could point outside "
            "your own folder). Remove it and try again.")


class _Dirs:
    """A chain of verified directory handles for one user.

    `_Dirs(root_fd, comps)` opens each component of comps below root_fd with
    O_NOFOLLOW; `enter(name, create=…)` descends one more verified component
    as a new chain that shares the parent's fds. Once a handle is open it
    keeps naming the directory it was opened on — nothing the user swaps in
    their folder afterwards can redirect our operations.
    """

    def __init__(self, root_fd: int, comps: list[str], create: bool = False):
        self._fds = [root_fd]
        self.path: list[str] = []
        try:
            self._open(comps, create)
        except BaseException:
            # Construction failed: close only the fds this chain opened
            # itself; root_fd stays with the caller, which owns it.
            for fd in self._fds[1:]:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._fds = []
            raise

    @classmethod
    def _child(cls, parent: _Dirs, name: str, create: bool) -> _Dirs:
        child = cls.__new__(cls)
        child._fds = list(parent._fds)
        child.path = parent.path + [name]
        try:
            child._open([name], create)
        except BaseException:
            # Only the fd this level added is ours to close; the shared
            # parent fds stay with the parent chain.
            for fd in child._fds[len(parent._fds):]:
                try:
                    os.close(fd)
                except OSError:
                    pass
            child._fds = []
            raise
        return child

    def _open(self, names: list[str], create: bool) -> None:
        for name in names:
            try:
                fd = _open_dir(self._fds[-1], name)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(name, 0o700, dir_fd=self._fds[-1])
                fd = _open_dir(self._fds[-1], name)
            except NotADirectoryError:
                raise _NotAFolder(name) from None
            self._fds.append(fd)
            self.path.append(name)

    def enter(self, name: str, create: bool = False) -> _Dirs:
        """A chain one directory deeper (shares the parent's fds; close only
        the deepest chain you keep)."""
        return _Dirs._child(self, name, create)

    @property
    def fd(self) -> int:
        return self._fds[-1]

    def close(self) -> None:
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds = []


def _index_tmp_name() -> str:
    # Unpredictable per write: the user cannot pre-place a link at the name,
    # and O_EXCL would refuse to use it anyway.
    return f"{_INDEX_TMP_PREFIX}{secrets.token_urlsafe(12)}"


# ---------------------------------------------------------------- renameat2
# The Python standard library has no renameat2, so call libc directly.
# RENAME_NOREPLACE makes the rename fail with EEXIST when the destination
# name is taken — the only rename that can never overwrite someone's work.
_RENAME_NOREPLACE = 1

try:
    _libc = ctypes.CDLL("libc.so.6", use_errno=True)
    _libc.renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p,
                                ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    _libc.renameat2.restype = ctypes.c_int
except (OSError, AttributeError):  # not glibc: the fallback path is used
    _libc = None


def _rename_noreplace(dir_fd: int, old: str, new: str) -> None:
    """Atomically rename `old` to `new` relative to `dir_fd`, failing with
    FileExistsError instead of overwriting when `new` is already taken.

    Falls back to link+unlink when the kernel or libc has no renameat2;
    the fallback leaves a sub-microsecond window between link and unlink
    during which the file sits under both names (a concurrent swap of the
    source name would make us rename whatever the name holds at link time —
    still never an overwrite of `new`)."""
    old_b, new_b = os.fsencode(old), os.fsencode(new)
    if _libc is not None:
        rc = _libc.renameat2(dir_fd, old_b, dir_fd, new_b, _RENAME_NOREPLACE)
        if rc == 0:
            return
        err = ctypes.get_errno()
        if err not in (errno.ENOSYS, errno.EINVAL):
            raise OSError(err, os.strerror(err), old)
        # renameat2 unsupported at runtime: use the fallback below.
    # Fallback: link(new) refuses EEXIST, so `new` is never overwritten;
    # unlink(old) then removes the source name.
    os.link(old, new_b, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    os.unlink(old_b, dir_fd=dir_fd)


@dataclass
class Transfer:
    id: str
    ticket: str
    user_id: str
    pair_id: str
    direction: str            # "up" (local->cloud) | "down" (cloud->local)
    local_path: str
    cloud_path: Path
    size: int = 0
    mtime_unix: int = 0
    received: int = 0         # up: bytes acknowledged; down: highest byte served
    inflight: int = 0         # up: bytes of the chunk currently arriving (not yet applied)
    sha: Any = None
    expected_sha: str = ""
    state: str = "active"     # active | done | failed
    error: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    created: float = field(default_factory=time.monotonic)
    updated: float = field(default_factory=time.monotonic)
    finished: float = 0.0
    done: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    dirs: _Dirs | None = None       # verified dir handles of the landing folder
    file_fd: int = -1               # up: anonymous temp fd; down: verified source fd

    def touch(self) -> None:
        self.updated = time.monotonic()

    def finish(self, ok: bool, error: str = "") -> None:
        if self.state != "active":
            return
        self.state = "done" if ok else "failed"
        self.error = error
        self.finished = time.monotonic()
        # Anonymous temp data dies with its fd; the temp name was never
        # visible at all, so there is nothing to sweep. Handles are never
        # left behind — this runs on success, failure, abort and timeout.
        if self.file_fd >= 0:
            try:
                os.close(self.file_fd)
            except OSError:
                pass
            self.file_fd = -1
        if self.dirs is not None:
            self.dirs.close()
            self.dirs = None
        self.done.set()


class TransferManager:
    def __init__(self, settings: Any, registry: Any, url_base: str):
        self.settings = settings
        self.registry = registry
        self.url_base = url_base.rstrip("/")
        e = os.environ
        # Mount point of all per-user Octop folders: {users_dir}/<user_id>/.
        self.users_dir = Path(e.get("LOCALOCTOP_USERS_DIR", "/users"))
        # Sub-folder inside each user's folder where fetched files land (the
        # agent sees it as "/{landing_subdir}").
        self.landing_subdir = e.get("LOCALOCTOP_LANDING_SUBDIR", "localoctop").strip("/") or "localoctop"
        self.max_bytes = int(e.get("LOCALOCTOP_MAX_TRANSFER_BYTES", DEFAULT_MAX_TRANSFER_BYTES))
        self._transfers: dict[str, Transfer] = {}
        self._index_lock = asyncio.Lock()
        self._scan_task: asyncio.Task | None = None

    # ------------------------------------------------------------ helpers
    def _user_root_fd(self, user_id: str) -> int:
        """A dir handle to the user's Octop folder, opened without following
        links (a symlink where the folder should be is refused — following
        it would smuggle one user's traffic into another's folder).

        The folder must already exist (it belongs to the platform); we never
        create it: an account without a folder simply cannot transfer."""
        name = _check_user_name(user_id)
        try:
            top_fd = os.open(self.users_dir, _O_DIR_FLAGS)
        except OSError:
            raise BridgeProtocolError(
                CODE_NOT_FOUND,
                f"this account has no folder on the cloud machine yet "
                f"(no Octop user named {user_id!r}); the transfer was not started. "
                "Create the Octop user first, then try again.")
        try:
            return _open_dir(top_fd, name)
        except NotADirectoryError:
            raise BridgeProtocolError(
                CODE_NOT_ALLOWED,
                f"the folder for user {user_id!r} on the cloud machine is a link, "
                "not a real folder; refusing to transfer (platform misconfiguration)")
        except OSError:
            raise BridgeProtocolError(
                CODE_NOT_FOUND,
                f"this account has no folder on the cloud machine yet "
                f"(no Octop user named {user_id!r}); the transfer was not started. "
                "Create the Octop user first, then try again.")
        finally:
            os.close(top_fd)

    def _dest_for(self, user_id: str, rel: str, name: str) -> tuple[list[str], PurePosixPath]:
        """Cloud landing location for a local file (stable per local path).

        Returns (components below the user root, path the agent sees). No
        disk access happens here — components are opened through _Dirs when
        the transfer actually starts."""
        _check_user_name(user_id)
        rel = rel.replace("\\", "/")
        parts = [p for p in PurePosixPath(rel).parts if p not in ("", ".", "..", "/")]
        # An absolute local path (file outside every whitelist root should
        # never reach here, but be defensive): keep only the file name.
        if not parts or re.match(r"^[A-Za-z]:", parts[0]) or rel.startswith("/"):
            parts = [name or "file"]
        clean = [re.sub(r'[<>:"|?*\x00-\x1f]', "_", p) for p in parts]
        visible = PurePosixPath("/") / self.landing_subdir / PurePosixPath(*clean)
        return [self.landing_subdir, *clean], visible

    def _index_load(self, dirs: _Dirs) -> dict[str, Any]:
        """Read the per-user index through the verified landing dir handle.
        An index replaced by a symlink (or anything but a readable regular
        file) is treated as "no index" — we must never follow it."""
        try:
            fd = os.open(_INDEX_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=dirs.fd)
        except OSError:
            return {}
        try:
            with os.fdopen(fd, "rb") as f:
                return json.loads(f.read().decode("utf-8"))
        except (OSError, ValueError):
            return {}

    def _index_save(self, dirs: _Dirs, idx: dict[str, Any]) -> None:
        """Replace the index atomically relative to the verified dir handle.
        The temp name is random and created O_EXCL: a link the user planted
        at the index's name is swapped away by the rename, never written
        through (rename never follows a symlink at the destination)."""
        data = json.dumps(idx, ensure_ascii=False).encode("utf-8")
        tmp = _index_tmp_name()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=dirs.fd)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            # Write/fsync failed: fdopen's close on the error path is not
            # guaranteed, so close the fd explicitly before unlinking.
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp, dir_fd=dirs.fd)
            except OSError:
                pass
            raise
        try:
            os.rename(tmp, _INDEX_NAME, src_dir_fd=dirs.fd, dst_dir_fd=dirs.fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dirs.fd)
            except OSError:
                pass
            raise

    def _keep_cloud_edited(self, dirs: _Dirs, name: str) -> str:
        """Move `name` (a regular file at the landing position that is NOT
        the copy we landed last time — the agent edited it, or we have no
        record of it) aside to "<stem>.cloud-edited-<YYYYMMDD-HHMMSS><ext>"
        so a re-fetch never destroys cloud-side work.

        Everything happens relative to the verified dir handle and the move
        uses renameat2(RENAME_NOREPLACE): it either happens in one atomic
        step or fails with EEXIST — there is no check-then-act window for
        the agent to slip into, and nothing at the keep-as name is ever
        overwritten (a taken candidate gets a numeric suffix).
        FileNotFoundError propagates: the occupant was moved away by someone
        else first, and the caller retries the landing link. Returns the
        kept-as name."""
        stem, dot, ext = name.rpartition(".")
        if not dot:  # no extension, or a dotfile like ".env"
            stem, ext = name, ""
        else:
            ext = "." + ext
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        base = f"{stem}.cloud-edited-{stamp}"
        for i in range(0, 1000):
            cand = f"{base}{'' if i == 0 else f'-{i}'}{ext}"
            try:
                _rename_noreplace(dirs.fd, name, cand)
            except FileExistsError:
                continue
            logger.info("cloud copy of %s was modified; kept as %s", name, cand)
            return cand
        raise OSError(f"no free keep-as name for the modified cloud copy of {name!r}")

    def _gc(self) -> None:
        now = time.monotonic()
        for tid, t in list(self._transfers.items()):
            if t.state != "active" and now - t.finished > RECORD_TTL_SECONDS:
                self._transfers.pop(tid, None)

    def _check_stall(self, t: Transfer) -> None:
        if t.state == "active" and time.monotonic() - t.updated > STALL_SECONDS:
            self._abort(t, "the transfer stopped making progress — the local machine went offline "
                           "or the localoctop client was closed. Start it again when the machine is back.")

    def sweep_transfers(self) -> None:
        """One pass over every transfer record: stall-abort actives that
        stopped making progress (their handles are closed by finish()) and
        drop finished records past the retention TTL.

        This is called by the background scan task AND directly by tests —
        a transfer nobody ever asks about again must not hold its file and
        directory handles forever."""
        for t in list(self._transfers.values()):
            self._check_stall(t)
        self._gc()

    def _ensure_scan_task(self) -> None:
        """Start the periodic sweep if it is not running yet. Lazy so the
        manager also works where no lifespan event ever fires (tests, and
        ASGI transports that skip startup)."""
        task = self._scan_task
        if task is not None and not task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop yet (sync context); retried on next _new
        self._scan_task = asyncio.create_task(self._scan_loop())

    async def _scan_loop(self) -> None:
        while True:
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)
            try:
                self.sweep_transfers()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("transfer scan failed; retrying next interval")

    async def aclose(self) -> None:
        """Stop the background scan (called from the app lifespan on
        shutdown). Transfer records themselves live until their TTL."""
        task = self._scan_task
        self._scan_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _abort(self, t: Transfer, msg: str) -> None:
        t.finish(False, msg)

    def _new(self, **kw: Any) -> Transfer:
        self._gc()
        t = Transfer(id=secrets.token_urlsafe(9), ticket=secrets.token_urlsafe(24), **kw)
        self._transfers[t.id] = t
        self._ensure_scan_task()
        return t

    def _advisory(self, user_id: str, pair_id: str, direction: str, local_path: str,
                  visible: str, size: int, message: str) -> Transfer:
        """A finished, not-transferred record for a call that hit a limit.
        Hitting a limit is a normal answer with a way forward, not a failure:
        tools.py renders it as isError=false (result['advisory'])."""
        t = self._new(user_id=user_id, pair_id=pair_id, direction=direction,
                      local_path=local_path, cloud_path=Path(visible or local_path), size=size)
        t.result = {"advisory": True, "visible": visible}
        t.finish(False, message)
        return t

    def _bridge_params(self, t: Transfer) -> dict[str, Any]:
        return {"transfer_id": t.id, "ticket": t.ticket,
                "url_path": f"{self.url_base}/{t.id}", "chunk_bytes": CHUNK_BYTES}

    async def _call(self, user_id: str, pair_id: str, method: str, params: dict[str, Any]) -> Any:
        if pair_id:
            return await self.registry.call_paired(user_id, pair_id, method, params,
                                                   timeout=self.settings.bridge_timeout)
        return await self.registry.call(user_id, method, params, timeout=self.settings.bridge_timeout)

    def _open_agent_file(self, user_id: str, raw: str) -> tuple[int, _Dirs, os.stat_result]:
        """Map an agent-visible absolute path ("/..." inside the user's
        folder) to an open file, confined to the user's own folder.

        Every directory component is opened relative to its parent handle
        with O_NOFOLLOW, and the file itself with O_NOFOLLOW: a symlink at
        any step — planted before or during this call — is refused, never
        followed. Returns (file fd, open dir chain, fstat of the file). The
        caller owns the fd and the chain; because both are held, later reads
        keep seeing this exact file no matter how the folder is rearranged.
        """
        if not raw or "\x00" in raw:
            raise BridgeProtocolError(CODE_INVALID_PARAMS, "cloud_path is required")
        pp = PurePosixPath(raw)
        if not pp.is_absolute():
            raise BridgeProtocolError(
                CODE_INVALID_PARAMS,
                f"cloud_path must be an absolute path the agent sees, starting with '/' "
                f"(your folder is the root), got {raw!r}")
        parts = [p for p in pp.parts if p not in ("/", "")]
        if not parts:
            raise BridgeProtocolError(CODE_NOT_FOUND, f"cloud file not found: {raw}")
        for p in parts:
            if p in (".", "..") or "/" in p or "\\" in p:
                raise BridgeProtocolError(
                    CODE_INVALID_PARAMS,
                    f"cloud_path {raw!r} must not contain '.' / '..' / stray separators; "
                    "give the plain absolute path as you see it inside your folder")
        root_fd = self._user_root_fd(user_id)
        chain = _Dirs(root_fd, [])
        try:
            for comp in parts[:-1]:
                try:
                    chain = chain.enter(comp)
                except FileNotFoundError:
                    raise BridgeProtocolError(CODE_NOT_FOUND, f"cloud file not found: {raw}")
                except _NotAFolder:
                    # A symlink sits where a directory must be — refused,
                    # never followed.
                    raise BridgeProtocolError(
                        CODE_NOT_ALLOWED,
                        f"{raw!r} points outside your own folder on the cloud machine "
                        "(a link sits in its path); only files inside your own folder "
                        "can be sent to the user's machine")
            try:
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=chain.fd)
            except FileNotFoundError:
                raise BridgeProtocolError(CODE_NOT_FOUND, f"cloud file not found: {raw}")
            except OSError:
                # Includes ELOOP (the final component is a symlink).
                raise BridgeProtocolError(
                    CODE_NOT_ALLOWED,
                    f"{raw!r} is a link, not a regular file; only files inside your own "
                    "folder can be sent to the user's machine")
            try:
                st = os.fstat(fd)
                if not statmod.S_ISREG(st.st_mode):
                    raise BridgeProtocolError(CODE_NOT_FOUND, f"cloud file not found: {raw}")
            except BaseException:
                os.close(fd)
                raise
            return fd, chain, st
        except BaseException:
            chain.close()
            raise

    async def _hash_matches(self, dirs: _Dirs, name: str, sha256: Any) -> bool:
        """True when the regular file `name` below the verified dir handle
        hashes (on its open fd, links never followed) to `sha256`. Any open
        or read failure — including a symlink or a mid-check deletion —
        simply means "not the same copy": the caller re-transfers."""
        if not isinstance(sha256, str) or not sha256:
            return False
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirs.fd)
        except OSError:
            return False
        try:
            st = os.fstat(fd)
            if not statmod.S_ISREG(st.st_mode):
                return False
            return await asyncio.to_thread(_sha256_fd, fd) == sha256
        except OSError:
            return False
        finally:
            os.close(fd)

    # ------------------------------------------------------- start: upload
    async def start_upload(self, user_id: str, pair_id: str, local_path: str,
                           force: bool = False) -> Transfer:
        # 1) Open the user's folder first: no folder, no transfer (and
        #    nothing is created on disk for unknown accounts).
        root_fd = self._user_root_fd(user_id)
        try:
            # 2) Ask the machine what the file looks like right now. No
            #    max_bytes here: the real bridge refuses an over-limit file
            #    with a 4003 error before reporting its size, and deciding
            #    what an over-limit answer looks like is OUR job (a normal
            #    advisory below, not an error).
            info = await self._call(user_id, pair_id, "transfer_upload",
                                    {"path": local_path, "stat_only": True})
            size, mtime = int(info.get("size", 0)), int(info.get("mtime_unix", 0))
            rel = str(info.get("path") or local_path)
            comps, visible = self._dest_for(user_id, rel, str(info.get("name") or ""))

            # 2b) Over the transfer limit? A normal answer with the way
            #     forward, not an error — and the machine is never asked to
            #     send a single byte.
            if size > self.max_bytes:
                return self._advisory(
                    user_id, pair_id, "up", rel, str(visible), size,
                    f"file is {size:,} bytes, over the {self.max_bytes:,}-byte transfer limit")

            # 3) Already have exactly this version in the cloud? Then do
            #    nothing — but only if the cloud file is still the very copy
            #    we landed: size and mtime must match the index AND the
            #    file's current content must hash to the recorded sha256.
            #    Content is checked because an edit that keeps the size and
            #    restores the mtime (many copy/extract tools preserve times)
            #    would otherwise look "unchanged". The file is opened
            #    relative to the verified dir handle without following
            #    links, and the hash is computed on the open fd in a worker
            #    thread (never blocking the event loop, never re-resolving
            #    the name).
            key = f"{pair_id}|{rel}"
            try:
                dirs = _Dirs(root_fd, comps[:-1])
                root_fd = -1  # the chain owns it now
            except FileNotFoundError:
                dirs = None  # landing folder missing: a normal upload below
            except _NotAFolder as exc:
                raise exc.refuse() from None
            if dirs is not None:
                try:
                    ent = self._index_load(dirs).get(key)
                    st = _stat_name(dirs.fd, comps[-1]) if ent else None
                    if (not force and ent and ent.get("size") == size and ent.get("mtime") == mtime
                            and st is not None and statmod.S_ISREG(st.st_mode)
                            and st.st_size == ent.get("landed_size")
                            and st.st_mtime_ns == ent.get("landed_mtime_ns")
                            and await self._hash_matches(dirs, comps[-1], ent.get("sha256"))):
                        t = self._new(user_id=user_id, pair_id=pair_id, direction="up",
                                      local_path=rel, cloud_path=Path(visible),
                                      size=size, mtime_unix=mtime, received=size)
                        t.result = {"sha256": ent.get("sha256", ""), "fresh": True,
                                    "visible": str(visible)}
                        t.finish(True)
                        return t
                finally:
                    dirs.close()

            # 4) An identical transfer already running? Join it instead of
            #    starting a second copy (the model may call the tool twice).
            for other in self._transfers.values():
                if (other.state == "active" and other.direction == "up" and other.user_id == user_id
                        and other.pair_id == pair_id and other.local_path == rel):
                    self._check_stall(other)
                    if other.state == "active":
                        return other

            # 5) Open the landing directory chain (creating what is missing)
            #    and the anonymous temp file. Everything from here on happens
            #    relative to these handles.
            if root_fd < 0:
                root_fd = self._user_root_fd(user_id)
            try:
                dirs = _Dirs(root_fd, comps[:-1], create=True)
                root_fd = -1
            except FileNotFoundError as exc:
                raise BridgeProtocolError(CODE_NOT_FOUND, str(exc))
            except _NotAFolder as exc:
                raise exc.refuse() from None
            try:
                # A symlink at the final name must never be written through;
                # refuse up front (a regular file is fine — it gets replaced
                # by the landing rename, which never follows links).
                st = _stat_name(dirs.fd, comps[-1])
                if st is not None and statmod.S_ISLNK(st.st_mode):
                    raise BridgeProtocolError(
                        CODE_NOT_ALLOWED,
                        f"the landing path {comps[-1]!r} is a link; the transfer was not "
                        "started (it would write outside your own folder)")
                free = shutil.disk_usage(f"/proc/self/fd/{dirs.fd}").free
                if size + DISK_RESERVE_BYTES > free:
                    t = self._advisory(
                        user_id, pair_id, "up", rel, str(visible), size,
                        f"not enough space in the cloud to receive this file ({size:,} bytes needed, "
                        f"{free:,} free, {DISK_RESERVE_BYTES:,} kept in reserve)")
                    dirs.close()
                    return t

                t = self._new(user_id=user_id, pair_id=pair_id, direction="up", local_path=rel,
                              cloud_path=Path(str(visible)), size=size, mtime_unix=mtime)
                t.result = {"visible": str(visible)}
                t.dirs = dirs
                t.sha = hashlib.sha256()
                # O_TMPFILE: the file has no name, so nothing in the user's
                # folder can redirect or replace what we write.
                t.file_fd = os.open(".", os.O_RDWR | os.O_TMPFILE | os.O_CLOEXEC, 0o600,
                                    dir_fd=dirs.fd)
            except BaseException:
                dirs.close()
                raise
            try:
                # max_bytes rides along as the bridge-side last line of
                # defence: if the file grew past the limit between the stat
                # probe and now, the bridge refuses with 4003 instead of
                # streaming an over-limit file. That refusal is turned into
                # the same advisory the probe-time check produces.
                await self._call(user_id, pair_id, "transfer_upload",
                                 {"path": local_path, "max_bytes": self.max_bytes, **self._bridge_params(t)})
            except BridgeProtocolError as exc:
                if exc.code == CODE_TOO_LARGE:
                    # Release this transfer's handles before handing back a
                    # fresh advisory record (finish() unlinks any
                    # materialized name, closes the temp fd and the dirs).
                    t.finish(False, exc.message)
                    return self._advisory(
                        user_id, pair_id, "up", rel, str(visible), size,
                        f"file is over the {self.max_bytes:,}-byte transfer limit "
                        "(it grew between the size check and the upload start)")
                self._abort(t, exc.message)
                raise
            return t
        finally:
            if root_fd >= 0:
                os.close(root_fd)

    # ----------------------------------------------------- start: download
    async def start_download(self, user_id: str, pair_id: str, cloud_path: str,
                             local_path: str, overwrite: bool) -> Transfer:
        fd, dirs, st = self._open_agent_file(user_id, cloud_path)
        size = st.st_size
        if size > self.max_bytes:
            os.close(fd)
            dirs.close()
            return self._advisory(
                user_id, pair_id, "down", local_path or cloud_path.rsplit("/", 1)[-1] or "file",
                cloud_path, size,
                f"file is {size:,} bytes, over the {self.max_bytes:,}-byte transfer limit")
        t = self._new(user_id=user_id, pair_id=pair_id, direction="down",
                      local_path=local_path or cloud_path.rsplit("/", 1)[-1] or "file",
                      cloud_path=Path(cloud_path), size=size)
        t.dirs = dirs
        t.file_fd = fd
        try:
            # Hash the held fd, not the path: the digest is of the exact
            # file /content will serve, whatever the user does to the name.
            t.expected_sha = await asyncio.to_thread(_sha256_fd, fd)
            acc = await self._call(user_id, pair_id, "transfer_download",
                                   {"path": t.local_path, "size": size, "sha256": t.expected_sha,
                                    "overwrite": bool(overwrite), **self._bridge_params(t)})
        except BridgeProtocolError as exc:
            t.finish(False, exc.message)
            raise
        t.local_path = str(acc.get("path") or t.local_path)
        return t

    # -------------------------------------------------------------- status
    def get_for_user(self, user_id: str, transfer_id: str) -> Transfer:
        t = self._transfers.get(transfer_id)
        if t is None or t.user_id != user_id:
            raise BridgeProtocolError(
                CODE_NOT_FOUND, f"no transfer {transfer_id!r} (finished long ago, or the server restarted) — "
                                "start it again with fetch_to_cloud / deliver_to_local")
        return t

    async def wait(self, t: Transfer, seconds: float) -> None:
        """Wait up to `seconds` for the transfer to finish."""
        deadline = time.monotonic() + seconds
        while t.state == "active":
            self._check_stall(t)
            left = deadline - time.monotonic()
            if left <= 0 or t.state != "active":
                return
            try:
                await asyncio.wait_for(t.done.wait(), timeout=min(left, 2.0))
            except asyncio.TimeoutError:
                continue

    def _agent_view(self, t: Transfer) -> str:
        """The path the agent should use, as seen from inside its folder."""
        if t.direction == "up":
            return str(t.result.get("visible") or "")
        return t.local_path

    def describe(self, t: Transfer) -> str:
        """Plain-text status for the model. Short on purpose."""
        what = "local → cloud" if t.direction == "up" else "cloud → local"
        if t.result.get("advisory"):
            # A limit was hit: nothing moved, and the text must hand the
            # model a concrete next step, not a dead end.
            if t.direction == "up":
                path_line = f"local_path: {t.local_path}\n"
                alt = ("What you can do instead: ask the user to split or compress the file on their "
                       "machine and fetch the smaller part(s), or ask the administrator to raise the "
                       "transfer limit / free up cloud space.")
            else:
                path_line = f"cloud_path: {t.cloud_path}\n"
                alt = ("What you can do instead: split or compress the file here in the cloud "
                       "(with your command-line tools) and deliver the smaller part(s), or ask the "
                       "administrator to raise the transfer limit.")
            return (f"NOT TRANSFERRED {what}: {t.error}\n"
                    f"{path_line}"
                    f"size: {t.size:,} bytes\n"
                    f"{alt}")
        if t.state == "done" and t.direction == "up":
            fresh = " (cloud copy was already up to date — nothing re-sent)" if t.result.get("fresh") else ""
            kept = t.result.get("kept_as")
            kept_line = (f"The previous cloud copy had been modified and was kept as {kept}.\n"
                         if kept else "")
            return (f"DONE {what}{fresh}\n"
                    f"cloud_path: {self._agent_view(t)}\n"
                    f"local_path: {t.local_path}\n"
                    f"size: {t.size:,} bytes\n"
                    f"sha256: {t.result.get('sha256', '')}\n"
                    f"{kept_line}"
                    "The file is now on the cloud machine at cloud_path. Work on it there with your own "
                    "file/shell tools. It is a copy: the user's original stays untouched on their machine.\n"
                    "If the file is larger than 10 MB, process it with command-line tools (execute): "
                    "the platform's built-in search skips files over 10 MB.")
        if t.state == "done":
            note = " The previous version was moved to the recycle bin." if t.result.get("replaced") else ""
            return (f"DONE {what}\n"
                    f"local_path: {t.result.get('path') or t.local_path}\n"
                    f"size: {t.size:,} bytes\n"
                    f"The file is now on the user's machine.{note}")
        if t.state == "failed":
            return f"FAILED {what}: {t.error}"
        have = min(t.received + t.inflight, t.size)
        pct = (100.0 * have / t.size) if t.size else 0.0
        elapsed = max(time.monotonic() - t.created, 0.001)
        speed = have / elapsed
        eta = f", about {int((t.size - have) / speed) + 1}s left" if speed > 0 and t.size else ""
        idle = time.monotonic() - t.updated
        if idle > 20:
            # No bytes for a while: say so plainly instead of showing a stale
            # speed. The bridge keeps retrying on its own and resumes from
            # where it stopped; nothing has to be restarted.
            left = max(int(STALL_SECONDS - idle), 0)
            return (f"IN PROGRESS {what}: paused at {pct:.0f}% ({have:,} of {t.size:,} bytes) — no data "
                    f"for {int(idle)}s, the user's computer seems to be offline. It will resume by itself "
                    f"from this point when the connection is back (waiting up to {left}s more).\n"
                    f"transfer_id: {t.id}\n"
                    "Call transfer_status again to keep waiting. Do NOT start the same transfer again.")
        return (f"IN PROGRESS {what}: {pct:.0f}% ({have:,} of {t.size:,} bytes, "
                f"{speed / 1048576:.1f} MB/s{eta})\n"
                f"transfer_id: {t.id}\n"
                "Call transfer_status with this transfer_id to keep waiting. Do NOT start the same "
                "transfer again — it is still running.")

    # ----------------------------------------------------- landing (upload)
    async def _land(self, t: Transfer, digest: str) -> tuple[os.stat_result, str]:
        """Give the verified anonymous upload file its final name, never
        overwriting anything: the linkat fails with EEXIST while the name is
        taken, a name already taken is first moved aside atomically and
        overwrite-free (renameat2 RENAME_NOREPLACE), and only a moved-aside
        copy that provably IS the copy we landed last time (inode + sha256
        of its content both match the index record) is removed.

        No visible intermediate name ever exists: the file goes straight
        from anonymous (O_TMPFILE) to the final name. Returns (fstat of the
        landed fd, agent-visible kept-as path or "")."""
        dirs = t.dirs
        assert dirs is not None
        final = t.cloud_path.name
        idx_prev = self._index_load(dirs).get(f"{t.pair_id}|{t.local_path}")
        kept_as = ""
        for _ in range(20):
            try:
                os.link(f"/proc/self/fd/{t.file_fd}", final,
                        dst_dir_fd=dirs.fd, follow_symlinks=True)
                st = os.fstat(t.file_fd)
                if kept_as:
                    # The agent-visible path of the kept copy: the
                    # transfer's visible path with the final component
                    # swapped for the kept-as name.
                    vis = str(t.result.get("visible") or "")
                    base = vis.rsplit("/", 1)[0] if "/" in vis else ""
                    t.result["kept_as"] = f"{base}/{kept_as}" if base else f"/{kept_as}"
                return st, kept_as
            except FileExistsError:
                pass  # the name is taken: move the occupant aside below
            st_dst = _stat_name(dirs.fd, final)
            if st_dst is None:
                continue  # the occupant vanished between the two syscalls
            if not statmod.S_ISREG(st_dst.st_mode):
                raise OSError(f"the landing path {final!r} is not a regular file")
            kept_as = self._keep_cloud_edited(dirs, final)
            if await self._drop_if_our_last_copy(dirs, kept_as, idx_prev):
                kept_as = ""  # it was our own untouched copy: nothing to report
        raise OSError(f"the landing path {final!r} stayed occupied through 20 attempts")

    async def _drop_if_our_last_copy(self, dirs: _Dirs, kept_as: str,
                                     idx_prev: Any) -> bool:
        """Delete the moved-aside occupant ONLY if it is provably the copy
        this adapter landed last time, untouched: its content hashes to the
        index's sha256 AND the name still resolves to the very inode that
        was hashed (re-checked after hashing). In every other case — the
        content was edited, the index has no record, the inode changed, any
        step fails — the file is kept and the transfer result names it.
        Returns True when the file was removed."""
        if not isinstance(idx_prev, dict) or not idx_prev.get("sha256"):
            return False  # no record of it: it is somebody's work — keep it
        try:
            fd = os.open(kept_as, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=dirs.fd)
        except OSError:
            return False
        try:
            st = os.fstat(fd)
            if not statmod.S_ISREG(st.st_mode):
                return False
            if await asyncio.to_thread(_sha256_fd, fd) != idx_prev["sha256"]:
                return False  # content changed since we landed it: cloud work — keep it
            # The stat between hashing and unlinking narrows the race, it
            # does not close it: the agent could still swap this
            # timestamped keep-as name in the microsecond between the two
            # calls, in which case we would remove what it just placed
            # there. That residual risk is accepted.
            st2 = _stat_name(dirs.fd, kept_as)
            if st2 is None or (st2.st_ino, st2.st_dev) != (st.st_ino, st.st_dev):
                return False
            os.unlink(kept_as, dir_fd=dirs.fd)
            logger.info("removed the untouched previous copy of the landed file (kept-as %s)", kept_as)
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    # ----------------------------------------------- bridge-facing HTTP API
    def _auth(self, request: Request, transfer_id: str) -> Transfer | JSONResponse:
        raw = request.headers.get("authorization", "")
        token = raw[7:].strip() if raw.lower().startswith("bearer ") else None
        try:
            user_id = self.settings.token_store.authenticate(KIND_BRIDGE, token)
        except BridgeProtocolError:
            return JSONResponse({"error": "authentication failed"}, status_code=401)
        t = self._transfers.get(transfer_id)
        if t is None:
            return JSONResponse({"error": "unknown transfer"}, status_code=404)
        ticket = request.headers.get("x-transfer-ticket", "")
        if t.user_id != user_id or not secrets.compare_digest(ticket, t.ticket):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return t

    def build_router(self) -> APIRouter:
        r = APIRouter()
        base = self.url_base

        @r.get(base + "/{tid}")
        async def status(tid: str, request: Request):
            t = self._auth(request, tid)
            if isinstance(t, JSONResponse):
                return t
            return {"state": t.state, "received": t.received, "size": t.size}

        @r.put(base + "/{tid}/chunk")
        async def put_chunk(tid: str, request: Request, offset: int = 0):
            t = self._auth(request, tid)
            if isinstance(t, JSONResponse):
                return t
            if t.direction != "up" or t.state != "active":
                return JSONResponse({"error": f"transfer is {t.state}", "received": t.received}, status_code=410)
            declared = int(request.headers.get("content-length") or 0)
            if declared > MAX_CHUNK_BYTES:
                return JSONResponse({"error": "chunk too large"}, status_code=413)
            # Buffer the whole chunk first: a chunk is applied all-or-nothing,
            # so a connection cut mid-chunk leaves the file at a clean offset.
            buf = bytearray()
            async for piece in request.stream():
                buf.extend(piece)
                # Bytes are arriving: that is activity and progress even
                # though the chunk is only applied once it is complete (on a
                # slow link one chunk takes tens of seconds).
                t.inflight = len(buf)
                t.touch()
                if len(buf) > MAX_CHUNK_BYTES:
                    return JSONResponse({"error": "chunk too large"}, status_code=413)
            if declared and len(buf) != declared:
                return JSONResponse({"error": "short chunk", "received": t.received}, status_code=400)
            async with t.lock:
                if t.state != "active":
                    return JSONResponse({"error": f"transfer is {t.state}"}, status_code=410)
                if offset != t.received:
                    return JSONResponse({"error": "offset mismatch", "received": t.received}, status_code=409)
                if t.received + len(buf) > t.size:
                    self._abort(t, "the local file grew during the transfer; please fetch it again")
                    return JSONResponse({"error": "exceeds declared size"}, status_code=413)
                try:
                    # All chunks go to the one anonymous temp fd held since
                    # the start; the folder's names cannot redirect this.
                    await asyncio.to_thread(os.pwrite, t.file_fd, bytes(buf), t.received)
                except OSError as exc:
                    self._abort(t, f"could not write to cloud storage: {exc}")
                    return JSONResponse({"error": "storage write failed"}, status_code=507)
                t.sha.update(buf)
                t.received += len(buf)
                t.inflight = 0
                t.touch()
            return {"received": t.received}

        @r.get(base + "/{tid}/content")
        async def content(tid: str, request: Request):
            t = self._auth(request, tid)
            if isinstance(t, JSONResponse):
                return t
            if t.direction != "down" or t.state != "active":
                return JSONResponse({"error": f"transfer is {t.state}"}, status_code=410)
            start = 0
            m = re.match(r"bytes=(\d+)-", request.headers.get("range", ""))
            if m:
                start = int(m.group(1))
            if start > t.size:
                return JSONResponse({"error": "range not satisfiable"}, status_code=416)

            async def body():
                # Reads go through the fd captured at start_download: even if
                # the source path was renamed or replaced by a link since,
                # the bytes served are the file that was verified then.
                pos = start
                while True:
                    data = await asyncio.to_thread(os.pread, t.file_fd, 1024 * 1024, pos)
                    if not data:
                        return
                    pos += len(data)
                    t.received = max(t.received, pos)
                    t.touch()
                    yield data

            return StreamingResponse(body(), status_code=206, media_type="application/octet-stream", headers={
                "Content-Range": f"bytes {start}-{max(t.size - 1, 0)}/{t.size}",
                "Content-Length": str(t.size - start),
            })

        @r.post(base + "/{tid}/complete")
        async def complete(tid: str, request: Request):
            t = self._auth(request, tid)
            if isinstance(t, JSONResponse):
                return t
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                return JSONResponse({"error": "invalid JSON"}, status_code=400)
            async with t.lock:
                if t.state != "active":
                    return {"state": t.state}
                if not body.get("ok"):
                    self._abort(t, str(body.get("error") or "the local machine reported a failure")[:500])
                    return {"state": t.state}
                if t.direction == "down":
                    t.result = {"path": str(body.get("path") or ""), "replaced": bool(body.get("replaced"))}
                    t.received = t.size
                    t.finish(True)
                    return {"state": t.state}
                # Integrity is checked against what actually landed: the
                # anonymous fd is the only writer (O_TMPFILE has no name, so
                # nothing else in the user's folder can reach it), and fstat
                # confirms exactly `size` bytes made it to disk.
                try:
                    st = os.fstat(t.file_fd)
                except OSError as exc:
                    self._abort(t, f"could not verify the received file: {exc}")
                    return {"state": t.state}
                digest = t.sha.hexdigest()
                if (t.received != t.size or st.st_size != t.size
                        or int(body.get("size", -1)) != t.size or body.get("sha256") != digest):
                    self._abort(t, "integrity check failed — the copy that reached the cloud does not match "
                                   "the local file; please fetch it again")
                    return {"state": t.state}
                try:
                    # Set the local file's mtime on our own fd, before the
                    # file gets any name the user could swap.
                    if t.mtime_unix:
                        os.utime(t.file_fd, ns=(t.mtime_unix * 1_000_000_000,) * 2)
                    # Match the platform's other files (0644), not the 0600
                    # an anonymous O_TMPFILE starts with.
                    os.fchmod(t.file_fd, 0o644)
                    st_final, kept_as = await self._land(t, digest)
                except OSError as exc:
                    self._abort(t, f"could not put the file in place in the cloud: {exc}")
                    return {"state": t.state}
                async with self._index_lock:
                    idx = self._index_load(t.dirs)
                    idx[f"{t.pair_id}|{t.local_path}"] = {
                        "size": t.size, "mtime": t.mtime_unix, "sha256": digest,
                        "landed_size": st_final.st_size,
                        "landed_mtime_ns": st_final.st_mtime_ns,
                        "at": int(time.time())}
                    self._index_save(t.dirs, idx)
                t.result = {**t.result, "sha256": digest}
                t.finish(True)
            return {"state": t.state}

        return r


def _sha256_fd(fd: int) -> str:
    """SHA-256 of an open file, read through the fd itself: the digest is of
    the exact object that was verified, whatever happens to its name."""
    h = hashlib.sha256()
    offset = 0
    while True:
        block = os.pread(fd, 1024 * 1024, offset)
        if not block:
            break
        h.update(block)
        offset += len(block)
    return h.hexdigest()
