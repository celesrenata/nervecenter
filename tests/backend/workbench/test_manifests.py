"""Manifest and nfs-check.sh tests for the optional Workbench base.

Validates: Requirements 1.3, 1.4, 1.5, 1.10, 6.3, 9.1, 9.11
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WB = ROOT / "deployments" / "kubernetes" / "optional" / "workbench"
K8S = ROOT / "deployments" / "kubernetes"
SCRIPT = WB / "nfs-check.sh"


def _load_all():
    kust = yaml.safe_load((WB / "kustomization.yaml").read_text())
    docs = []
    for res in kust["resources"]:
        docs.extend(d for d in yaml.safe_load_all((WB / res).read_text()) if d)
    return docs


def test_kustomization_renders():
    if shutil.which("kubectl"):
        r = subprocess.run(
            ["kubectl", "kustomize", str(WB)], capture_output=True, text=True
        )
        if r.returncode == 0:
            assert [d for d in yaml.safe_load_all(r.stdout) if d]
            return
    assert _load_all()


def test_pvs_retain_rwx_hard_nfs41():
    pvs = [d for d in _load_all() if d["kind"] == "PersistentVolume"]
    # opencode (ns-less), opencode-ns claim (Phase 2), menagerie
    assert {pv["metadata"]["name"] for pv in pvs} >= {"oc-opencode-projects"}
    assert len(pvs) == 3
    for pv in pvs:
        spec = pv["spec"]
        assert spec["persistentVolumeReclaimPolicy"] == "Retain"
        assert "ReadWriteMany" in spec["accessModes"]
        assert "nfsvers=4.1" in spec["mountOptions"]
        assert "hard" in spec["mountOptions"]


def _pod_specs(doc):
    if doc["kind"] == "Pod":
        yield doc["spec"]
    elif "template" in doc.get("spec", {}):
        yield doc["spec"]["template"]["spec"]


def test_no_pod_mounts_both_stores():
    docs = _load_all()
    stores = {
        d["metadata"]["name"]: d["metadata"]["labels"]["workbench.nervcenter/store"]
        for d in docs
        if d["kind"] == "PersistentVolumeClaim"
    }
    assert set(stores.values()) == {"opencode", "menagerie"}
    for d in docs:
        for spec in _pod_specs(d):
            claims = {
                stores[v["persistentVolumeClaim"]["claimName"]]
                for v in spec.get("volumes", [])
                if "persistentVolumeClaim" in v
                and v["persistentVolumeClaim"]["claimName"] in stores
            }
            assert len(claims) <= 1, d["metadata"]["name"]


OPENCODE_DATA_SHA256 = (
    "53f583a04ad61f2fe8b91e194abe5990a153eea8c0434e2412caafc091e66289"
)


def test_opencode_data_pvc_unchanged_and_not_redefined():
    """Req 1.5, 1.9: the existing Longhorn PVC is untouched by the Workbench."""
    import hashlib

    mirror = ROOT / "docs" / "central-opencode" / "pvc.yaml"
    assert hashlib.sha256(mirror.read_bytes()).hexdigest() == OPENCODE_DATA_SHA256
    doc = yaml.safe_load(mirror.read_text())
    assert doc["metadata"]["name"] == "opencode-data"
    assert doc["spec"]["storageClassName"] == "longhorn"
    for d in _load_all():
        assert d["metadata"]["name"] != "opencode-data"


def test_workbench_claims_use_static_nfs_pvs():
    claims = {
        d["metadata"]["name"]: d
        for d in _load_all()
        if d["kind"] == "PersistentVolumeClaim"
    }
    assert claims["oc-opencode-projects"]["metadata"]["namespace"] == "opencode"
    assert claims["oc-opencode-projects"]["spec"]["storageClassName"] == ""


def test_base_kustomization_does_not_reference_workbench():
    for p in [K8S / "kustomization.yaml", *(K8S / "base").glob("kustomization.y*ml")]:
        if p.exists():
            assert "workbench" not in p.read_text().lower(), p


def test_no_new_ingress():
    for d in _load_all():
        assert d["kind"] != "Ingress"


def _run(env_extra):
    env = {
        **os.environ,
        "WORKBENCH_UID": str(os.getuid()),
        "WORKBENCH_GID": str(os.getgid()),
        "WORKBENCH_NFS_MOUNT": "0",
        **env_extra,
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
    )


def test_nfs_check_passes_on_temp_dirs(tmp_path):
    a, b = tmp_path / "o", tmp_path / "m"
    a.mkdir()
    b.mkdir()
    r = _run({"WORKBENCH_OPENCODE_PATH": str(a), "WORKBENCH_MENAGERIE_PATH": str(b)})
    assert r.returncode == 0, r.stderr
    assert list(a.iterdir()) == [] and list(b.iterdir()) == []


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores directory permissions")
def test_nfs_check_fails_naming_export_when_unwritable(tmp_path):
    a, b = tmp_path / "o", tmp_path / "m"
    a.mkdir()
    b.mkdir()
    b.chmod(0o555)
    try:
        r = _run(
            {"WORKBENCH_OPENCODE_PATH": str(a), "WORKBENCH_MENAGERIE_PATH": str(b)}
        )
    finally:
        b.chmod(0o755)
    assert r.returncode != 0
    assert "menagerie" in r.stderr


# ---- Phase 3 (Req 1.5, 6.3, 7.12) ----
def _all_with_patches():
    kust = yaml.safe_load((WB / "kustomization.yaml").read_text())
    docs = _load_all()
    for p in kust.get("patches", []):
        docs.extend(d for d in yaml.safe_load_all((WB / p["path"]).read_text()) if d)
    return docs


def test_no_cluster_scoped_rbac():
    for d in _all_with_patches():
        assert d["kind"] not in ("ClusterRole", "ClusterRoleBinding")


def test_role_verbs_are_exact_and_not_wildcard():
    roles = [d for d in _load_all() if d["kind"] == "Role"]
    assert roles
    allowed = {
        "pods": {"get", "list", "watch", "create", "delete"},
        "secrets": {"get", "create", "delete"},
        "events": {"list"},
    }
    seen = set()
    for role in roles:
        for rule in role["rules"]:
            assert "*" not in rule["verbs"]
            assert "*" not in rule["resources"]
            assert "*" not in rule.get("apiGroups", [])
            for res in rule["resources"]:
                assert res in allowed, res
                assert set(rule["verbs"]) <= allowed[res], res
                seen.add(res)
    assert seen == set(allowed)


def test_no_pod_template_mounts_both_stores_with_patches():
    docs = _all_with_patches()
    stores = {
        d["metadata"]["name"]: d["metadata"]["labels"]["workbench.nervcenter/store"]
        for d in docs
        if d["kind"] == "PersistentVolumeClaim"
    }
    for d in docs:
        for spec in _pod_specs(d):
            claims = {
                stores[v["persistentVolumeClaim"]["claimName"]]
                for v in spec.get("volumes", [])
                if v.get("persistentVolumeClaim", {}).get("claimName") in stores
            }
            assert len(claims) <= 1, d["metadata"]["name"]


def test_no_ingress_resource_anywhere_in_workbench_dir():
    for d in _all_with_patches():
        assert d["kind"] != "Ingress"
    for p in WB.glob("*.y*ml"):
        for d in yaml.safe_load_all(p.read_text()):
            if d:
                assert d.get("kind") != "Ingress", p


def _netpols(name):
    return [
        d for d in _load_all()
        if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == name
    ]


def test_ide_ingress_only_from_api():
    (pol,) = _netpols("ide-ingress-from-api")
    assert pol["spec"]["policyTypes"] == ["Ingress"]
    assert pol["spec"]["podSelector"]["matchLabels"] == {"app": "ide"}
    assert len(pol["spec"]["ingress"]) == 1
    (src,) = pol["spec"]["ingress"][0]["from"]
    assert src["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "nervecenter"
    }
    assert src["podSelector"]["matchLabels"] == {"app": "api"}
    assert "ipBlock" not in src


def test_ide_egress_excludes_private_and_link_local():
    (pol,) = _netpols("ide-egress")
    assert pol["spec"]["policyTypes"] == ["Egress"]
    blocks = [
        t["ipBlock"]
        for rule in pol["spec"]["egress"]
        for t in rule["to"]
        if "ipBlock" in t
    ]
    assert blocks
    required = {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"}
    for b in blocks:
        assert b["cidr"] == "0.0.0.0/0"
        assert required <= set(b["except"])
