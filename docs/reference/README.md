# Reference artifacts

Non-canonical reference material for contributors and tooling. Root contracts and the
implementation remain authoritative.

## Sources

| Source | Description |
| -- | -- |
| [footmark.proto](footmark.proto) | Protocol-buffers schema of the footmark wire format; `txstore` encodes it without a protobuf runtime → [STORAGE.md](../../STORAGE.md#footmarks). Never change field numbers or types: stored footmarks depend on them. |
