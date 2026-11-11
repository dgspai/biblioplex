# Security

## Threat model

Biblioplex is built for a single user or a trusted LAN.

- The stack binds to `127.0.0.1` by default (`BIND_ADDR` in `.env`). Nothing is reachable from other machines until you change it.
- Mutating endpoints (`POST`/`DELETE` for imports, decks, inventory, `/admin/refresh`) have **no authentication**, by design. Anyone who can reach port 8000 or 8080 can change or delete your data.
- CORS is off by default (`CORS_ORIGINS` is empty); the web UI reaches the API same-origin through nginx `/api/`. List origins in `CORS_ORIGINS` (comma-separated) only for a separately hosted front end.
- Cross-site write guard: `POST`/`PUT`/`PATCH`/`DELETE` (REST and `/mcp`) that carry an `Origin` header are rejected with `403` unless the origin is the request's own host:port or is listed in `CORS_ORIGINS`. Requests with no `Origin` (curl, MCP clients, scripts) pass. This stops a web page in your browser from writing to `localhost`. It is not authentication: any non-browser client can omit or forge `Origin`, so it does not replace auth when the API is exposed. `CORS_ORIGINS=*` disables the guard.
- `/mcp` exposes 76 tools, 20 of which write data, including `delete_deck`, `clear_inventory_collection` and `clear_wishlist_collection`. Set `MCP_AUTH_TOKEN` whenever `/mcp` is reachable by anything other than a trusted local client; clients then must send `Authorization: Bearer <token>`.
- `MCP_AUTH_TOKEN` protects only `/mcp`. The REST API (port 8000) and the web proxy (port 8080, `/api/`) stay unauthenticated, so the same writes are reachable over REST. That is why the `127.0.0.1` default and a reverse proxy with auth matter.
- Outbound network calls: on every api start it calls 3 Scryfall catalog endpoints, refreshes also fetch Scryfall bulk data and Commander Spellbook, and the embedding model loads from the local cache, so HuggingFace is contacted only when the model isn't cached (normally the first start). It needs no inbound access.

## Exposing it safely

Keep `BIND_ADDR=127.0.0.1` and put a reverse proxy that does authentication and TLS in front. Minimal Caddyfile (Caddy v2.8+; older v2 releases spell `basic_auth` as `basicauth`) with basic auth for the UI and API, and the bearer token alone for MCP:

```
biblioplex.example.com {
    @mcp path /api/mcp /api/mcp/*

    # MCP clients: no basic auth, only MCP_AUTH_TOKEN (Bearer).
    handle @mcp {
        reverse_proxy 127.0.0.1:8080
    }

    # Everything else: basic auth.
    handle {
        basic_auth {
            # generate with: caddy hash-password
            alice <bcrypt-hash>
        }
        reverse_proxy 127.0.0.1:8080
    }
}
```

Why the split: basic auth and the MCP bearer token both use the `Authorization` header, so a client can't send both, and one route behind `basic_auth` makes `/api/mcp` return 401. MCP therefore gets its own route without `basic_auth`, protected by `MCP_AUTH_TOKEN` alone (clients connect to `https://biblioplex.example.com/api/mcp` with `Authorization: Bearer <token>`).

**Set `MCP_AUTH_TOKEN` before you use this.** With it empty, the `@mcp` route is open to the whole internet, and 20 of the MCP tools write or delete data. The REST API under `/api/` stays behind basic auth.

The web container proxies the API under `/api/`, so exposing only port 8080 through the proxy is enough for the UI.

Setting `BIND_ADDR=0.0.0.0` publishes both ports on every interface with no authentication. Only do that on a network you trust.

## Reporting a vulnerability

Use GitHub private vulnerability reporting: the repository's **Security** tab, then **Report a vulnerability**. Please do not open a public issue for security problems. Expect an acknowledgement within a week.
