# Vendored assets — provenance

| file | what | version | sha256 | source |
|---|---|---|---|---|
| `redoc.standalone.js` | Redoc standalone bundle (MIT, © Rebilly) | 2.5.0 | `0ec05be285ac885a330289b02f470e1bdbd2b6b3223a9fa213f24bf805a851d1` | https://cdn.jsdelivr.net/npm/redoc@2.5.0/bundles/redoc.standalone.js |
| `redoc.standalone.js.LICENSE.txt` | Third-party license notices bundled by Redoc | 2.5.0 | `1b18b986225f8a85fa7fcaf191f0118bb297ba8c6f027b669e4b79828c9c17ed` | https://cdn.jsdelivr.net/npm/redoc@2.5.0/bundles/redoc.standalone.js.LICENSE.txt |
| `redoc.LICENSE` | Redoc's own MIT license text | 2.5.0 | — | https://cdn.jsdelivr.net/npm/redoc@2.5.0/LICENSE |
| `init.js` | Our Redoc bootstrap (external because index.html's CSP has no 'unsafe-inline') | — | — | this repo |

Refresh / upgrade with `scripts/fetch_redoc.sh <version>` — it downloads the
pinned files and prints their sha256; update this table in the same commit.

The bundle is loaded by `docs/api/index.html`, whose Content-Security-Policy
pins the page to `'self'` — a bundle upgrade that starts phoning home will be
blocked by the browser, not silently allowed.
