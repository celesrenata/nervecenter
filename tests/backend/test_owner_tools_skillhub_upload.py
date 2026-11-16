"""Owner tool: Skill Hub upload (NS-ADR-112)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import httpx
import yaml

from backend.agents.skillpack import build_pack
from backend.owner_tools import skillhub_upload as tool

KEY = "super-secret-priv-key"


class FakeRun:
    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self.fail = fail

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        self.inputs.append(kw.get("input"))
        if self.fail and self.fail in " ".join(argv):
            return subprocess.CompletedProcess(argv, 1, "", "")
        out = ""
        if "jsonpath={.data.key}" in argv:
            import base64

            out = base64.b64encode(KEY.encode()).decode()
        elif any("containers[?" in a for a in argv):
            out = "registry.celestium.life/nervecenter/api:abc123"
        return subprocess.CompletedProcess(argv, 0, out, "")


def test_plan_has_one_subpath_mount_and_writes_nothing(capsys) -> None:
    run = FakeRun()
    assert tool.main(["plan"], run=run, env={}) == 0
    out = capsys.readouterr().out
    assert "192.168.42.8:/volume1/Kubernetes/omniroute/skills" in out
    assert "nervecenter/planner/SKILL.md" in out and "api:abc123" in out
    assert KEY not in out
    assert all(c[:2] != ["kubectl", "apply"] and "delete" not in c for c in run.calls)

    job = yaml.safe_load(tool.render_job("img:1"))
    spec = job["spec"]["template"]["spec"]
    mounts = spec["containers"][0]["volumeMounts"]
    writable = [m for m in mounts if not m.get("readOnly")]
    assert [(m["mountPath"], m["subPath"]) for m in writable] == [
        ("/skills/nervecenter", "nervecenter")
    ]
    nfs = [v["nfs"] for v in spec["volumes"] if "nfs" in v]
    assert nfs == [{"server": "192.168.42.8", "path": "/volume1/Kubernetes/omniroute/skills"}]
    assert job["spec"]["backoffLimit"] == 2 and job["spec"]["activeDeadlineSeconds"] == 120
    assert spec["imagePullSecrets"] == [{"name": "harbor-pull"}]


def test_configmap_covers_the_pack_and_the_script_stays_in_its_folder() -> None:
    cm = tool.render_configmap()
    pack = build_pack()
    assert {f"{s}.SKILL.md" for s in pack} <= set(cm["data"])
    assert {"pack-manifest.json", "upload.sh"} <= set(cm["data"])
    script = cm["data"]["upload.sh"]
    assert "/skills/nervecenter" in script
    assert "/skills/" not in script.replace("/skills/nervecenter", "")
    assert "rm -rf" not in script
    assert not Path(tool.TEMPLATE_DIR / "kustomization.yaml").exists()


def test_apply_without_approval_refuses_and_calls_nothing(capsys) -> None:
    run = FakeRun()
    assert tool.main(["apply"], run=run, env={}) != 0
    assert run.calls == []
    assert "--approved" in capsys.readouterr().err


def test_apply_approved_runs_configmap_then_job_then_waits() -> None:
    run = FakeRun()
    assert tool.main(["apply", "--approved", "--image", "img:2"], run=run, env={}) == 0
    verbs = [" ".join(c[:2] + c[-2:]) for c in run.calls]
    assert run.calls[0] == ["kubectl", "apply", "-f", "-"]
    assert "ConfigMap" in (run.inputs[0] or "")
    assert "delete" in run.calls[1] and tool.JOB in run.calls[1]
    assert "img:2" in (run.inputs[2] or "")
    assert "wait" in run.calls[3], verbs
    assert tool.main(["apply", "--approved", "--image", "i"], run=FakeRun(fail="wait"), env={}) == 1


def test_refresh_and_verify_use_the_key_without_printing_it(capsys) -> None:
    seen: list[httpx.Request] = []
    pack = build_pack()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"success": True})
        results = [{"id": f"imported:nervecenter/{s}", "name": f"nervecenter-{s}"} for s in pack]
        return httpx.Response(200, json={"results": results})

    transport = httpx.MockTransport(handler)
    env = {tool.KEY_ENV: KEY}
    assert tool.main(["refresh"], run=FakeRun(), env=env, transport=transport) == 0
    assert tool.main(["verify"], run=FakeRun(), env=env, transport=transport) == 0
    assert seen[0].url.path == "/api/skills/catalog/refresh" and seen[0].method == "POST"
    assert seen[1].url.params["domain"] == "nervecenter"
    assert all(r.headers["authorization"] == f"Bearer {KEY}" for r in seen)
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err


def test_refresh_and_verify_reach_every_replica(capsys) -> None:
    from contextlib import contextmanager

    pack = build_pack()
    refreshed: set[str] = set()

    class PodRun(FakeRun):
        def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            if "pods" in argv:
                return subprocess.CompletedProcess(argv, 0, "omni-b omni-a", "")
            return super().__call__(argv, **kw)

    @contextmanager
    def forward(pod: str):  # type: ignore[no-untyped-def]
        yield f"http://{pod}.test"

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.method == "POST":
            refreshed.add(host)
            return httpx.Response(200, json={"success": True})
        names = [f"nervecenter-{s}" for s in pack] if host in refreshed else []
        if host == "omni-b.test":
            names.append("nervecenter-research-agent")
        return httpx.Response(200, json={"results": [{"name": n, "id": n} for n in names]})

    transport = httpx.MockTransport(handler)
    env = {tool.KEY_ENV: KEY}
    assert tool.main(["verify"], run=PodRun(), env=env, transport=transport, forward=forward) == 1
    assert tool.main(["refresh"], run=PodRun(), env=env, transport=transport, forward=forward) == 0
    assert refreshed == {"omni-a.test", "omni-b.test"}
    assert tool.main(["verify"], run=PodRun(), env=env, transport=transport, forward=forward) == 1
    out = capsys.readouterr().out
    assert "not in pack: nervecenter-research-agent" in out and KEY not in out


def test_verify_reports_missing_ids_and_key_comes_from_the_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    assert tool.main(["verify"], run=FakeRun(), env={}, transport=httpx.MockTransport(handler)) == 1
    assert (
        tool.main(
            ["verify"],
            run=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""),
            env={},
            transport=httpx.MockTransport(handler),
        )
        == 1
    )


def _run_script(tmp_path: Path, pack: dict[str, str]) -> None:
    pack_dir, dest = tmp_path / "pack", tmp_path / "skills" / "nervecenter"
    dest.mkdir(parents=True, exist_ok=True)
    pack_dir.mkdir(exist_ok=True)
    for old in pack_dir.iterdir():
        old.unlink()
    for slug, text in pack.items():
        (pack_dir / f"{slug}.SKILL.md").write_text(text)
    (pack_dir / "pack-manifest.json").write_text(tool.manifest_text(pack))
    script = (tool.TEMPLATE_DIR / "upload.sh").read_text()
    script = script.replace("DEST=/skills/nervecenter", f"DEST={dest}").replace(
        "PACK=/pack", f"PACK={pack_dir}"
    )
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_upload_script_is_idempotent_and_removes_only_its_own_stale_slugs(tmp_path: Path) -> None:
    pack = build_pack()
    other = tmp_path / "skills" / "devops" / "keep" / "SKILL.md"
    other.parent.mkdir(parents=True)
    other.write_text("not ours")
    _run_script(tmp_path, pack)
    dest = tmp_path / "skills" / "nervecenter"
    assert (dest / "planner" / "SKILL.md").read_text() == pack["planner"]
    (dest / "planner" / "SKILL.md").write_text("drifted")
    foreign = dest / "foreign" / "SKILL.md"
    foreign.parent.mkdir()
    foreign.write_text("someone else's")
    _run_script(tmp_path, pack)  # re-run: updated in place
    assert (dest / "planner" / "SKILL.md").read_text() == pack["planner"]
    smaller = {k: v for k, v in pack.items() if k != "planner"}
    _run_script(tmp_path, smaller)  # planner was ours and is no longer in the pack
    assert not (dest / "planner").exists() and (dest / "finance" / "SKILL.md").exists()
    assert foreign.read_text() == "someone else's" and other.read_text() == "not ours"
