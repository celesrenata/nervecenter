"""Owner-side SSH signer for dataset manifests (NerveShield).

Runs ``ssh-keygen -Y sign`` in the dedicated namespace. Lives outside
``backend.mail_security`` on purpose: mail-processing code must stay free of
subprocess and network primitives (enforced by tests). Nothing in the backend
imports this module; the owner calls it from their own machine.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from backend.mail_security.ssh_signing import NAMESPACE, SshSignatureError, armor_decode


@dataclass(frozen=True)
class SshKeygenSigner:
    """Signs via ``ssh-keygen -Y sign``. ``key_path`` is the private key path, or
    its ``.pub`` when the key is held by ssh-agent. The file is never read here."""

    key_path: Path
    namespace: str = NAMESPACE
    timeout_s: float = 30.0

    def sign(self, message: bytes) -> str:
        exe = shutil.which("ssh-keygen")
        if exe is None:
            raise SshSignatureError("ssh-keygen not found")
        path = Path(os.path.expanduser(os.fspath(self.key_path)))
        try:
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [exe, "-Y", "sign", "-q", "-n", self.namespace, "-f", str(path)],
                input=message,
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
                stdin=None,
            )
        except subprocess.TimeoutExpired:
            raise SshSignatureError("ssh-keygen timed out (passphrase prompt?)") from None
        if result.returncode != 0:
            raise SshSignatureError("ssh-keygen could not sign (key locked or not loaded?)")
        text = result.stdout.decode()
        armor_decode(text)  # sanity
        return text.strip() + "\n"


__all__ = ["SshKeygenSigner"]
