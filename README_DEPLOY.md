# SPY 0DTE Terminal — Deploy Runbook

Three moving parts:

```
[ main.py worker ]  --writes-->  [ MongoDB Atlas ]  <--reads--  [ server.py API ]  <--polls--  [ scanner_dashboard.html on GitHub Pages ]
```

The browser only ever talks to `server.py`. **No DB credentials ever go into the HTML.**

---

## 0. FIRST: rotate your Mongo password
You pasted your live password into a chat. Treat it as compromised.
Atlas → **Database Access** → edit `andresmercado1919_db_user` → **Edit Password** → generate a new one.
Then grab the real connection string: Atlas → **Connect** → **Drivers** → copy the `mongodb+srv://…` URI
(the `cluster0.xxxxx` in your message is a placeholder; the real host looks like `cluster0.ab1cd.mongodb.net`).
Under **Network Access**, add `0.0.0.0/0` (or Render's egress IPs) so Render can connect.

## 1. Get the free API keys
- **FlashAlpha:** https://flashalpha.com/profile → sign up (no card) → copy key. Free tier = **5 requests/day** (that's why GEX is cached; leave `GEX_REFRESH_SECONDS=14400`).
- **Anthropic:** https://console.anthropic.com → API key (narrative only; set `ENABLE_LLM_NARRATIVE=false` to skip and avoid all LLM cost).

## 2. Local test (2 minutes)
```bash
cd scanner-terminal
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in MONGODB_URI + FLASHALPHA_API_KEY (+ANTHROPIC_API_KEY)
set -a; source .env; set +a
RUN_ONCE=true python main.py       # runs one cycle, prints/writes the payload
uvicorn server:app --reload        # then open http://localhost:8000/api/spy
```

## 3. Deploy on Render
1. Push this folder to `github.com/amercado19/scanner-terminal`.
2. Render → **New → Blueprint** → pick the repo (it reads `render.yaml`).
3. It creates two services: `spy-terminal-api` (web) and `spy-terminal-worker`.
4. In each service → **Environment**, paste the `sync:false` secrets: `MONGODB_URI`, `FLASHALPHA_API_KEY`, `ANTHROPIC_API_KEY`.
5. Copy the API service URL (e.g. `https://spy-terminal-api.onrender.com`).

> **Free-tier reality:** Render's free plan does NOT run an always-on worker, and free web services sleep after ~15 min idle (first request then takes ~30s to wake). For a true 24/7 loop the worker needs a paid **Starter** plan (~$7/mo), or run `python main.py` yourself (a local machine / cron / a cheap VPS). The web API can stay on free if you tolerate cold starts.

## 4. Wire up the dashboard
1. Edit `scanner_dashboard.html`: set `const API_BASE = "https://spy-terminal-api.onrender.com";`
2. Commit & push. GitHub Pages serves it at
   `https://amercado19.github.io/scanner-terminal/scanner_dashboard.html`.
3. Open on your phone — it polls every 30s and updates in place.

## 5. Verify end-to-end
- `curl https://spy-terminal-api.onrender.com/api/spy` returns the JSON doc.
- Atlas → Collections → `spy_terminal_db.spy_payloads` shows `_id: spy_live_data` with a recent `updated_at`.
- Dashboard status dot is green ("live"); values change across refreshes.

---

### Reality checks (read before trusting this with money)
- **The rule engine is a heuristic, not a proven edge.** Backtest it (you already have a backtesting setup in this project) before risking capital.
- **yfinance is delayed ~15 min.** Fine for a structure dashboard; wrong for real 0DTE execution timing. Swap `fetch_spy_bars()` for a real feed (Polygon, Databento, your broker) when you go live.
- **The LLM narrative is commentary only.** It never sets the bias/action.
