"""Streamed and in-memory digest helpers.

Digest dicts from `ChecksumOpen.get_digests`, `calc_digests`, and `Storage.transfer` share the
key spellings owned by [STORAGE.md](STORAGE.md#digests); `compare_digests` never lets `MD5Sum`
decide a match on its own.
"""

import logging
from hashlib import md5, sha1, sha256, sha512
from typing import IO

# Digest key → hashlib constructor; the one place the algorithms are named.
_ALGORITHMS = {"MD5Sum": md5, "SHA1": sha1, "SHA256": sha256, "SHA512": sha512}

SUPPORT_HASH_LIST = list(_ALGORITHMS)

# `MD5Sum` never decides a comparison on its own -> [STORAGE.md](STORAGE.md#digests).
COMPARE_HASH_LIST = [alg for alg in SUPPORT_HASH_LIST if alg != "MD5Sum"]

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class ChecksumOpen:
    """Class for calculating checksums of file-like objects.

    Attributes:
        size (int): size of file-like object
        fp (object): file-like object
        name (str): name of file-like object
        closed (bool): whether file-like object is closed
    """

    def __init__(self, fp: IO[bytes], name: str | None = None) -> None:
        """Initialize checksum calculators for a file-like object.

        Args:
            fp (IO[bytes]): file-like object
            name (Optional[str]): name of file-like object
        """
        self._hashes = {alg: new() for alg, new in _ALGORITHMS.items()}
        self.size = 0
        self.fp = fp
        self.name = name
        if self.name is None and hasattr(self.fp, "name"):
            self.name = fp.name

        self.closed = False

    def write(self, data: bytes) -> int:
        """Write data to file-like object.

        Args:
            data (bytes): data to write

        Returns:
            int: number of bytes written
        """
        self.fp.write(data)
        self._update(data)
        return len(data)

    def read(self, size: int = -1) -> bytes:
        """Read data from file-like object.

        Args:
            size (int): number of bytes to read; ``-1`` reads to the end, as
                ``IO.read`` does. Streaming callers pass ``CHUNK_SIZE``.

        Returns:
            bytes: data read
        """
        data = self.fp.read(size)
        self._update(data)
        return data

    def _update(self, data: bytes) -> None:
        """Count `data` and feed it to every hash."""
        self.size += len(data)
        for digest in self._hashes.values():
            digest.update(data)

    def get_digests(self) -> dict[str, str]:
        """Get hex digests of file-like object.

        Returns:
            Dict[str, str]: The checksum hexdigests.
        """
        return {
            **{alg: digest.hexdigest() for alg, digest in self._hashes.items()},
            "Size": str(self.size),
        }

    def close(self) -> None:
        """Close file-like object."""
        if not self.closed:
            self.fp.close()
            self.closed = True


def calc_digests(data: bytes) -> dict[str, str]:
    """Get hex digests of memory object.

    Args:
        data (bytes): The data to calculate digests.

    Returns:
        Dict[str, str]: The checksum hexdigests.
    """
    return {
        **{alg: new(data).hexdigest() for alg, new in _ALGORITHMS.items()},
        "Size": str(len(data)),
    }


def compare_digests(expected: dict[str, str], incoming: dict[str, str]) -> bool:
    """Validate two digest dicts.

    `MD5Sum` can refuse a match but never make one: a shared `MD5Sum` that differs
    fails the comparison, and at least one other algorithm must be shared and agree
    -> [STORAGE.md](STORAGE.md#digests).

    Args:
        expected (Dict[str,str]): The first digest to validate.
        incoming (Dict[str,str]): The second digest to validate.

    Returns:
        bool: True if digests are valid, False otherwise.
    """
    if not expected or not incoming:
        logger.debug("Empty digests: expected=%s incoming=%s", expected, incoming)
        return False

    logger.debug("expected: %s", expected)
    logger.debug("incoming: %s", incoming)

    match: bool = False

    for alg in SUPPORT_HASH_LIST:
        if alg in expected and alg in incoming:
            if expected[alg] != incoming[alg]:
                return False
            if alg in COMPARE_HASH_LIST:
                match = True

    return match


def normalize_digests(digests: dict[str, str]) -> dict[str, str]:
    """Canonicalize algorithm names and drop unsupported ones and `MD5Sum`.

    `MD5Sum` is dropped so no skip or verify decision rests on it alone
    -> [STORAGE.md](STORAGE.md#digests).

    Args:
        digests (Dict[str,str]): The digests to normalize.

    Returns:
        Dict[str, str]: The normalized digests.
    """
    up_digests = {k.upper(): v for k, v in digests.items()}
    return {alg: up_digests[alg] for alg in COMPARE_HASH_LIST if alg in up_digests}
