# API

OpenGunstore exposes two HTTP APIs, both served by Frappe on the POS host.

- **Custom RPC endpoints**: every whitelisted method in the three apps, called as `POST /api/method/<dotted.path>`. Browse them in [Redoc](index.html), read the per-module [reference](reference/README.md), or load the [OpenAPI spec](openapi.json). All three are generated from the source by `make api-docs`, and CI fails when they are stale.
- **Frappe's standard REST layer**: read and write any DocType through `/api/resource/<DocType>`. See [Frappe REST](frappe-rest.md).

## Authentication

Use a per-user API key: in the POS, open the user, generate keys under **API Access**, then send `Authorization: token <api_key>:<api_secret>`. A call runs with that user's roles and permissions, so give an integration its own user with only the roles it needs. Session cookies from a signed-in browser work as well.

AI assistants do not use API keys: they connect through the MCP server with OAuth. See [The MCP server](../ai/mcp.md).
