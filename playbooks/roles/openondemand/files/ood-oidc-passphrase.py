#!/usr/bin/env python3
"""Create a persistent local OIDC secret without printing its value."""
import fcntl
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile


def ensure_passphrase(path):
    path = Path(path)
    with open(str(path) + ".lock", "a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.is_symlink():
            raise ValueError("Persisted OIDC passphrase must not be a symlink")
        if path.exists():
            if not re.fullmatch(r"[0-9a-f]{64}\n?", path.read_text()):
                raise ValueError("Invalid persisted OIDC passphrase; restore the saved secret")
            os.chmod(path, 0o600)
            return False
        fd, temporary = tempfile.mkstemp(prefix=".oidc-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as output:
                output.write(secrets.token_hex(32) + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True


if __name__ == "__main__":
    print("created" if ensure_passphrase(sys.argv[1]) else "existing")
