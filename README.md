# WHOOP connector

A personal MCP server that lets Claude (and the Plate & Progress app) read your WHOOP data:

- `get_daily_energy` — total kcal burned and strain per day
- `get_workouts` — workouts with sport, minutes, kcal and strain

No dependencies beyond the Python standard library, plus `redis` when `REDIS_URL` is set.

## Environment

| Variable | What |
| --- | --- |
| `WHOOP_CLIENT_ID`, `WHOOP_CLIENT_SECRET` | From your app at developer.whoop.com |
| `PUBLIC_URL` | The service's URL, e.g. `https://whoop-connector.onrender.com` |
| `CONNECTOR_KEY` | Long random string. Treat it like a password |
| `REDIS_URL` | Where the WHOOP refresh token is kept |

## Setup

1. In the WHOOP developer dashboard, create an app with redirect URL `PUBLIC_URL/callback` and scopes `read:cycles`, `read:workout`, `offline`.
2. Open `PUBLIC_URL/login?key=CONNECTOR_KEY` and approve.
3. In claude.ai, go to Settings → Connectors → Add custom connector, name it `WHOOP`, URL `PUBLIC_URL/mcp/CONNECTOR_KEY`.

If WHOOP ever says "not connected", repeat step 2.

## Run locally

```bash
CONNECTOR_KEY=dev PORT=8000 python3 server.py
```
