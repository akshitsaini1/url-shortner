# Shortener HTTP API

| Method and path | Result |
|---|---|
| `POST /urls` | Accepts an HTTP(S) `url` and optional integer `expires_in_seconds` from 1 through 31,536,000. Returns 201 with `code`, normalized `url` and nullable UTC Unix timestamp `expires_at`. |
| `GET /{code}` | Returns a 302 with the original URL in `Location`, 404 for an unknown code, or 410 at and after expiration. |
| `GET /urls/{code}/analytics` | Returns link metadata and the number of successful redirects, including for expired links. Unknown codes return 404. |

Invalid request bodies return 422. Redirects send `Cache-Control: no-store`. The service stores destinations without fetching them. FastAPI exposes OpenAPI at `/openapi.json`.
