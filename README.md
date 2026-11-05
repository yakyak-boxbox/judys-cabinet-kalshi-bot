# Judy's Cabinet — Kalshi flat-20 bot (cloud deploy)

Runs [kalshi_bot.py](./kalshi_bot.py) 24/7 on Railway — no local computer,
no terminal, no Python install. This repo exists only as the source for
Judy's Cabinet's "Deploy on Railway" button; if you're not deploying via
that button, the plain [kalshi_bot.py](./kalshi_bot.py) + `pip install -r
requirements.txt` + `python3 kalshi_bot.py` works anywhere Python 3 runs.

**Self-custodial.** Your Kalshi API key and funds live only in your own
Railway project's environment variables and your own Kalshi account. Judy's
Cabinet never sees or holds either.

Full setup instructions, the risk disclosure, and your personal signal
token: **https://app.judyscabinet.com/setup** (Discord login required).
