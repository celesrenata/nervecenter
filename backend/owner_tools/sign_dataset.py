"""Owner tool: build or sign a dataset manifest (6.2) for the 6.3 benchmark split.

  build: writes the unsigned canonical manifest body for review.
  sign:  the owner records their review and signs with their SSH key (SSHSIG,
         namespace ``nervecenter-dataset-manifest@1``). Run by the owner only:
         signing asserts that *they* reviewed the dataset.

Usage:
  python -m backend.owner_tools.sign_dataset build \
      --out ~/.cache/nervecenter/datasets/bench-v2.unsigned.json
  python -m backend.owner_tools.sign_dataset sign --body <unsigned.json> --reviewer owner \
      --key ~/.ssh/id_ed25519 --out <signed.json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)


def build(out: Path) -> dict:
    from backend.mail_security.dataset_manifest import ExampleMeta, build_manifest
    from backend.mail_security.training_selection import (
        SelectedExample,
        SelectionManifest,
        SourceKind,
        TrainingLabel,
    )
    from backend.owner_tools.model_bench import load_corpus, split_items

    salt = hashlib.sha256(b"nerveshield-bench-6.3").digest()
    cutoff = datetime.fromisoformat("2002-09-01T00:00:00+00:00")
    items, _ = load_corpus(Path("~/.cache/nervecenter/corpora/spamassassin").expanduser(), salt)
    split = split_items(items, cutoff, 7)
    keep = split["fit"] + split["val"] + split["eval"]
    examples = [
        SelectedExample(
            i.eid,
            TrainingLabel.MALICIOUS if i.label else TrainingLabel.BENIGN,
            "public-corpus-label",
            "public-corpus",
            SourceKind.SYNTHETIC,
            i.text,
        )
        for i in keep
    ]
    meta = {i.eid: ExampleMeta(i.when, i.tkey, i.ckey) for i in keep}
    selection = SelectionManifest(
        "public-corpus:spamassassin",
        "public-corpus",
        datetime.now(UTC),
        candidates=len(items),
        included=len(keep),
    )
    body = build_manifest(
        "spamassassin-bench-v2",
        selection,
        examples,
        meta,
        cutoff=cutoff,
        created_at=datetime.now(UTC),
    )
    body["note"] = (
        "Public SpamAssassin corpus, spam/ham proxy labels from the corpus, not human "
        "feedback; label_source below is the manifest default and is overridden here."
    )
    for entry in body["entries"]:
        entry["label_source"] = "PUBLIC_CORPUS_LABEL"
        entry["label_proposal_id"] = "n/a"
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    _write_private(out, text)
    return {
        "path": str(out),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "entries": len(body["entries"]),
        "counts": body["counts"],
        "dropped": body["dropped"],
    }


def sign(body_path: Path, reviewer: str, key: Path, out: Path) -> dict:
    from backend.mail_security.dataset_manifest import (
        HumanReview,
        sign_manifest_ssh,
        verify_manifest_ssh,
    )
    from backend.mail_security.ssh_signing import SshPublicKey, pinned_ssh_keys
    from backend.owner_tools.ssh_signer import SshKeygenSigner

    body = json.loads(body_path.read_text())
    pub_line = Path(f"{os.path.expanduser(os.fspath(key))}.pub").read_text()
    signed = sign_manifest_ssh(
        body,
        HumanReview(reviewer, datetime.now(UTC)),
        SshKeygenSigner(key),
        SshPublicKey.from_line(pub_line),
    )
    verify_manifest_ssh(signed.to_json(), pinned_ssh_keys([pub_line]))
    _write_private(out, signed.to_json())
    return {
        "path": str(out),
        "digest": signed.digest,
        "signing_key_id": signed.body["signing_key_id"],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, required=True)
    s = sub.add_parser("sign")
    s.add_argument("--body", type=Path, required=True)
    s.add_argument("--reviewer", required=True)
    s.add_argument("--key", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    if args.cmd == "build":
        print(json.dumps(build(args.out.expanduser()), indent=1))
    else:
        print(
            json.dumps(
                sign(args.body.expanduser(), args.reviewer, args.key, args.out.expanduser()),
                indent=1,
            )
        )


if __name__ == "__main__":
    main()
