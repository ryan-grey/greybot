"""NAS-owned audit copies. No application delete, edit, or retention API.

Filesystem administrators retain full restore/removal control. This is not
storage-enforced retention, and is intentionally not described as immutable.
"""
import os
import tempfile
from pathlib import Path

from .store import canonical, digest


class LocalArchive:
    def __init__(self, directory):
        self.directory = Path(directory)

    def check(self):
        if not self.directory.is_dir() or self.directory.is_symlink():
            raise RuntimeError("NAS audit archive mount is unavailable")

    def flush(self, store):
        self.check()
        pending = store.pending()
        if not pending:
            return
        if not store.verify():
            raise RuntimeError("Journal integrity check failed")
        for row in pending:
            body = canonical(row).encode()
            hashed = digest(body.decode())
            key = f"{row['seq']:020d}-{hashed}.json"
            target = self.directory / key
            # Write/fsync fully before linking into place. Never overwrite an
            # existing event, including after a crash before receipt creation.
            fd, name = tempfile.mkstemp(prefix=".pending-", dir=self.directory)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(body)
                    output.flush()
                    os.fchmod(output.fileno(), 0o440)
                    os.fsync(output.fileno())
                try:
                    os.link(name, target)
                except FileExistsError:
                    pass
                if target.is_symlink() or target.read_bytes() != body:
                    raise RuntimeError("NAS archive conflicts with journal")
                directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                store.receipt(row["seq"], key, "sha256:" + hashed)
            finally:
                os.unlink(name)
