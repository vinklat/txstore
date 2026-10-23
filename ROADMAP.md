# txstore Roadmap

## Phases

| Phase | Status | Feature | Notes |
| -- | -- | -- | -- |
| 0.1.0 | ⏳ | Initial public release | fs, S3, HTTP, and null backends; transactions, history, and retention; protobuf-free footmark codec; configuration by arguments or `TX_STORE_*` variables; Debian testing floors |
| 0.2.0 | ⏳ | Debian package for forky | `python3-txstore` built with `dh-python` and `pybuild-plugin-pyproject`, autopkgtest running pytest, DEP-14 `debian/latest` branch, `debian/watch` on GitHub tags; ITP, sponsor or Debian Python Team, upload to unstable, migrate to testing before the forky freeze |
| 1.0.0 | ⏳ | Stable API | Freeze the façade before the forky freeze so forky ships an API maintained for the whole release |

## Ideas

Unscheduled improvements, each waiting on a trigger. Promote a row to a phase when its trigger fires
or the work is planned; drop it when it no longer applies.

| Idea | Revisit when | Area |
| -- | -- | -- |

## Known deviations

Implementation that does not yet meet a root contract. Contracts link here instead of carrying
TODOs; remove a row when the code meets the rule.

| Contract | Deviation | Source |
| -- | -- | -- |
