"""Shared helpers for tests that build signed /v1/code bundles."""

import base64
import io
import zipfile
from pathlib import Path

from jarvis import deploy


def make_tree(root, files):
    for name, data in files.items():
        path = Path(root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def pack(entries, code, signer, version=None):
    """A /v1/code bundle. `version` set means version + vsig, the same keys code_bundle serves."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    raw = buffer.getvalue()
    bundle = {"code": code, "zip": base64.b64encode(raw).decode("ascii"),
              "signature": deploy.sign(raw, signer)}
    if version is not None:
        bundle["version"] = version
        bundle["vsig"] = deploy.sign(deploy._bundle_bytes(raw, version, code), signer)
    return bundle
