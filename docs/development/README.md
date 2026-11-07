# Developer guidance

Non-canonical explanations for contributors: code maps, transaction and lock flows, and the
reasoning behind the rules. Root contract documents remain authoritative; when a page here conflicts
with a root contract, update or remove the page. The user-facing guides are in
[docs/guides/](../guides/README.md).

## Page template

Every page opens with one paragraph: what the page explains, which contract sections own the
behavior, and the guide it complements. Sections follow in this order: `Code map`
(`Concern | Module | Notes`), a flow section (one diagram per path, followed by a bullet list headed
"Details that matter"), domain-specific sections, `Testing notes`, and `Related contracts` ending
with a `Sibling pages:` bullet. [class.md](class.md) is diagrams plus commentary rather than a flow
and says so in its preamble.

Diagram conventions: PlantUML only, one source per `diagrams/<page>-<name>.puml` embedded as the
rendered `.svg`; rendering and the freshness check → [SETUP.md](../../SETUP.md#diagrams). A
sequence diagram shows one call path; an activity diagram shows a decision tree. The backend
participant is `database Backend` (filesystem or bucket).

## Pages

| Page | Description |
| -- | -- |
| [architecture.md](architecture.md) | Module graph, import directions, load-time cycles, where configuration is read. |
| [class.md](class.md) | Class model: `Storage`, `FootmarkMixin`, capability stubs, backends. |
| [transactions.md](transactions.md) | Begin, commit, revert, and discard flows; `maybe_transaction` with history and auto-prune. |
| [s3.md](s3.md) | The boto3 client wrapper, ACL sidecar, metadata rewrites, error classification, the S3 lock. |
| [footmark.md](footmark.md) | Footmark tokens and paths, the wire codec, the timestamp text form, and why the last two are frozen. |
