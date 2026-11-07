"""Storage metrics for tracking storage operations.

This module provides the StorageMetrics class for tracking read/write/delete
operations and their bytes transferred. Metrics can be exposed to Prometheus
or other monitoring systems via the dump() method.

Metrics tracked:
    - read_objects_total: Number of successful object reads (covers both
      whole-buffer ``get()`` and streaming ``get_stream()``; failures bump
      ``read_errors_total`` instead).
    - read_bytes: Total bytes read (success path only).
    - read_errors_total: Number of read errors.
    - write_objects_total: Number of objects successfully written.
    - write_bytes: Total bytes written (success path only).
    - write_errors_total: Number of write errors.
    - delete_objects_total: Number of objects deleted; a `move` that is not onto itself
      counts one, whether it renamed or copied then deleted.
"""


class StorageMetrics:
    """Metrics container for Storage operations.

    Tracks read, write, and delete operations along with bytes transferred
    and error counts. All counters start at zero and are incremented via
    the *_inc() methods.

    Attributes:
        read_objects_total (int): Number of successful object reads. Covers
            both whole-buffer ``get()`` and streaming ``get_stream()``; only
            the success path increments this counter, failures bump
            ``read_errors_total`` instead.
        read_bytes (int): Total bytes read (success path only).
        read_errors_total (int): Number of read errors.
        write_objects_total (int): Number of objects successfully written.
        write_bytes (int): Total bytes written (success path only).
        write_errors_total (int): Number of write errors.
        delete_objects_total (int): Number of objects deleted; a `move` that is not onto
            itself counts one.
    """

    def __init__(self) -> None:
        """Initialize all metrics to zero."""
        self.read_objects_total = 0
        self.read_bytes = 0
        self.read_errors_total = 0
        self.write_objects_total = 0
        self.write_bytes = 0
        self.write_errors_total = 0
        self.delete_objects_total = 0

    def __repr__(self) -> str:
        """Return a string representation of the metrics."""
        counters = ", ".join(f"{name}={value}" for name, value in self.dump().items())
        return f"StorageMetrics({counters})"

    def dump(self) -> dict[str, int]:
        """Return all metrics as a dictionary.

        Returns:
            Dict[str, int]: All metrics as key-value pairs.
        """
        return {
            "read_objects_total": self.read_objects_total,
            "read_bytes": self.read_bytes,
            "read_errors_total": self.read_errors_total,
            "write_objects_total": self.write_objects_total,
            "write_bytes": self.write_bytes,
            "write_errors_total": self.write_errors_total,
            "delete_objects_total": self.delete_objects_total,
        }

    def read_objects_inc(self) -> None:
        """Increment ``read_objects_total`` by 1.

        Call this only on the success path of a read (after the payload has
        been buffered for ``get()`` or after the stream handle has been
        successfully opened for ``get_stream()``). Failures must bump
        ``read_errors_total`` instead so the two counters stay disjoint.
        """
        self.read_objects_total += 1

    def read_bytes_inc(self, size: int) -> None:
        """Increment read_bytes by the given size.

        Args:
            size (int): Number of bytes to add.
        """
        self.read_bytes += size

    def read_errors_inc(self) -> None:
        """Increment read_errors_total by 1."""
        self.read_errors_total += 1

    def write_objects_inc(self) -> None:
        """Increment write_objects_total by 1."""
        self.write_objects_total += 1

    def write_bytes_inc(self, size: int) -> None:
        """Increment write_bytes by the given size.

        Args:
            size (int): Number of bytes to add.
        """
        self.write_bytes += size

    def write_errors_inc(self) -> None:
        """Increment write_errors_total by 1."""
        self.write_errors_total += 1

    def delete_objects_inc(self) -> None:
        """Increment delete_objects_total by 1."""
        self.delete_objects_total += 1
