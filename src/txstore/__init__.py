"""Public façade of the `txstore` package.

Import from `txstore` only; submodules are not a supported surface.

Concrete backends and helpers:

    - StorageFS   : Filesystem storage with full transaction support.
    - StorageS3   : S3 object storage with full transaction support.
    - StorageHTTP : Read-only HTTP storage (no write/transaction support).
    - StorageNull : Write-only /dev/null-like storage (no read/transaction support).

Transactions are exposed through `FootmarkMixin` (mixed into FS/S3).
Backends that do not support them still expose the `transaction_*` names
as `NotImplementedError` stubs; use `is_transactional()` as the capability
test.
"""

from importlib.metadata import version as _pkg_version

from txstore._s3_client import BucketCannedACL, ObjectCannedACL
from txstore._timestamp import Timestamp
from txstore.base import (
    CHUNK_SIZE,
    REQUEST_TIMEOUT,
    Storage,
    StorageException,
    StorageNotFoundException,
    StorageType,
)
from txstore.factory import NO_ACL, make_storage
from txstore.footmark import ObjectOperation
from txstore.hexdigest import (
    COMPARE_HASH_LIST,
    SUPPORT_HASH_LIST,
    ChecksumOpen,
    calc_digests,
    compare_digests,
    normalize_digests,
)
from txstore.history import (
    HISTORY_KEEP_DEFAULT,
    HISTORY_MAX_AGE_DAYS_DEFAULT,
    HistoryRetention,
    PruneResult,
    TransactionHistory,
    history_lock,
    maybe_transaction,
)
from txstore.metrics import StorageMetrics
from txstore.storage_fs import StorageFS
from txstore.storage_http import StorageHTTP
from txstore.storage_null import StorageNull
from txstore.storage_s3 import StorageS3, parse_bucket_acl, parse_object_acl
from txstore.transaction import (
    FootmarkMixin,
    TransactionModel,
    is_transactional,
)
from txstore.url import (
    get_supported_schemes,
    is_unsafe_path,
    join_url,
    parse_storage_url,
    redact_url,
)

__version__ = _pkg_version("txstore")

__all__ = [
    "CHUNK_SIZE",
    "COMPARE_HASH_LIST",
    "HISTORY_KEEP_DEFAULT",
    "HISTORY_MAX_AGE_DAYS_DEFAULT",
    "NO_ACL",
    "REQUEST_TIMEOUT",
    "SUPPORT_HASH_LIST",
    "BucketCannedACL",
    "ChecksumOpen",
    "FootmarkMixin",
    "is_transactional",
    "maybe_transaction",
    "HistoryRetention",
    "ObjectCannedACL",
    "ObjectOperation",
    "PruneResult",
    "Storage",
    "StorageException",
    "StorageFS",
    "StorageHTTP",
    "StorageMetrics",
    "StorageNotFoundException",
    "StorageNull",
    "StorageS3",
    "StorageType",
    "Timestamp",
    "TransactionHistory",
    "TransactionModel",
    "calc_digests",
    "compare_digests",
    "get_supported_schemes",
    "history_lock",
    "is_unsafe_path",
    "join_url",
    "make_storage",
    "normalize_digests",
    "parse_bucket_acl",
    "parse_object_acl",
    "parse_storage_url",
    "redact_url",
]
