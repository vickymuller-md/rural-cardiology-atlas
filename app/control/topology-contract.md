# Census 2025 display topology contract

Version 0.2.0 includes the two controlled Census 2025-derived display-topology
assets at these exact paths under `app/public/data/topology/`:

| Asset | Bytes | SHA-256 | Object | Features |
|---|---:|---|---|---:|
| `counties-2025-5m.topo.json` | 1,349,047 | `affdae57c1fe33dd5e64ccf1c735f83db830374d5dcff375231a667aa96aa4d5` | `counties` | 3,144 |
| `states-2025-5m.topo.json` | 465,345 | `05a7896dc0cd3be17813dbe7b571e74eafd690a6078a99698aa9a52df01b6bbe` | `states` | 51 |

The app loads only these same-origin paths and validates byte length, SHA-256,
object name, string feature IDs, exact equality with the analytical county/state
IDs, current Connecticut planning regions, and absence of obsolete Connecticut
county IDs. The assets are display-only and cannot assign providers or hospitals,
define the county universe, or calculate distance.

An absent or mismatched version-bound asset fails prebuild/runtime verification
and never triggers a CDN request.
