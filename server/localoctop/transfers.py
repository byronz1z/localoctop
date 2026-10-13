"""Out-of-band file transfer between the user's machine and the cloud
(experiment branch).

Why this exists: an MCP tool result is fed to the model verbatim, so a file
must never travel inside one. Here the MCP tool only *starts* a transfer and
reports where the file ended up; the bytes move over plain HTTP between the
bridge and this adapter:

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
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
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
DEFAULT_MAX_TRANSFER_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
# Keep this much disk free after a transfer lands, so one big file cannot
# fill the volume the rest of the platform lives on.
DISK_RESERVE_BYTES = 1024 * 1024 * 1024
# A transfer with no byte movement for this long is declared dead (the
# machine went offline / the bridge was closed).
STALL_SECONDS = float(os.environ.get("LOCALOCTOP_TRANSFER_STALL_SECONDS", 420))
# Finished transfer records are kept this long for transfer_status lookups.
RECORD_TTL_SECONDS = 3600.0

# A user name (== the token's user_id == the Octop folder name) must not
# contain anything that could break the one-folder-per-user confinement.
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _check_user_name(user_id: str) -> str:
    """Validate that user_id is safe to use as a folder name, verbatim.

    Allowed: any non-blank name without path separators, control characters,
    or "." / ".." — Chinese names such as '马威' are fine and kept as-is.
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
    part_path: Path | None = None
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

    def touch(self) -> None:
        self.updated = time.monotonic()

    def finish(self, ok: bool, error: str = "") -> None:
        if self.state != "active":
            return
        self.state = "done" if ok else "failed"
        self.error = error
        self.finished = time.monotonic()
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
        self._sweep_stale_parts()

    def _sweep_stale_parts(self) -> None:
        """Transfers live in memory, so after a restart every half-received
        '.part-<id>' file is an orphan nobody will finish. Remove them, or
        each interrupted transfer would leak its size in disk space. Sweep
        every existing user's landing sub-folder."""
        try:
            users = list(self.users_dir.iterdir())
        except OSError:
            return
        for u in users:
            try:
                parts_dir = u / self.landing_subdir
                if not parts_dir.is_dir():
                    continue
                for p in parts_dir.rglob("*.part-*"):
                    if p.is_file():
                        try:
                            p.unlink()
                            logger.info("removed orphaned partial file %s", p)
                        except OSError:
                            logger.warning("could not remove orphaned partial file %s", p)
            except OSError:
                continue

    # ------------------------------------------------------------ helpers
    def _user_root(self, user_id: str) -> Path:
        """The real path of the user's Octop folder.

        The folder must already exist as a real directory (it belongs to the
        platform) and must not itself be a symlink — otherwise a planted
        link would smuggle one user's traffic into another's folder. We
        never create it: an account without a folder simply cannot transfer.
        """
        name = _check_user_name(user_id)
        root = self.users_dir / name
        if root.is_symlink():
            raise BridgeProtocolError(
                CODE_NOT_ALLOWED,
                f"the folder for user {user_id!r} on the cloud machine is a link, "
                "not a real folder; refusing to transfer (platform misconfiguration)")
        if not root.is_dir():
            raise BridgeProtocolError(
                CODE_NOT_FOUND,
                f"this account has no folder on the cloud machine yet "
                f"(no Octop user named {user_id!r}); the transfer was not started. "
                "Create the Octop user first, then try again.")
        return root.resolve()

    def _inside(self, p: Path, root: Path) -> bool:
        try:
            p.relative_to(root)
            return True
        except ValueError:
            return False

    def _dest_for(self, user_id: str, rel: str, name: str) -> tuple[Path, PurePosixPath]:
        """Cloud landing path for a local file (stable per local path).

        Returns (absolute path on disk, path the agent sees). The parent
        directory chain is created only after each step is proven to stay
        inside the user's folder, so a symlink planted by the agent cannot
        steer the write into someone else's folder."""
        root = self._user_root(user_id)
        rel = rel.replace("\\", "/")
        parts = [p for p in PurePosixPath(rel).parts if p not in ("", ".", "..", "/")]
        # An absolute local path (file outside every whitelist root should
        # never reach here, but be defensive): keep only the file name.
        if not parts or re.match(r"^[A-Za-z]:", parts[0]) or rel.startswith("/"):
            parts = [name or "file"]
        clean = [re.sub(r'[<>:"|?*\x00-\x1f]', "_", p) for p in parts]
        visible = PurePosixPath("/") / self.landing_subdir / PurePosixPath(*clean)
        dest = root.joinpath(self.landing_subdir, *clean)
        self._check_path_chain(dest, root)
        return dest, visible

    def _check_path_chain(self, dest: Path, root: Path) -> None:
        """Write-side symlink defence: walk from the user's real folder down
        to `dest`; every component that already exists (including a dangling
        symlink, which Path.exists() would miss) must resolve to a place
        still inside the user's folder. A planted link pointing at another
        user's folder fails the check and nothing is written or created."""
        rel = dest.relative_to(root)
        probe = root
        for part in rel.parts:
            probe = probe / part
            if probe.is_symlink() or probe.exists():
                if not self._inside(probe.resolve(), root):
                    raise BridgeProtocolError(
                        CODE_NOT_ALLOWED,
                        f"the landing path contains a link that points outside your own "
                        f"folder ({part!r}); the transfer was not started. Remove the "
                        "link and try again.")

    def _ensure_parent(self, dest: Path, root: Path) -> Path:
        """Create dest's parent directories, then prove the real location of
        the parent is still inside the user's folder (nothing was written
        through a planted link)."""
        parent = dest.parent
        parent.mkdir(parents=True, exist_ok=True)
        real_parent = parent.resolve()
        if not self._inside(real_parent, root):
            raise BridgeProtocolError(
                CODE_NOT_ALLOWED,
                "the destination folder resolves outside your own folder via a link; "
                "the transfer was not started")
        return real_parent

    def _index_path(self, user_id: str) -> Path:
        return self._user_root(user_id) / self.landing_subdir / ".localoctop-index.json"

    def _index_load(self, user_id: str) -> dict[str, Any]:
        try:
            return json.loads(self._index_path(user_id).read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _index_save(self, user_id: str, idx: dict[str, Any]) -> None:
        p = self._index_path(user_id)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(idx, ensure_ascii=False), "utf-8")
        os.replace(tmp, p)

    def _gc(self) -> None:
        now = time.monotonic()
        for tid, t in list(self._transfers.items()):
            if t.state != "active" and now - t.finished > RECORD_TTL_SECONDS:
                self._transfers.pop(tid, None)

    def _check_stall(self, t: Transfer) -> None:
        if t.state == "active" and time.monotonic() - t.updated > STALL_SECONDS:
            self._abort(t, "the transfer stopped making progress — the local machine went offline "
                           "or the localoctop client was closed. Start it again when the machine is back.")

    def _abort(self, t: Transfer, msg: str) -> None:
        if t.direction == "up" and t.part_path is not None:
            try:
                t.part_path.unlink(missing_ok=True)
            except OSError:
                pass
        t.finish(False, msg)

    def _new(self, **kw: Any) -> Transfer:
        self._gc()
        t = Transfer(id=secrets.token_urlsafe(9), ticket=secrets.token_urlsafe(24), **kw)
        self._transfers[t.id] = t
        return t

    def _bridge_params(self, t: Transfer) -> dict[str, Any]:
        return {"transfer_id": t.id, "ticket": t.ticket,
                "url_path": f"{self.url_base}/{t.id}", "chunk_bytes": CHUNK_BYTES}

    async def _call(self, user_id: str, pair_id: str, method: str, params: dict[str, Any]) -> Any:
        if pair_id:
            return await self.registry.call_paired(user_id, pair_id, method, params,
                                                   timeout=self.settings.bridge_timeout)
        return await self.registry.call(user_id, method, params, timeout=self.settings.bridge_timeout)

    def _resolve_agent_path(self, user_id: str, raw: str) -> Path:
        """Map an agent-visible absolute path ("/..." inside the user's
        folder) to a real file on disk, confined to the user's own folder.

        The path must stay inside the user's real folder after every link is
        resolved, and it must be a regular file — anything else is refused.
        Only the token owner's folder is ever readable; there are no other
        read roots."""
        if not raw or "\x00" in raw:
            raise BridgeProtocolError(CODE_INVALID_PARAMS, "cloud_path is required")
        pp = PurePosixPath(raw)
        if not pp.is_absolute():
            raise BridgeProtocolError(
                CODE_INVALID_PARAMS,
                f"cloud_path must be an absolute path the agent sees, starting with '/' "
                f"(your folder is the root), got {raw!r}")
        root = self._user_root(user_id)
        parts = [p for p in pp.parts if p not in ("/", "")]
        mapped = root.joinpath(*parts)
        try:
            real = mapped.resolve(strict=True)
        except OSError:
            raise BridgeProtocolError(CODE_NOT_FOUND, f"cloud file not found: {raw}")
        if not self._inside(real, root):
            raise BridgeProtocolError(
                CODE_NOT_ALLOWED,
                f"{raw!r} points outside your own folder on the cloud machine; "
                "only files inside your own folder can be sent to the user's machine")
        if not real.is_file():
            raise BridgeProtocolError(CODE_NOT_FOUND, f"cloud file not found: {raw}")
        return real

    # ------------------------------------------------------- start: upload
    async def start_upload(self, user_id: str, pair_id: str, local_path: str,
                           force: bool = False) -> Transfer:
        # 1) Resolve the user's folder first: no folder, no transfer (and
        #    nothing is created on disk for unknown accounts).
        root = self._user_root(user_id)
        # 2) Ask the machine what the file looks like right now.
        info = await self._call(user_id, pair_id, "transfer_upload",
                                {"path": local_path, "stat_only": True, "max_bytes": self.max_bytes})
        size, mtime = int(info.get("size", 0)), int(info.get("mtime_unix", 0))
        rel = str(info.get("path") or local_path)
        dest, visible = self._dest_for(user_id, rel, str(info.get("name") or ""))

        # 3) Already have exactly this version in the cloud? Then do nothing.
        key = f"{pair_id}|{rel}"
        idx = self._index_load(user_id)
        ent = idx.get(key)
        if (not force and ent and ent.get("size") == size and ent.get("mtime") == mtime
                and dest.is_file() and not dest.is_symlink() and dest.stat().st_size == size):
            t = self._new(user_id=user_id, pair_id=pair_id, direction="up", local_path=rel,
                          cloud_path=dest, size=size, mtime_unix=mtime, received=size)
            t.result = {"sha256": ent.get("sha256", ""), "fresh": True,
                        "visible": str(visible)}
            t.finish(True)
            return t

        # 4) An identical transfer already running? Join it instead of
        #    starting a second copy (the model may call the tool twice).
        for other in self._transfers.values():
            if (other.state == "active" and other.direction == "up" and other.user_id == user_id
                    and other.pair_id == pair_id and other.local_path == rel):
                self._check_stall(other)
                if other.state == "active":
                    return other

        # 5) Room on the cloud disk?
        real_parent = self._ensure_parent(dest, root)
        if dest.is_symlink():
            raise BridgeProtocolError(
                CODE_NOT_ALLOWED,
                f"the landing path {dest.name!r} is a link; the transfer was not started "
                "(it would write outside your own folder)")
        free = shutil.disk_usage(real_parent).free
        if size + DISK_RESERVE_BYTES > free:
            raise BridgeProtocolError(
                CODE_TOO_LARGE,
                f"not enough space in the cloud to receive this file ({size:,} bytes needed, "
                f"{free:,} free, {DISK_RESERVE_BYTES:,} kept in reserve)")

        t = self._new(user_id=user_id, pair_id=pair_id, direction="up", local_path=rel,
                      cloud_path=dest, size=size, mtime_unix=mtime)
        t.result = {"visible": str(visible)}
        t.part_path = dest.with_name(dest.name + f".part-{t.id}")
        t.sha = hashlib.sha256()
        t.part_path.write_bytes(b"")
        try:
            await self._call(user_id, pair_id, "transfer_upload",
                             {"path": local_path, "max_bytes": self.max_bytes, **self._bridge_params(t)})
        except BridgeProtocolError as exc:
            self._abort(t, exc.message)
            raise
        return t

    # ----------------------------------------------------- start: download
    async def start_download(self, user_id: str, pair_id: str, cloud_path: str,
                             local_path: str, overwrite: bool) -> Transfer:
        src = self._resolve_agent_path(user_id, cloud_path)
        size = src.stat().st_size
        if size > self.max_bytes:
            raise BridgeProtocolError(
                CODE_TOO_LARGE, f"file is {size:,} bytes, larger than the {self.max_bytes:,}-byte transfer limit")
        sha = await asyncio.to_thread(_sha256_file, src)
        if not local_path:
            local_path = src.name
        t = self._new(user_id=user_id, pair_id=pair_id, direction="down", local_path=local_path,
                      cloud_path=src, size=size)
        t.expected_sha = sha
        try:
            acc = await self._call(user_id, pair_id, "transfer_download",
                                   {"path": local_path, "size": size, "sha256": sha,
                                    "overwrite": bool(overwrite), **self._bridge_params(t)})
        except BridgeProtocolError as exc:
            t.finish(False, exc.message)
            raise
        t.local_path = str(acc.get("path") or local_path)
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
        if t.state == "done" and t.direction == "up":
            fresh = " (cloud copy was already up to date — nothing re-sent)" if t.result.get("fresh") else ""
            return (f"DONE {what}{fresh}\n"
                    f"cloud_path: {self._agent_view(t)}\n"
                    f"local_path: {t.local_path}\n"
                    f"size: {t.size:,} bytes\n"
                    f"sha256: {t.result.get('sha256', '')}\n"
                    "The file is now on the cloud machine at cloud_path. Work on it there with your own "
                    "file/shell tools. It is a copy: the user's original stays untouched on their machine.")
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
                    await asyncio.to_thread(_append, t.part_path, bytes(buf))
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
                with open(t.cloud_path, "rb") as f:
                    f.seek(start)
                    pos = start
                    while True:
                        data = await asyncio.to_thread(f.read, 1024 * 1024)
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
                digest = t.sha.hexdigest()
                if t.received != t.size or int(body.get("size", -1)) != t.size or body.get("sha256") != digest:
                    self._abort(t, "integrity check failed — the copy that reached the cloud does not match "
                                   "the local file; please fetch it again")
                    return {"state": t.state}
                # Final write-side check: the landing path must still be a
                # real path inside the user's folder (a link planted while
                # the transfer ran must not be followed by the rename).
                root = self._user_root(t.user_id)
                parent_real = t.cloud_path.parent.resolve()
                if not self._inside(parent_real, root) or t.cloud_path.is_symlink():
                    self._abort(t, "the landing path no longer resolves inside your own folder; "
                                   "the transfer was aborted")
                    return {"state": t.state}
                try:
                    os.replace(t.part_path, t.cloud_path)
                    if t.mtime_unix:
                        os.utime(t.cloud_path, (t.mtime_unix, t.mtime_unix))
                except OSError as exc:
                    self._abort(t, f"could not put the file in place in the cloud: {exc}")
                    return {"state": t.state}
                async with self._index_lock:
                    idx = self._index_load(t.user_id)
                    idx[f"{t.pair_id}|{t.local_path}"] = {
                        "size": t.size, "mtime": t.mtime_unix, "sha256": digest, "at": int(time.time())}
                    self._index_save(t.user_id, idx)
                t.result = {**t.result, "sha256": digest}
                t.finish(True)
            return {"state": t.state}

        return r


def _append(path: Path, data: bytes) -> None:
    with open(path, "ab") as f:
        f.write(data)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()
