"""Property 14: IDE pod spec invariants.

**Validates: Requirements 5.3, 5.4, 5.5, 5.6, 6.6, 7.4, 7.5, 7.6, 7.11, 7.12, 8.7**
"""
from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from backend.workbench.ide import (
    IPC_SOCKET_PATH,
    KEY_SECRET_KEY,
    NAMESPACE,
    PROJECT_MOUNT,
    IdeConfig,
    IdeProject,
    build_pod_spec,
    pvc_name_for,
)

slugs = st.from_regex(r"[a-z0-9][a-z0-9-]{0,30}", fullmatch=True)
kinds = st.sampled_from(["code", "docs", "notes", "data"])
digests = st.text("0123456789abcdef", min_size=64, max_size=64)
images = st.builds(
    lambda repo, d: f"{repo}@sha256:{d}",
    st.from_regex(r"[a-z][a-z0-9./-]{0,30}", fullmatch=True),
    digests,
)
cpu = st.one_of(
    st.integers(1, 64).map(str), st.integers(1, 4000).map(lambda n: f"{n}m")
)
mem = st.one_of(
    st.integers(1, 64).map(lambda n: f"{n}Gi"),
    st.integers(1, 4096).map(lambda n: f"{n}Mi"),
)
configs = st.builds(
    IdeConfig,
    image=images,
    push_approved=st.booleans(),
    uid=st.integers(1, 65534),
    gid=st.integers(1, 65534),
    cpu_request=cpu,
    memory_request=mem,
    cpu_limit=cpu,
    memory_limit=mem,
)
secret_names = st.from_regex(r"ide-key-[a-z0-9]{1,12}", fullmatch=True)
key_values = st.from_regex(r"sk-[A-Za-z0-9]{24,40}", fullmatch=True)


def _walk(node):
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


@settings(max_examples=100, deadline=None)
@given(slug=slugs, kind=kinds, config=configs, secret=secret_names)
def test_ide_pod_spec_invariants(slug, kind, config, secret):
    pod = build_pod_spec(IdeProject(slug, kind), config, secret)
    spec = pod["spec"]

    assert pod["metadata"]["namespace"] == NAMESPACE
    assert spec["automountServiceAccountToken"] is False

    # Exactly one project volume, of the right kind's PVC, mounted by subPath.
    pvc_vols = [v for v in spec["volumes"] if "persistentVolumeClaim" in v]
    assert len(pvc_vols) == 1
    assert pvc_vols[0]["persistentVolumeClaim"]["claimName"] == pvc_name_for(kind)
    vol_name = pvc_vols[0]["name"]

    # No secret volumes / projected secrets besides none at all.
    for v in spec["volumes"]:
        assert "secret" not in v
        assert "projected" not in v

    assert spec["containers"]
    for c in spec["containers"]:
        mounts = [m for m in c["volumeMounts"] if m["name"] == vol_name]
        assert len(mounts) == 1
        assert mounts[0]["subPath"] == slug
        assert mounts[0]["mountPath"] == PROJECT_MOUNT

        sc = c["securityContext"]
        assert sc["runAsUser"] != 0 and sc["runAsUser"] == config.uid
        assert sc["allowPrivilegeEscalation"] is False
        assert sc["capabilities"]["drop"] == ["ALL"]
        assert sc["readOnlyRootFilesystem"] is True
        assert sc["seccompProfile"]["type"] == "RuntimeDefault"

        for section in ("requests", "limits"):
            for res in ("cpu", "memory"):
                q = c["resources"][section][res]
                assert q and float("".join(ch for ch in q if ch.isdigit() or ch == ".")) > 0

        env = {e["name"]: e for e in c["env"]}
        assert env["ROO_CODE_IPC_SOCKET_PATH"]["value"] == IPC_SOCKET_PATH
        args = c["args"]
        assert args[args.index("--auth") + 1] == "none"

        key_env = env[KEY_SECRET_KEY]
        assert "value" not in key_env
        assert key_env["valueFrom"]["secretKeyRef"]["name"] == secret

        # Only secret reference anywhere is the per-IDE key.
        assert "envFrom" not in c
        refs = [
            n for n in _walk(c) if isinstance(n, dict) and "secretKeyRef" in n
        ]
        assert len(refs) == 1

        assert "@sha256:" in c["image"] and c["image"] == config.image

    # No other secret reference anywhere in the pod.
    assert not [
        n for n in _walk(spec) if isinstance(n, dict) and ("secretRef" in n or "secretName" in n)
    ]
    assert "imagePullSecrets" not in spec


@settings(max_examples=100, deadline=None)
@given(slug=slugs, kind=kinds, config=configs, secret=secret_names, key=key_values)
def test_no_literal_key_value_in_spec(slug, kind, config, secret, key):
    pod = build_pod_spec(IdeProject(slug, kind), config, secret)
    assert key not in json.dumps(pod)
