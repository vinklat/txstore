"""Package contracts: public surface, runtime dependencies, and import directions.

Source is walked with `ast`, never imported for the boundary checks →
[TESTING.md](../TESTING.md#package-contracts), [STRUCTURE.md](../STRUCTURE.md#dependencies).
"""

from __future__ import annotations

import ast
import importlib.metadata
import re
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

import txstore

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "txstore"

_EXPECTED_ALL = sorted(
    [
        "BucketCannedACL",
        "CHUNK_SIZE",
        "COMPARE_HASH_LIST",
        "ChecksumOpen",
        "FootmarkMixin",
        "HISTORY_KEEP_DEFAULT",
        "HISTORY_MAX_AGE_DAYS_DEFAULT",
        "HistoryRetention",
        "NO_ACL",
        "ObjectCannedACL",
        "ObjectOperation",
        "PruneResult",
        "REQUEST_TIMEOUT",
        "SUPPORT_HASH_LIST",
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
        "is_transactional",
        "is_unsafe_path",
        "join_url",
        "make_storage",
        "maybe_transaction",
        "normalize_digests",
        "parse_bucket_acl",
        "parse_object_acl",
        "parse_storage_url",
    ]
)

# Top-level module names a runtime import may use; distributions are asserted below.
_THIRD_PARTY_RUNTIME = {"boto3", "botocore", "requests", "xattr"}

# Modules that import no other txstore module.
_LEAVES = {
    "_capabilities",
    "_env",
    "_footmark_wire",
    "_s3_client",
    "_timestamp",
    "hexdigest",
    "metrics",
}
_BACKENDS = {"storage_fs", "storage_http", "storage_null", "storage_s3"}
# Importers allowed to name a backend module: the façade and the factory.
_BACKEND_IMPORTERS = {"__init__", "factory"}
# Core and Transactions rows of STRUCTURE.md#dependencies: the txstore modules each listed module
# may import at run time; `history` is held by deny rules instead, since its row lists no allowlist.
_ALLOWED_IMPORTS = {
    "base": {"hexdigest", "metrics"},
    "url": {"base"},
    "_reserved": {"base"},
    "_fs_paths": {"base"},
    "_s3_acl": {"base", "_s3_client"},
    "_s3_errors": {"base", "_s3_client"},
    "_s3_lock": {"base", "_s3_client", "_s3_errors"},
    "footmark": {"_footmark_wire", "_timestamp"},
    # `transaction` imports `_reclaim`, so `_reclaim` names `transaction` for types only.
    "_reclaim": {"footmark", "_reserved"},
    "transaction": {"base", "footmark", "_reserved", "_reclaim"},
}
# Function-level imports allowed, each breaking a named load-time cycle → STRUCTURE.md.
_DEFERRED_ALLOWED: set[tuple[str, str]] = set()


class _Import:
    def __init__(
        self,
        module: str,
        top: str,
        deferred: bool,
        type_only: bool,
        names: tuple[str, ...] = (),
    ) -> None:
        self.module = module
        self.top = top
        self.deferred = deferred
        self.type_only = type_only
        # Names a `from module import ...` binds; empty for a plain `import`.
        self.names = names


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(_SRC.glob("*.py")):
        yield path.stem, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(tree: ast.Module) -> Iterator[_Import]:
    type_only: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            type_only.update(id(child) for child in ast.walk(node))
    top_level = {id(node) for node in tree.body}
    top_level |= type_only
    for node in ast.walk(tree):
        modules: list[str] = []
        bound: tuple[str, ...] = ()
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = [node.module]
            bound = tuple(alias.name for alias in node.names)
        for name in modules:
            yield _Import(
                module=name,
                top=name.split(".")[0],
                deferred=id(node) not in top_level,
                type_only=id(node) in type_only,
                names=bound,
            )


def _internal(module: str) -> str | None:
    """Return the txstore submodule `module` names, ``"txstore"`` for the façade, else None."""
    if module == "txstore":
        return "txstore"
    if module.startswith("txstore."):
        return module.split(".")[1]
    return None


