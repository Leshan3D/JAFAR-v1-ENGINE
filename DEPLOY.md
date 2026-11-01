# Deploy to Railway

Runs the whole engine (pipeline + dashboard + Telegram alerts + bot) as one
container. Local behavior is unchanged — `python3 run_service.py` still works
on a desktop.

## 1. Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → pick a name
   (e.g. `JAFAR Intel`). Copy the token.
2. Open a chat with your new bot and send `/start` — the service adopts that
   chat automatically and all alerts go there. (Or find your numeric chat id
   with @userinfobot and set `TELEGRAM_CHAT_ID`.)

## 2. Push the code

The repo already has the Dockerfile. The container builds itself — no runner
config needed.

```
git add -A
git commit -m "deploy: add Railway container + Telegram alerts/bot"
git push
```

## 3. Create the Railway service

1. Railway dashboard → **New Project** → **Deploy from GitHub repo**
   → pick `JAFAR/TradingPipeline`.
2. Railway detects the `Dockerfile` automatically and builds it.

## 4. Environment (Variables tab)

| Var | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from §1 |
| `TELEGRAM_CHAT_ID` | optional, §1 |
| `FRED_API_KEY` | optional — falls back to the built-in key |
| `PIPELINE_INTERVAL_S` | `3600` (default) |
| `PRICE_REFRESH_INTERVAL_S` | `900` (default) |
| `ZONE_POLL_INTERVAL_S` | `60` (default) |
| `OPENAI_API_KEY` | optional, enables free-text LLM chat in the bot |

The dashboard listens on `$PORT` (Railway injects it — nothing to set).

## 5. Volume (optional, recommended)

Service → **Volumes** → mount at `/data`. Persists snapshots/signals/alert
state across redeploys. **Note:** Railway volumes need Hobby+; on Trial the
container still works, it just regenerates data at boot and forgets alert
cooldowns on redeploy.

## 6. Deploy

After first build, the service:

1. Runs the full pipeline immediately (~2–5 min: news, FinBERT, FRED,
   Polymarket, Fear & Greed).
2. Serves the dashboard on the Railway URL (`/`).
3. Refreshes prices every 15 min, digests everything hourly.
4. Watches entry zones every 60s and pings Telegram on zone crossings and
   action flips.

## Rate-limit budget (why the cadence is safe)

| API | Budget | Our usage |
|---|---|---|
| Binance (spot) | 1200 weight/min | klines 2/15 min + ticker ~3/min → trivial |
| yfinance | unofficial, throttles | 6 quotes /15 min (stocks only in US session for zone watch) |
| FRED | 120 req/min | 6 series / hour |
| Polymarket | generous REST | ~10 searches / hour (built-in 0.5s gaps) |
| GDELT / RSS | hard backoff built in | news only on the hourly pass |
| alternative.me F&G | ~10/min | 1 / hour |

If you push it harder, ASK_RES the pipeline first ran with ≥2s sleeps between
assets precisely to survive these free tiers.

## Local test before pushing

```
docker build -t trading-pipeline .
docker run --rm -p 5050:5000 -e PORT=5000 -e DATA_DIR=/data \
  -e PIPELINE_INTERVAL_S=60 -e PRICE_REFRESH_INTERVAL_S=60 \
  -v "$PWD"/docker_test:/data trading-pipeline
# dashboard: http://localhost:5050
# then Ctrl+C — snapshots/signals land in ./docker_test
```

## FAQ

- **Is my local setup affected?** No. Data defaults to "next to the scripts";
  the env vars only change behavior inside the container.
- **Bot chat the way I talk to llm?** The bot answers structured commands
  (`/signals`, `/signal zec`, `/price btc`, `/news gold`, `/run`). With
  `OPENAI_API_KEY` set, free text goes to an LLM that has the current
  signals.json as context — it can reason about positions but cannot modify
  the engine or trade for you.
- **Why no CRON?** The intervals are configurable; one long-lived process is
  simpler on Railway than a scheduler and keeps state in one place.