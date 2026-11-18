"""File worker (internal only): the sole process that mounts a Project_Store.

Run as ``python -m backend.workbench.fsworker --kind <kind>``. Phase 1
endpoints: /mkproject, /clone, /rmproject, /dirs, /stat, /probe.
Phase 2: /list, /read, /write, /op, /changes. Every
endpoint that takes a path calls ``PathGuard.resolve`` first.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from backend.workbench.paths import PathGuard, PathNotPermitted
from backend.workbench.validation import (
    MAX_OPEN_BYTES,
    classify_file,
    sort_tree,
    validate_git_url,
    validate_kind,
    validate_slug,
)

CLONE_TIMEOUT_S = 300
PROBE_TIMEOUT_S = 5
TMP_PREFIX = ".tmp-"
WRITE_CONCURRENCY = 4
CHANGES_LIMIT = 5000
_SKIP_DIRS = {".git", "node_modules"}


class MkProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str


class CloneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str
    git_url: str


class RmProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str
    confirm: str


class WriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    content: str
    base_mtime_ns: int | None = None  # None means "the file must not exist yet"


class OpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["create", "rename", "move", "delete"]
    path: str
    is_dir: bool = False  # create only
    new_name: str | None = None  # rename only
    dest: str | None = None  # move only (full path relative to the store root)


def _err(status: int, code: str, message: str = "") -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _err_detail(status: int, detail: dict[str, Any]) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


class _Conflict(Exception):
    def __init__(self, current: int | None) -> None:
        self.current = current


class _NotEditable(Exception):
    def __init__(self, size: int, detected: str) -> None:
        self.size = size
        self.detected = detected


def _dir_size(path: str) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def _remove_tree(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def create_app(kind: str, root: str | os.PathLike) -> FastAPI:
    if not validate_kind(kind):
        raise ValueError(f"unknown kind: {kind!r}")
    store_root = os.fspath(root)
    app = FastAPI(title=f"wb-fs-{kind}", docs_url=None, redoc_url=None, openapi_url=None)

    def guard(rel: str, *, create_leaf: bool = False) -> Path:
        try:
            return PathGuard.resolve(store_root, rel, create_leaf=create_leaf)
        except PathNotPermitted as exc:
            raise _err(400, "path_not_permitted", str(exc)) from exc

    def project_path(slug: str, *, create_leaf: bool) -> Path:
        if not validate_slug(slug):
            raise _err(422, "invalid_slug")
        return guard(slug, create_leaf=create_leaf)

    def new_tmp() -> str:
        return os.path.join(store_root, f"{TMP_PREFIX}{uuid.uuid4().hex}")

    @app.post("/mkproject")
    async def mkproject(req: MkProjectRequest) -> dict[str, Any]:
        target = project_path(req.slug, create_leaf=True)
        if os.path.lexists(target):
            raise _err(409, "exists")
        tmp = new_tmp()

        def work() -> None:
            try:
                os.mkdir(tmp)
                if os.path.lexists(target):
                    raise FileExistsError(str(target))
                os.rename(tmp, target)
            except BaseException:
                _remove_tree(tmp)
                raise

        try:
            await asyncio.to_thread(work)
        except FileExistsError as exc:
            raise _err(409, "exists") from exc
        except OSError as exc:
            raise _err(503, "store_error", exc.strerror or "io error") from exc
        return {"slug": req.slug, "kind": kind}

    @app.post("/clone")
    async def clone(req: CloneRequest) -> dict[str, Any]:
        if not validate_git_url(req.git_url):
            raise _err(422, "invalid_git_url")
        target = project_path(req.slug, create_leaf=True)
        if os.path.lexists(target):
            raise _err(409, "exists")
        tmp = new_tmp()
        proc: asyncio.subprocess.Process | None = None
        try:
            env = {
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ALLOW_PROTOCOL": "https:ssh",
                "GIT_SSH_COMMAND": os.environ.get(
                    "GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
                ),
            }
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--quiet", "--", req.git_url, tmp,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                _out, stderr = await asyncio.wait_for(proc.communicate(), CLONE_TIMEOUT_S)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise _err(504, "clone_timeout", f"clone exceeded {CLONE_TIMEOUT_S}s")
            if proc.returncode != 0:
                msg = stderr.decode("utf-8", "replace").strip()[-500:]
                raise _err(502, "clone_failed", msg)
            if os.path.lexists(target):
                raise _err(409, "exists")
            await asyncio.to_thread(os.rename, tmp, target)
        except HTTPException:
            await asyncio.to_thread(_remove_tree, tmp)
            raise
        except BaseException as exc:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            await asyncio.to_thread(_remove_tree, tmp)
            if isinstance(exc, OSError):
                raise _err(503, "store_error", exc.strerror or "io error") from exc
            raise
        return {"slug": req.slug, "kind": kind}

    @app.post("/rmproject")
    async def rmproject(req: RmProjectRequest) -> dict[str, Any]:
        if req.confirm != req.slug:
            raise _err(422, "confirmation_mismatch")
        target = project_path(req.slug, create_leaf=True)
        if not os.path.isdir(target):
            raise _err(404, "not_found")
        # Rename first so no partial project directory is ever visible under its slug.
        doomed = new_tmp()
        try:
            await asyncio.to_thread(os.rename, target, doomed)
        except OSError as exc:
            raise _err(503, "store_error", exc.strerror or "io error") from exc
        await asyncio.to_thread(_remove_tree, doomed)
        return {"slug": req.slug, "removed": not os.path.lexists(doomed)}

    @app.get("/dirs")
    async def dirs() -> dict[str, Any]:
        root_path = guard("")

        def work() -> list[str]:
            names = []
            with os.scandir(root_path) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        names.append(entry.name)
            return sorted(names)

        try:
            names = await asyncio.to_thread(work)
        except OSError as exc:
            raise _err(503, "store_unavailable", exc.strerror or "io error") from exc
        return {"dirs": names}

    @app.get("/stat")
    async def stat(path: str = "", size: bool = False) -> dict[str, Any]:
        resolved = guard(path)

        def work() -> dict[str, Any]:
            try:
                st = os.stat(resolved)
            except FileNotFoundError:
                return {"exists": False}
            is_dir = os.path.isdir(resolved)
            out: dict[str, Any] = {
                "exists": True,
                "is_dir": is_dir,
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
            }
            if size and is_dir:
                out["size_bytes"] = _dir_size(os.fspath(resolved))
            return out

        try:
            return await asyncio.to_thread(work)
        except OSError as exc:
            raise _err(503, "store_unavailable", exc.strerror or "io error") from exc

    @app.get("/probe")
    async def probe() -> dict[str, Any]:
        def work() -> dict[str, Any]:
            result: dict[str, Any] = {
                "kind": kind,
                "mounted": False,
                "writable": False,
                "free_bytes": 0,
            }
            try:
                os.listdir(store_root)
                result["mounted"] = True
                vfs = os.statvfs(store_root)
                result["free_bytes"] = vfs.f_bavail * vfs.f_frsize
            except OSError:
                return result
            marker = os.path.join(store_root, f"{TMP_PREFIX}probe-{uuid.uuid4().hex}")
            try:
                with open(marker, "wb") as fh:
                    fh.write(b"ok")
                result["writable"] = True
            except OSError:
                pass
            finally:
                try:
                    os.unlink(marker)
                except OSError:
                    pass
            return result

        started = time.monotonic()
        try:
            out = await asyncio.wait_for(asyncio.to_thread(work), PROBE_TIMEOUT_S)
        except asyncio.TimeoutError:
            return {"kind": kind, "mounted": False, "writable": False, "free_bytes": 0, "timeout": True}
        out["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return out

    # ---- Phase 2: file operations ----

    write_slots: dict[str, int] = {}

    def project_of(rel: str) -> str:
        parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
        return parts[0] if parts else ""

    def require_inside_project(rel: str) -> None:
        """Mutations must target something below a project root."""
        parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
        if len(parts) < 2:
            raise _err(400, "path_not_permitted", "operation must target a path inside a project")

    def not_editable(size: int, detected: str) -> HTTPException:
        return _err_detail(415, {"error": "not_editable", "size": size, "type": detected,
                                 "message": "file is larger than 2 MiB or not UTF-8 text"})

    @app.get("/list")
    async def list_dir(path: str = "") -> dict[str, Any]:
        resolved = guard(path)

        def work() -> list[dict[str, Any]]:
            entries = []
            with os.scandir(resolved) as it:
                for entry in it:
                    if entry.name.startswith(TMP_PREFIX):
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    entries.append({"name": entry.name, "is_dir": is_dir,
                                    "size": 0 if is_dir else st.st_size, "mtime_ns": st.st_mtime_ns})
            return sort_tree(entries)

        try:
            return {"path": path, "entries": await asyncio.to_thread(work)}
        except FileNotFoundError as exc:
            raise _err(404, "not_found") from exc
        except NotADirectoryError as exc:
            raise _err(400, "not_a_directory") from exc
        except OSError as exc:
            raise _err(503, "store_error", exc.strerror or "io error") from exc

    @app.get("/read")
    async def read_file(path: str) -> dict[str, Any]:
        resolved = guard(path)

        def work() -> dict[str, Any]:
            fd = PathGuard.open_leaf(resolved)
            try:
                st = os.fstat(fd)
                if not os.path.isfile(resolved) or (st.st_mode & 0o170000) != 0o100000:
                    raise IsADirectoryError(errno.EISDIR, "not a regular file")
                with os.fdopen(fd, "rb", closefd=False) as fh:
                    data = fh.read(MAX_OPEN_BYTES + 1)
            finally:
                os.close(fd)
            klass = classify_file(data, st.st_size)
            if not klass.openable:
                raise not_editable(klass.size, klass.detected_type)
            return {"path": path, "content": data.decode("utf-8"), "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns}

        try:
            return await asyncio.to_thread(work)
        except PathNotPermitted as exc:
            raise _err(400, "path_not_permitted", str(exc)) from exc
        except FileNotFoundError as exc:
            raise _err(404, "not_found") from exc
        except IsADirectoryError as exc:
            raise _err(400, "not_a_file") from exc
        except OSError as exc:
            raise _err(503, "store_error", exc.strerror or "io error") from exc

    @app.put("/write")
    async def write_file(req: WriteRequest) -> dict[str, Any]:
        require_inside_project(req.path)
        target = guard(req.path, create_leaf=True)
        data = req.content.encode("utf-8")
        klass = classify_file(data)
        if not klass.openable:
            raise not_editable(klass.size, klass.detected_type)
        project = project_of(req.path)
        # Single event loop thread: check-and-increment is atomic.
        if write_slots.get(project, 0) >= WRITE_CONCURRENCY:
            raise _err(429, "concurrency_limit", "too many concurrent writes for this project")
        write_slots[project] = write_slots.get(project, 0) + 1

        def work() -> int:
            directory = os.path.dirname(target)
            tmp = os.path.join(directory, f"{TMP_PREFIX}{uuid.uuid4().hex}")
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(data)
                        fh.flush()
                        os.fsync(fh.fileno())
                    # Advisory lock on the directory: it survives os.replace, which
                    # swaps the file inode and would drop a lock held on the file.
                    fcntl.flock(dir_fd, fcntl.LOCK_EX)
                    try:
                        try:
                            st = os.lstat(target)
                        except FileNotFoundError:
                            st = None
                        if st is None:
                            if req.base_mtime_ns is not None:
                                raise _Conflict(None)
                        else:
                            if (st.st_mode & 0o170000) != 0o100000:
                                raise IsADirectoryError(errno.EISDIR, "not a regular file")
                            if req.base_mtime_ns is None or st.st_mtime_ns != req.base_mtime_ns:
                                raise _Conflict(st.st_mtime_ns)
                            with open(target, "rb") as old:
                                prefix = old.read(MAX_OPEN_BYTES + 1)
                            oc = classify_file(prefix, st.st_size)
                            if not oc.openable:
                                raise _NotEditable(oc.size, oc.detected_type)
                        os.replace(tmp, target)
                        os.fsync(dir_fd)
                        return os.stat(target).st_mtime_ns
                    finally:
                        fcntl.flock(dir_fd, fcntl.LOCK_UN)
                finally:
                    try:
                        os.unlink(tmp)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(dir_fd)

        try:
            mtime = await asyncio.to_thread(work)
        except _Conflict as exc:
            raise _err_detail(409, {"error": "conflict", "current_mtime_ns": exc.current}) from exc
        except _NotEditable as exc:
            raise not_editable(exc.size, exc.detected) from exc
        except FileNotFoundError as exc:
            raise _err(404, "not_found", "parent directory does not exist") from exc
        except IsADirectoryError as exc:
            raise _err(400, "not_a_file") from exc
        except OSError as exc:
            raise _err(503, "store_error", exc.strerror or "io error") from exc
        finally:
            write_slots[project] -= 1
        return {"path": req.path, "mtime_ns": mtime, "size": len(data)}

    @app.post("/op")
    async def file_op(req: OpRequest) -> dict[str, Any]:
        require_inside_project(req.path)
        # create_leaf=True rejects a symlink leaf so delete/rename never act on its target.
        src = guard(req.path, create_leaf=True)
        dest: Path | None = None
        if req.op == "rename":
            name = req.new_name or ""
            if not name or name in (".", "..") or any(c in name for c in "/\\\x00"):
                raise _err(422, "invalid_name")
            parent = req.path.replace("\\", "/").rstrip("/").rsplit("/", 1)[0]
            dest_rel = f"{parent}/{name}"
            dest = guard(dest_rel, create_leaf=True)
        elif req.op == "move":
            if not req.dest:
                raise _err(422, "dest_required")
            require_inside_project(req.dest)
            if project_of(req.dest) != project_of(req.path):
                raise _err(400, "path_not_permitted", "move must stay within the project")
            dest = guard(req.dest, create_leaf=True)

        def work() -> None:
            if req.op == "create":
                if os.path.lexists(src):
                    raise FileExistsError(str(src))
                if req.is_dir:
                    os.mkdir(src)
                else:
                    fd = os.open(src, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                    os.close(fd)
                return
            if not os.path.lexists(src):
                raise FileNotFoundError(str(src))
            if req.op == "delete":
                if os.path.isdir(src) and not os.path.islink(src):
                    shutil.rmtree(src)
                else:
                    os.unlink(src)
                return
            assert dest is not None
            if os.path.lexists(dest):
                raise FileExistsError(str(dest))
            if os.path.isdir(src) and os.path.commonpath([src, dest]) == os.fspath(src):
                raise ValueError("cannot move a directory into itself")
            os.rename(src, dest)

        try:
            await asyncio.to_thread(work)
        except FileExistsError as exc:
            raise _err(409, "exists") from exc
        except FileNotFoundError as exc:
            raise _err(404, "not_found") from exc
        except ValueError as exc:
            raise _err(400, "invalid_move", str(exc)) from exc
        except OSError as exc:
            raise _err(503, "store_error", exc.strerror or "io error") from exc
        out: dict[str, Any] = {"op": req.op, "path": req.path}
        if req.op == "rename":
            out["path"] = dest_rel
        elif req.op == "move":
            out["path"] = req.dest
        return out

    @app.get("/changes")
    async def changes(path: str, since_ns: int = 0) -> dict[str, Any]:
        """Files under ``path`` modified after ``since_ns`` (external-edit detection)."""
        root_dir = guard(path)

        def work() -> dict[str, Any]:
            found: list[dict[str, Any]] = []
            truncated = False
            base = os.fspath(root_dir)
            for dirpath, dirnames, files in os.walk(base, followlinks=False):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                for name in files:
                    if name.startswith(TMP_PREFIX):
                        continue
                    full = os.path.join(dirpath, name)
                    try:
                        st = os.lstat(full)
                    except OSError:
                        continue
                    if st.st_mtime_ns > since_ns:
                        if len(found) >= CHANGES_LIMIT:
                            truncated = True
                            break
                        found.append({"path": os.path.relpath(full, base).replace(os.sep, "/"),
                                      "mtime_ns": st.st_mtime_ns, "size": st.st_size})
                if truncated:
                    break
            found.sort(key=lambda e: e["path"])
            return {"changed": found, "truncated": truncated, "now_ns": time.time_ns()}

        try:
            return await asyncio.to_thread(work)
        except OSError as exc:
            raise _err(503, "store_error", exc.strerror or "io error") from exc

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Workbench file worker")
    parser.add_argument("--kind", required=True, choices=["opencode", "menagerie"])
    parser.add_argument("--root", default=os.environ.get("WORKBENCH_STORE_ROOT", "/projects"))
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - NetworkPolicy restricts callers
    parser.add_argument("--port", type=int, default=int(os.environ.get("WORKBENCH_FSWORKER_PORT", "8090")))
    args = parser.parse_args()
    uvicorn.run(create_app(args.kind, args.root), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