# Surface


def test_facade_all_is_the_expected_surface() -> None:
    assert sorted(txstore.__all__) == _EXPECTED_ALL


@pytest.mark.parametrize("name", _EXPECTED_ALL)
def test_every_exported_name_resolves(name: str) -> None:
    assert getattr(txstore, name) is not None


def test_version_is_read_from_the_distribution_and_not_exported() -> None:
    assert txstore.__version__ == importlib.metadata.version("txstore")
    assert "__version__" not in txstore.__all__


def test_package_ships_type_information() -> None:
    assert (_SRC / "py.typed").is_file()


# Dependencies


def test_runtime_imports_are_stdlib_or_declared_dependencies() -> None:
    allowed = (
        set(sys.stdlib_module_names)
        | _THIRD_PARTY_RUNTIME
        | {
            "txstore",
            "__future__",
        }
    )
    offenders = [
        f"{name}: {imp.module}"
        for name, tree in _modules()
        for imp in _imports(tree)
        if not imp.type_only and imp.top not in allowed
    ]
    assert offenders == []


def test_declared_dependencies_match_runtime_imports() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = {dep.split(">")[0].split("<")[0].split("=")[0] for dep in project["dependencies"]}
    assert declared == {"boto3", "botocore", "pyxattr", "requests"}


def test_only_env_module_reads_the_environment() -> None:
    readers = set()
    for name, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv", "environb"}:
                readers.add(name)
    assert readers == {"_env"}


# Import directions


def test_leaf_modules_import_no_txstore_module() -> None:
    offenders = [
        f"{name}: {imp.module}"
        for name, tree in _modules()
        if name in _LEAVES
        for imp in _imports(tree)
        if not imp.type_only and _internal(imp.module)
    ]
    assert offenders == []


def test_submodules_never_import_the_facade() -> None:
    offenders = [
        name
        for name, tree in _modules()
        if name != "__init__"
        for imp in _imports(tree)
        if _internal(imp.module) == "txstore"
    ]
    assert offenders == []


def test_backends_are_imported_only_by_facade_and_factory() -> None:
    offenders = [
        f"{name}: {imp.module}"
        for name, tree in _modules()
        if name not in _BACKEND_IMPORTERS
        for imp in _imports(tree)
        if not imp.type_only and _internal(imp.module) in _BACKENDS
    ]
    assert offenders == []


@pytest.mark.parametrize(("module", "allowed"), sorted(_ALLOWED_IMPORTS.items()))
def test_modules_import_only_their_row(module: str, allowed: set[str]) -> None:
    tree = dict(_modules())[module]
    imported = {
        _internal(imp.module)
        for imp in _imports(tree)
        if not imp.type_only and _internal(imp.module) is not None
    }
    assert imported <= allowed


def test_history_imports_no_s3_module() -> None:
    """The reserved-object ACL belongs to `StorageS3`, so history stays backend-neutral."""
    history = dict(_modules())["history"]
    assert [
        imp.module for imp in _imports(history) if (_internal(imp.module) or "").startswith("_s3")
    ] == []


def test_deferred_imports_only_break_named_cycles() -> None:
    deferred = {
        (name, _internal(imp.module) or imp.module)
        for name, tree in _modules()
        for imp in _imports(tree)
        if imp.deferred and not imp.type_only
    }
    assert deferred == _DEFERRED_ALLOWED


# Test imports → TESTING.md#harness-and-gates

_TESTS = _ROOT / "tests"
# Test files that cover no single module: contract surfaces over several backends, and harness
# checks → TESTING.md#file-layout. Every other `test_<m>.py` covers `txstore.<m>`.
_COVERS_NOTHING = {
    "test_transactions",
    "test_fs_transactions",
    "test_s3_transactions",
    "test_package",
    "test_harness",
    "test_check_docs",
    "test_render_diagrams",
}
# Private names a file may take from a module it does not cover, each with its reason.
_IMPORT_EXCEPTIONS: dict[str, dict[str, set[str]]] = {
    # The designated re-exporter of the reserved names, and the stub's commit hook type.
    "helpers/storage": {
        "_reserved": {"HISTORY_FILENAME", "LOCK_FILENAME"},
        "transaction": {"_CommitResult"},
    },
    # Crafts malformed tokens the public API cannot mint.
    "test_footmark": {"_footmark_wire": {"FootmarkFields", "encode"}},
}


