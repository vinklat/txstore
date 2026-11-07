"""Post-write public-ACL probe for `StorageS3`.

`is_public` reads an object's live grants, retried with back-off; exhaustion raises
`StorageException` rather than reporting a missing grant → [STORAGE.md](STORAGE.md#s3-acl-rules).
"""

from __future__ import annotations

import logging
import time

from txstore._s3_client import S3_BACKEND_ERRORS, S3Client, grants_all_users_read
from txstore.base import StorageException

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# One sleep between each pair of attempts → STORAGE.md#s3-acl-rules.
_ACL_PROBE_BACKOFF_S = (0.5, 1.0)
_ACL_PROBE_ATTEMPTS = len(_ACL_PROBE_BACKOFF_S) + 1


def is_public(s3_client: S3Client, bucket: str, full_key: str) -> bool:
    """Return True iff the live ACL on `full_key` grants AllUsers READ.

    Retries a backend error of the ``object_get_acl`` call up to
    ``_ACL_PROBE_ATTEMPTS`` times with back-off before giving up; any other
    exception is a bug and propagates at once.

    Raises:
        StorageException: On backend errors after all retries.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _ACL_PROBE_ATTEMPTS + 1):
        try:
            grants = s3_client.object_get_acl(bucket, full_key)
            break
        except S3_BACKEND_ERRORS as exc:
            last_exc = exc
            if attempt < _ACL_PROBE_ATTEMPTS:
                logger.warning(
                    "ACL probe for %s failed (attempt %d/%d): %s -- retrying",
                    full_key,
                    attempt,
                    _ACL_PROBE_ATTEMPTS,
                    exc,
                )
                time.sleep(_ACL_PROBE_BACKOFF_S[attempt - 1])
    else:
        raise StorageException(f"Error reading ACL {full_key}: {last_exc}") from last_exc

    if grants_all_users_read(grants):
        return True
    # Grantee type and permission only: owner grants carry account IDs and display names.
    shape = [(g.get("Grantee", {}).get("Type"), g.get("Permission")) for g in grants]
    logger.debug("S3 object %s is not public: grants=%s", full_key, shape)
    return False
