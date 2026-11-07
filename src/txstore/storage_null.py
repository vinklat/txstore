"""StorageNull: write-only ``/dev/null``-style storage backend.

Accepts writes (and bumps metrics) but discards the data. Every read /
list / copy / move / mkdir is inherited from `WriteOnlyStorageMixin`
and raises `NotImplementedError`. Transaction methods raise
`NotImplementedError` via `TransactionNotSupportedMixin`.

Useful for:

    - Testing write pipelines without actual storage
    - Benchmarking write performance
    - "Compute digests during streaming without keeping a copy" --
      `repo_sync`'s checksum-only verify path.
"""

from __future__ import annotations

import builtins
import logging
from typing import IO

from txstore._capabilities import TransactionNotSupportedMixin, WriteOnlyStorageMixin
from txstore.base import CHUNK_SIZE, Storage, StorageException, StorageType

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class StorageNull(TransactionNotSupportedMixin, WriteOnlyStorageMixin, Storage):
    """Write-only storage backend that discards all data (``/dev/null``-like).

    Implements `put_stream` (and so `put`), `delete`, `add_metadata`,
    `delete_metadata`, `delete_metadata_fields`, `format`, `rmdir` as
    no-ops (with metrics where applicable). Every read/list/copy/move/
    mkdir method is inherited from `WriteOnlyStorageMixin` and raises
    `NotImplementedError`.
    """

    def __init__(self) -> None:
        """Initialize the StorageNull instance."""
        super().__init__()
        logger.debug("Init null storage class.")

    def __repr__(self) -> str:
        """Return a string representation of the storage."""
        return f"{self.get_type()}"

    def get_type(self) -> StorageType:
        """Return the storage backend type identifier."""
        return StorageType.null

    def put_stream(
        self,
        path: str,
        stream: IO[bytes],
        metadata: dict[str, str] | None = None,
        acl: str | None = None,
    ) -> None:
        """Drain `stream` to its end, counting the bytes it held; bump metrics.

        Raises:
            StorageException: If reading the stream fails.
        """
        del acl
        logger.debug("(no effect) writing file (stream) %s", path)
        nbytes = 0

        try:
            # Draining is what a digest-only transfer needs: the source hashes as it is read.
            while chunk := stream.read(CHUNK_SIZE):
                nbytes += len(chunk)
        except Exception as exc:
            # Only the source stream raises, e.g. urllib3 under an HTTP transfer.
            self.metrics.write_errors_inc()
            raise StorageException(
                f"Error reading the stream for null object {path}: {exc}"
            ) from exc

        if metadata is not None:
            self.add_metadata(path, metadata)

        self.metrics.write_bytes_inc(nbytes)
        self.metrics.write_objects_inc()

    def add_metadata(
        self,
        path: str,
        metadata: dict[str, str],
        acl: str | None = None,
    ) -> None:
        """No-op: accepts and discards `metadata`/`acl`."""
        del acl
        logger.debug("(no effect) set metadata keys %s on %s", sorted(metadata), path)

    def delete(self, path: str) -> None:
        """No-op delete; bumps `delete_objects_total`."""
        logger.debug("(no effect) delete object %s...", path)
        self.metrics.delete_objects_inc()

    def delete_metadata(self, path: str) -> None:
        """No-op: null storage has no metadata to remove."""
        logger.debug("(no effect) delete metadata from object %s...", path)

    def delete_metadata_fields(self, path: str, keys: builtins.list[str]) -> None:
        """No-op: null storage has no metadata to remove."""
        logger.debug("(no effect) delete metadata %s from object %s...", keys, path)

    def format(self) -> None:
        """No-op: nothing to reset."""
        logger.debug("(no effect) reset null storage.")

    def rmdir(self, path: str) -> None:
        """No-op: null storage has no directories."""
        logger.debug("(no effect) rmdir %s...", path)
