"""Property 8: File operations match a model filesystem.

**Validates: Requirements 4.4**
"""
from __future__ import annotations

import os
import shutil
import tempfile

from fastapi.testclient import TestClient
from hypothesis import HealthCheck, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from backend.workbench import fsworker

PROJECT = "proj"
_names = st.sampled_from(["a", "b", "c", "d.txt", "e.py"])


def _under(prefix: str, path: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


class FsOpsMachine(RuleBasedStateMachine):
    """Model: dict of project-relative path -> is_dir. The root "" is an implicit dir."""

    def __init__(self) -> None:
        super().__init__()
        self.root = tempfile.mkdtemp(prefix="wb-ops-")
        os.makedirs(os.path.join(self.root, PROJECT))
        self.client = TestClient(fsworker.create_app("menagerie", self.root))
        self.model: dict[str, bool] = {}

    def teardown(self) -> None:
        self.client.close()
        shutil.rmtree(self.root, ignore_errors=True)

    # -- helpers --
    def _dirs(self) -> list[str]:
        return [""] + sorted(p for p, is_dir in self.model.items() if is_dir)

    def _all(self) -> list[str]:
        return sorted(self.model)

    @staticmethod
    def _join(parent: str, name: str) -> str:
        return f"{parent}/{name}" if parent else name

    def _op(self, **body):
        return self.client.post("/op", json=body)

    @staticmethod
    def _full(rel: str) -> str:
        return f"{PROJECT}/{rel}"

    # -- rules --
    @rule(data=st.data(), name=_names, is_dir=st.booleans())
    def create(self, data, name: str, is_dir: bool) -> None:
        parent = data.draw(st.sampled_from(self._dirs()))
        rel = self._join(parent, name)
        r = self._op(op="create", path=self._full(rel), is_dir=is_dir)
        if rel in self.model:
            assert r.status_code == 409
        else:
            assert r.status_code == 200, r.text
            self.model[rel] = is_dir

    @rule(data=st.data(), new_name=_names)
    def rename(self, data, new_name: str) -> None:
        if not self.model:
            return
        src = data.draw(st.sampled_from(self._all()))
        parent = src.rsplit("/", 1)[0] if "/" in src else ""
        dest = self._join(parent, new_name)
        r = self._op(op="rename", path=self._full(src), new_name=new_name)
        if dest in self.model:
            assert r.status_code == 409
        else:
            assert r.status_code == 200, r.text
            assert r.json()["path"] == self._full(dest)
            self._relocate(src, dest)

    @rule(data=st.data(), name=_names)
    def move(self, data, name: str) -> None:
        if not self.model:
            return
        src = data.draw(st.sampled_from(self._all()))
        dest_parent = data.draw(st.sampled_from(self._dirs()))
        dest = self._join(dest_parent, name)
        r = self._op(op="move", path=self._full(src), dest=self._full(dest))
        if dest in self.model:
            assert r.status_code == 409
        elif self.model[src] and _under(src, dest):
            assert r.status_code == 400
        else:
            assert r.status_code == 200, r.text
            self._relocate(src, dest)

    @rule(data=st.data())
    def delete(self, data) -> None:
        if not self.model:
            return
        target = data.draw(st.sampled_from(self._all()))
        r = self._op(op="delete", path=self._full(target))
        assert r.status_code == 200, r.text
        for p in [p for p in self.model if _under(target, p)]:
            del self.model[p]

    @rule(name=_names)
    def delete_missing(self, name: str) -> None:
        if name in self.model:
            return
        r = self._op(op="delete", path=self._full(name))
        assert r.status_code == 404

    def _relocate(self, src: str, dest: str) -> None:
        moved = {p: v for p, v in self.model.items() if _under(src, p)}
        for p in moved:
            del self.model[p]
        for p, v in moved.items():
            self.model[dest + p[len(src):]] = v

    # -- invariants --
    @invariant()
    def disk_matches_model(self) -> None:
        # Nothing exists outside the project root.
        assert os.listdir(self.root) == [PROJECT]
        base = os.path.join(self.root, PROJECT)
        actual: dict[str, bool] = {}
        for dirpath, dirnames, files in os.walk(base):
            for n in dirnames + files:
                full = os.path.join(dirpath, n)
                assert not os.path.islink(full)
                actual[os.path.relpath(full, base).replace(os.sep, "/")] = os.path.isdir(full)
        assert actual == self.model

    @invariant()
    def list_matches_model(self) -> None:
        for d in self._dirs():
            r = self.client.get("/list", params={"path": self._full(d).rstrip("/")})
            assert r.status_code == 200, r.text
            got = {e["name"]: e["is_dir"] for e in r.json()["entries"]}
            prefix = d + "/" if d else ""
            want = {
                p[len(prefix):]: v
                for p, v in self.model.items()
                if p.startswith(prefix) and "/" not in p[len(prefix):]
            }
            assert got == want


FsOpsMachine.TestCase.settings = settings(
    max_examples=40,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
TestFsOpsMatchModel = FsOpsMachine.TestCase