def _test_files() -> Iterator[tuple[str, ast.Module]]:
    """Yield ``(key, tree)`` for every Python file under `tests/`, keyed by relative stem."""
    for path in sorted(_TESTS.rglob("*.py")):
        key = path.relative_to(_TESTS).with_suffix("").as_posix()
        yield key, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _covered(key: str) -> set[str]:
    """The txstore module a test file covers: `test_<m>.py` covers `<m>` or the private `_<m>`."""
    if key in _COVERS_NOTHING or not key.startswith("test_"):
        return set()
    stem = key.removeprefix("test_")
    return {m for m in (stem, f"_{stem}") if (_SRC / f"{m}.py").exists()}


def _test_import_offenders(key: str, tree: ast.Module) -> list[str]:
    covered = _covered(key)
    allowed = _IMPORT_EXCEPTIONS.get(key, {})
    public = set(txstore.__all__)
    offenders: list[str] = []
    for imp in _imports(tree):
        module = _internal(imp.module)
        if module is None:
            continue
        if module == "txstore":
            # `from txstore import _x` names a private module, not a façade name.
            offenders += [
                f"{key}: txstore.{name}"
                for name in imp.names
                if name.startswith("_") and name not in covered
            ]
            continue
        for name in imp.names or ("",):
            if name in public:
                offenders.append(f"{key}: {imp.module}.{name} is in the façade")
            elif module not in covered and name not in allowed.get(module, set()):
                offenders.append(f"{key}: {imp.module}{'.' + name if name else ''}")
    return offenders


def test_tests_import_only_their_module_and_the_facade() -> None:
    """Façade names come from `txstore`; a submodule only for the module a file covers."""
    offenders = [o for key, tree in _test_files() for o in _test_import_offenders(key, tree)]
    assert offenders == []


def test_the_test_import_gate_flags_a_facade_name_from_a_submodule() -> None:
    tree = ast.parse("from txstore.history import TransactionHistory, _MAX_TXN_ID\n")
    assert _test_import_offenders("test_history", tree) == [
        "test_history: txstore.history.TransactionHistory is in the façade"
    ]
    assert _test_import_offenders("test_url", tree) == [
        "test_url: txstore.history.TransactionHistory is in the façade",
        "test_url: txstore.history._MAX_TXN_ID",
    ]


# Exceptions gate → TESTING.md#harness-and-gates


def _raised_classes(source: str) -> set[str]:
    """Names passed alone as the first argument of `pytest.raises(...)` calls.

    Walks the AST, so a commented-out or quoted `pytest.raises(X)` does not count. A tuple
    credits no class: `pytest.raises((A, B))` passes when either is raised.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "raises"
            and isinstance(func.value, ast.Name)
            and func.value.id == "pytest"
        ):
            continue
        first = node.args[0]
        if isinstance(first, ast.Name):
            names.add(first.id)
    return names


def test_raised_classes_ignores_comments_and_tuples() -> None:
    source = "# pytest.raises(A)\nx = 'pytest.raises(B)'\npytest.raises((C, D))\npytest.raises(E)\n"
    assert _raised_classes(source) == {"E"}


def test_every_documented_exception_is_raised_by_a_test() -> None:
    """Each class in the EXCEPTIONS.md table is the first argument of some `pytest.raises`."""
    table = (_ROOT / "EXCEPTIONS.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| `(\w+Exception)` \|", table, flags=re.MULTILINE))
    raised: set[str] = set()
    for path in (_ROOT / "tests").rglob("test_*.py"):
        raised |= _raised_classes(path.read_text(encoding="utf-8"))
    assert documented
    assert documented - raised == set()
