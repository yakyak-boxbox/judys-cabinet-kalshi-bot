#!/usr/bin/env python3
"""
Kalshi flat-20 "honest slate" bot — PAPER by default.

Runs the late-favorite method on Kalshi 15-minute crypto markets (KXBTC15M /
KXETH15M): in the final minutes of each window, buy the side whose ask sits in
the builder band and clears the per-asset floor, size a FIXED 20% of bankroll,
and grade on settlement.

This mirrors flat_sim() in flipsim.py — the survivable, never-bust sizing — not
an all-in roll.

WHY PAPER FIRST:
  * Every market-data endpoint Kalshi exposes is PUBLIC. No API key, no signing,
    and no third-party deps are needed to read markets or results. Only PLACING
    an order (or reading your OWN balance/positions) needs auth — that lives
    behind EXEC_LIVE=1 and needs `pip install cryptography`.
  * The liquidity gate is built in: a real losing contract with no bid settles
    to ~$0 (a ~100% loss on that stake), not whatever an optimistic stop would
    suggest. Both bankroll curves are tracked side by side in paper mode so the
    gap is visible, not hidden:
      * stop@STOP_FRAC — optimistic; assumes a bid existed to cut the loss
      * hold2settle     — realistic; losers ride to $0 with STOP disabled

HONEST DISCLOSURE — READ BEFORE SETTING EXEC_LIVE=1:
  * STOP_ENABLED (default ON, STOP_FRAC default 0.50) places a REAL sell
    order to cut a losing live position once it's down STOP_FRAC from entry,
    using the current best bid. This is a real order and can itself fail to
    fill (same timing risk as any live order) — if it misses, the bot keeps
    retrying each poll until it either fills or the window closes and the
    position rides to settlement normally. Set STOP_ENABLED=0 to go back to
    pure ride-to-settlement (flat-sizing-as-the-only-risk-control) behavior.
  * This is YOUR Kalshi account, YOUR funds, YOUR API key. This tool never
    sends your key or your money anywhere else. Nobody else can see your
    balance or your trades. You are fully responsible for what it does with
    your account. There is no guarantee of profit — read SETUP.md first.
  * The historical win-rate this strategy was backtested against was graded
    on a Chainlink/Polymarket-style 60-second-TWAP settlement, NOT Kalshi's
    actual CF Benchmarks BRTI settlement. Real Kalshi performance is tracked
    in kalshi_live_audit.jsonl and should be compared against that baseline
    over time — it is not yet formally proven equivalent.
  * Kalshi's 15m crypto markets have can_close_early=true — a market can
    stop accepting new orders before its listed close_time, often right as
    the price move that triggered a signal happens. A real order attempt can
    come back HTTP 410 Gone even with time left on the clock. The bot
    refetches the book once and retries against the current price before
    giving up (see the 410 branch in run()); if Kalshi closed the market
    before that retry too, it's logged as "market_closed_early" and recorded
    paper-only for that window — a known Kalshi behavior, not a bug.

Run:   python3 kalshi_bot.py            # paper, both assets, 24/7
Env:   ASSETS=BTC,ETH  FRAC=0.20  STOP_FRAC=0.30  START_BANK=100
       BAND_LO=0.70 BAND_HI=0.95  FLOOR_BTC=0.70 FLOOR_ETH=0.80
       ENTRY_TTE_LO=120 ENTRY_TTE_HI=180   # enter when 120s<=time-left<=180s
       POLL_SEC=5  LEDGER=kalshi_paper.jsonl  AUDIT_LOG=kalshi_live_audit.jsonl
       EXEC_LIVE=0                           # 1 = place REAL orders (needs key)
       KALSHI_API_KEY_ID=...  KALSHI_PRIVATE_KEY_PATH=...   # only for EXEC_LIVE=1
       ALERT_WEBHOOK_URL=...                 # optional: Discord webhook for circuit-breaker alerts
"""
import os, sys, json, time, uuid, subprocess, urllib.request, urllib.error
from datetime import datetime, timezone

# Force unbuffered/line-buffered stdout. When stdout is redirected to a file
# (e.g. `nohup python3 kalshi_bot.py > kalshi_bot.log`), Python defaults to
# block buffering — print() output can sit unwritten for a long time, and a
# hard `kill` (not a clean KeyboardInterrupt) loses whatever's still
# buffered. That previously made it impossible to tell, after the fact,
# whether the bot actually saw a signal and failed to act on it, or never
# saw it at all. Fixed here instead of relying on launch flags (-u) being
# remembered every time this gets started.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass


def _load_dotenv(path=".env"):
    """Tiny stdlib .env loader — no dependency needed. Real environment
    variables always win (so `EXEC_LIVE=1 python3 kalshi_bot.py` still works
    to override a .env file). Silently does nothing if .env doesn't exist."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            # Strip a trailing inline comment (" # ..."), but only when it's
            # preceded by whitespace so a value that legitimately contains
            # "#" isn't mangled.
            if " #" in v:
                v = v.split(" #", 1)[0].rstrip()
            if k and k not in os.environ:
                os.environ[k] = v


_load_dotenv()

BASE = "https://api.elections.kalshi.com/trade-api/v2"
UA = "judys-cabinet-kalshi-bot/1.0"

SERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M"}

def _env(k, d): return os.environ.get(k, d)
ASSETS      = [a.strip().upper() for a in _env("ASSETS", "BTC,ETH").replace("/", ",").split(",") if a.strip()]
FRAC        = float(_env("FRAC", "0.20"))
STOP_FRAC   = float(_env("STOP_FRAC", "0.50"))
START_BANK  = float(_env("START_BANK", "100"))
BAND_LO     = float(_env("BAND_LO", "0.70"))
BAND_HI     = float(_env("BAND_HI", "0.95"))
FLOORS      = {"BTC": float(_env("FLOOR_BTC", "0.70")), "ETH": float(_env("FLOOR_ETH", "0.80"))}
ENTRY_TTE_LO = float(_env("ENTRY_TTE_LO", "120"))
ENTRY_TTE_HI = float(_env("ENTRY_TTE_HI", "180"))
# Real orders only: pad the limit price up to this much past the quoted ask so a
# small price move between our book snapshot and the order reaching Kalshi still
# fills (a limit order is a ceiling, not a fixed price -- Kalshi still fills at
# the best available price if it's better than this, so padding never means
# paying more than necessary). Paper/backtest bookkeeping still uses the
# unpadded quoted ask, since that's what's being graded against the strategy.
FILL_SLIPPAGE = float(_env("FILL_SLIPPAGE", "0.05"))
# If the first entry attempt is accepted but gets zero fill (price moved before
# the order landed), retry ONCE at the fresh current price -- can be better OR
# worse than the original quote, a limit order only pays what's needed to
# cross either way. Only retry if the fresh price hasn't dropped below this
# floor; below it, treat the setup as having changed too much to chase rather
# than blindly following the price down.
RETRY_FLOOR = float(_env("RETRY_FLOOR", "0.60"))
POLL_SEC    = float(_env("POLL_SEC", "5"))
LEDGER      = _env("LEDGER", "kalshi_paper.jsonl")
AUDIT_LOG   = _env("AUDIT_LOG", "kalshi_live_audit.jsonl")
EXEC_LIVE   = _env("EXEC_LIVE", "0") == "1"
# Real stop-loss, default ON at -50%: a handful of live trades that would
# have settled as wins got wiped out by later losses riding all the way to
# $0 -- ETH especially moves fast enough that -50% is a real, meaningfully
# different outcome from a full loss, not just noise. When a real position
# exists (live_order + a real filled count), this now places a REAL sell
# order to close it once the mark-to-market loss hits STOP_FRAC, using the
# same current best bid logic as the paper comparison below. Set
# STOP_ENABLED=0 to go back to the old ride-to-settlement-only behavior.
STOP_ENABLED = _env("STOP_ENABLED", "1") == "1"
# If a real stop-exit order gets zero fill this many times in a row (price
# kept moving before our shaded limit could cross), stop chasing the bid
# down by a fixed FILL_SLIPPAGE each poll and switch to taking whatever bid
# actually exists (price floor $0.01) on the next attempt. Fixed-shade
# retries forever is exactly what let one real stop ride a book gap from a
# 0.88 bid to a 0.01 fill over ~58s / ~11 retries -- escalating after a
# couple of misses bounds how long we keep politely re-quoting a market
# that's already moved past us.
STOP_ESCALATE_AFTER = int(_env("STOP_ESCALATE_AFTER", "2"))
ALERT_WEBHOOK_URL = _env("ALERT_WEBHOOK_URL", "")
NOTIFY_MACOS = _env("NOTIFY_MACOS", "1") == "1"
AUTH_FAIL_LIMIT = int(_env("AUTH_FAIL_LIMIT", "3"))

# ---- Signal-following mode -------------------------------------------------
# When configured, the bot stops making its own independent band/floor
# decision against Kalshi's book and instead mirrors Judy's Cabinet's own
# live builder calls exactly — same signal that drives #live-btc-alerts /
# #live-eth-alerts and the 84.4%-win-rate track record. Kalshi's own book is
# then used ONLY to execute (price + liquidity), not as a second opinion.
# Leave SIGNAL_URL unset to fall back to the original independent-book mode.
SIGNAL_URL   = _env("SIGNAL_URL", "")
SIGNAL_TOKEN = _env("SIGNAL_TOKEN", "")   # per-subscriber token (get yours at /setup)
SIGNAL_USER  = _env("SIGNAL_USER", "")    # internal/admin use only, not for subscribers
SIGNAL_PASS  = _env("SIGNAL_PASS", "")
SIGNAL_MODE  = bool(SIGNAL_URL)
_SIGNAL_ASSET_MAP = {"bitcoin": "BTC", "ethereum": "ETH"}
_SIGNAL_SIDE_MAP = {"UP": "yes", "DOWN": "no"}


def _floor(asset): return FLOORS.get(asset, BAND_LO)

def now(): return datetime.now(timezone.utc)
def ts(): return now().strftime("%H:%M:%S")


def alert(msg):
    """Best-effort notification: console always, native macOS notification if
    on a Mac (zero setup — great when backgrounded locally), Discord webhook
    if configured (works anywhere, e.g. once this moves to a VPS)."""
    print(f"{ts()} [ALERT] {msg}", flush=True)
    if NOTIFY_MACOS and sys.platform == "darwin":
        try:
            script = (f'display notification {json.dumps(msg)} '
                      f'with title "Kalshi Bot" sound name "Glass"')
            subprocess.run(["osascript", "-e", script], timeout=5,
                           capture_output=True)
        except Exception as e:
            print(f"{ts()} macOS notify err: {e}")
    if not ALERT_WEBHOOK_URL:
        return
    try:
        body = json.dumps({"content": f"🚨 kalshi_bot: {msg}"}).encode()
        req = urllib.request.Request(ALERT_WEBHOOK_URL, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": UA})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"{ts()} alert webhook err: {e}")


# ---------------------------------------------------------------- public data
def _get(path, params=None):
    url = BASE + path
    if params:
        q = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        url += "?" + q
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _with_retry(fn, max_retries=3, base_delay=1.5):
    """Retry transient (network/5xx) failures with exponential backoff. Auth
    failures (401/403) are NOT retried here — they're handled by the caller's
    circuit-breaker logic instead, since retrying a bad key just wastes time."""
    last_err = None
    for attempt in range(max_retries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 410):
                # 410 means Kalshi has already closed this specific market to
                # new orders (these crypto markets have can_close_early=true
                # and can close before their listed close_time). Retrying the
                # exact same (stale) request will fail identically every
                # time — the caller needs a fresh market snapshot, not a
                # blind resend, so raise immediately instead of burning the
                # backoff delay.
                raise
            last_err = e
        except Exception as e:
            last_err = e
        if attempt < max_retries - 1:
            time.sleep(base_delay * (2 ** attempt))
    raise last_err


def _f(v):
    """Parse a Kalshi numeric field (string dollars/fp or number) to float, or None."""
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def current_market(asset):
    """Return the active (soonest-closing, open) 15m market for an asset, with live
    top-of-book, or None.

    NOTE (hard-won): Kalshi crypto markets price in sub-cent steps and expose data
    under *_dollars / *_fp field names. The legacy integer-cent fields (volume,
    yes_bid, yes_ask, last_price) come back NULL for these markets — reading them
    makes a very liquid market look dead. Always read the *_dollars fields. The
    market object carries full top-of-book + sizes, so no separate orderbook call
    is needed (the REST /orderbook endpoint returns {} for these)."""
    try:
        d = _get("/markets", {"series_ticker": SERIES[asset], "status": "open", "limit": 50})
    except Exception as e:
        print(f"{ts()} {asset} markets err: {e}"); return None
    ms = [m for m in d.get("markets", []) if m.get("close_time")]
    if not ms: return None
    ms.sort(key=lambda m: m["close_time"])
    m = ms[0]
    ya, yb = _f(m.get("yes_ask_dollars")), _f(m.get("yes_bid_dollars"))
    na, nb = _f(m.get("no_ask_dollars")),  _f(m.get("no_bid_dollars"))
    return {
        "ticker": m["ticker"],
        "strike": _f(m.get("floor_strike")) or _f(m.get("cap_strike")),
        "strike_type": m.get("strike_type"),  # "greater"/"greater_or_equal" -> YES=up; "less"/"less_or_equal" -> YES=down
        "close_time": m["close_time"],
        "status": m.get("status"),
        # Kalshi shards balance/matching across exchange_index 0-3+ -- an order
        # auto-routes to whichever shard THIS market lives on and the real
        # order check only looks at that shard's balance, not your total
        # (confirmed by Kalshi support: the web/app auto-moves funds between
        # shards when placing an order, the API does not). Carried through so
        # a live order attempt can pre-check the RIGHT shard's balance instead
        # of your aggregate total.
        "exchange_index": m.get("exchange_index"),
        # live top-of-book (dollars). Fall back to the 1-opposite identity if a
        # side's own quote is briefly absent.
        "yes_ask": ya if ya is not None else (round(1 - nb, 4) if nb is not None else None),
        "yes_bid": yb if yb is not None else (round(1 - na, 4) if na is not None else None),
        "no_ask":  na if na is not None else (round(1 - yb, 4) if yb is not None else None),
        "no_bid":  nb if nb is not None else (round(1 - ya, 4) if ya is not None else None),
        "yes_ask_size": _f(m.get("yes_ask_size_fp")),
        "yes_bid_size": _f(m.get("yes_bid_size_fp")),
        "last": _f(m.get("last_price_dollars")),
        "oi":  _f(m.get("open_interest_fp")),
        "vol": _f(m.get("volume_fp")),
        "empty": ya is None and yb is None and na is None and nb is None,
    }


def pick_side(m, asset):
    """Pure decision function, pulled out of run()'s loop so it's unit-testable:
    given a market dict (from current_market) and an asset code, return
    (side, ask) for the stronger in-band/above-floor favorite, or (None, None)."""
    side, ask = None, None
    for s in ("yes", "no"):
        a = m.get(f"{s}_ask")
        if a is not None and BAND_LO <= a <= BAND_HI and a >= _floor(asset):
            if ask is None or a > ask:  # the stronger favorite
                side, ask = s, a
    return side, ask


def settle_result(ticker):
    """Return 'yes'/'no' once the market settles, else None."""
    try:
        m = _get(f"/markets/{ticker}").get("market", {})
    except Exception:
        return None
    if m.get("status") in ("settled", "finalized") and m.get("result") in ("yes", "no"):
        return m["result"]
    return None


# ---------------------------------------------------------------- signal-following
def get_signal_alerts():
    """Fetch the live builder-calls feed. Never raises — a signal-fetch
    failure should degrade to 'no new signal this poll', not crash the bot
    or place a bad trade.

    Two ways to authenticate, in priority order:
      SIGNAL_TOKEN          — your personal token from /setup (subscribers).
      SIGNAL_USER/SIGNAL_PASS — shared admin basic auth (internal use only)."""
    url = SIGNAL_URL
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if SIGNAL_TOKEN:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={SIGNAL_TOKEN}"
    elif SIGNAL_USER or SIGNAL_PASS:
        import base64 as _b64
        cred = _b64.b64encode(f"{SIGNAL_USER}:{SIGNAL_PASS}".encode()).decode()
        headers["Authorization"] = f"Basic {cred}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode()).get("alerts", [])
    except Exception as e:
        print(f"{ts()} signal fetch err: {e}")
        return []


def latest_builder_call(asset, current_close_time_iso, tolerance_sec=60):
    """Return (side, call_ts) for the most recent Judy's Cabinet builder call
    on `asset` that belongs to THIS specific Kalshi window (matched by close
    time, within tolerance_sec — the two systems run on the same :00/:15/:30/
    :45 UTC grid, so a real match should be near-exact). Returns None if no
    matching call exists yet, or the signal feed is unreachable."""
    rev_map = {v: k for k, v in _SIGNAL_ASSET_MAP.items()}
    jc_asset = rev_map.get(asset)
    if jc_asset is None:
        return None
    target_close = datetime.fromisoformat(current_close_time_iso.replace("Z", "+00:00"))
    best = None
    for a in get_signal_alerts():
        if a.get("kind") != "builder" or a.get("asset") != jc_asset:
            continue
        slug = a.get("slug", "")
        try:
            open_ts = int(slug.rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            continue
        call_close = datetime.fromtimestamp(open_ts + 900, tz=timezone.utc)
        if abs((call_close - target_close).total_seconds()) > tolerance_sec:
            continue  # a real call, but for a different window than Kalshi's current one
        side = _SIGNAL_SIDE_MAP.get(a.get("side"))
        if side is None:
            continue
        if best is None or a.get("ts", 0) > best[1]:
            best = (side, a.get("ts", 0))
    return best


# ---------------------------------------------------------------- bankroll (mirrors flat_sim)
class Books:
    """Two parallel flat-20 PAPER bankrolls: optimistic -30% stop vs realistic
    hold-to-settle. This is a backtest-comparison ledger only — it never drives
    real order sizing (see get_balance() for that)."""
    def __init__(self, start):
        self.stop = start      # assumes a bid existed to cut at -30%
        self.hold = start      # losers ride to ~$0
        self.start = start
        self.wins = self.losses = 0
        self.peak_stop = self.peak_hold = start

    def apply(self, p, won, exit_px=None):
        fee = 0.07 * p * (1 - p)
        if won:
            gain_frac = (1 - fee) / p - 1
            self.stop += self.stop * FRAC * gain_frac
            self.hold += self.hold * FRAC * gain_frac
            self.wins += 1
        else:
            # optimistic: cut at -30% of stake (or worse if a bid filled below that)
            cut = STOP_FRAC
            if exit_px is not None and p > 0:
                cut = min(1.0, max(STOP_FRAC, (p - exit_px) / p))
            self.stop -= self.stop * FRAC * cut
            # realistic: contract -> $0, lose the whole stake
            self.hold -= self.hold * FRAC * 1.0
            self.losses += 1
        self.stop = max(self.stop, 0.0); self.hold = max(self.hold, 0.0)
        self.peak_stop = max(self.peak_stop, self.stop)
        self.peak_hold = max(self.peak_hold, self.hold)

    def line(self):
        n = self.wins + self.losses
        wr = f"{self.wins}/{n}" if n else "0/0"
        return (f"stop@30%=${self.stop:,.2f} (pk ${self.peak_stop:,.2f}) | "
                f"hold2settle=${self.hold:,.2f} (pk ${self.peak_hold:,.2f}) | {wr}")


def log(row):
    row["t"] = now().isoformat()
    print("KB_EVENT " + json.dumps(row, default=str), flush=True)
    try:
        with open(LEDGER, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        print(f"{ts()} log err: {e}")


def audit(row):
    """Structured, append-only log of REAL account activity (orders, fills,
    settlements) — separate from the paper LEDGER. This is what a member can
    point at to verify what the bot actually did with their money, and it's
    where the real-vs-backtest win-rate comparison lives over time."""
    row["t"] = now().isoformat()
    print("KB_AUDIT " + json.dumps(row, default=str), flush=True)
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        print(f"{ts()} audit log err: {e}")


# ---------------------------------------------------------------- live exec (gated; needs key)
def _load_key():
    """Load and type-check the configured private key. Raises clearly if the
    key is missing/unparseable, or is neither RSA nor Ed25519.

    Two ways to configure it, in priority order:
      KALSHI_PRIVATE_KEY_PEM  — the raw PEM text directly (for cloud deploys
                                 like Railway, where there's no local disk to
                                 point a file path at — set this as a normal
                                 multi-line env var).
      KALSHI_PRIVATE_KEY_PATH — a file path on disk (local/laptop use)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
    pem_env = os.environ.get("KALSHI_PRIVATE_KEY_PEM")
    if pem_env:
        # Common cloud-env-var gotcha: some dashboards (Railway included, in
        # some paste paths) store a pasted multi-line secret with literal
        # backslash-n characters instead of real line breaks, which breaks
        # PEM parsing with a cryptic "MalformedFraming" error. Normalize
        # defensively — this is a no-op if the newlines were already real.
        pem_text = pem_env.replace("\\n", "\n").strip()
        data = pem_text.encode()
        src = "KALSHI_PRIVATE_KEY_PEM"
    else:
        path = os.environ["KALSHI_PRIVATE_KEY_PATH"]
        with open(path, "rb") as f:
            data = f.read()
        src = path
    try:
        pk = serialization.load_pem_private_key(data, password=None)
    except Exception as e:
        if pem_env:
            # Safe diagnostic — the BEGIN/END markers and whitespace shape
            # are not secret (every PEM file has identical boilerplate
            # there); only the base64 body in between is sensitive, and
            # that's never printed here.
            t = pem_text
            newline_char = chr(10)
            backslash_n = chr(92) + "n"
            has_literal = backslash_n in pem_env
            print(f"{ts()} KALSHI_PRIVATE_KEY_PEM failed to parse ({e}). "
                  f"Diagnostic (safe, no key material shown): "
                  f"length={len(t)} chars, lines={t.count(newline_char) + 1}, "
                  f"starts_with={t[:28]!r}, ends_with={t[-26:]!r}, "
                  f"had_literal_backslash_n_before_normalize={has_literal}")
        raise
    if not isinstance(pk, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
        raise ValueError(f"private key ({src}) is neither an RSA nor "
                          f"an Ed25519 private key (got {type(pk).__name__})")
    return pk


def _signed_headers(method, path):
    """Build the three KALSHI-ACCESS-* headers for an authenticated request.
    Supports RSA-PSS or Ed25519 keys, with an explicit type check (not an
    implicit fallthrough) so an unsupported key type fails loudly here."""
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    key_id = os.environ["KALSHI_API_KEY_ID"]
    pk = _load_key()
    tms = int(time.time() * 1000)
    msg = f"{tms}{method.upper()}{path}".encode()
    if isinstance(pk, rsa.RSAPrivateKey):
        sig = pk.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                       salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    else:  # Ed25519, already isinstance-checked in _load_key()
        sig = pk.sign(msg)
    return {"User-Agent": UA, "KALSHI-ACCESS-KEY": key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(tms),
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


def _signed_get(path):
    def _do():
        # Kalshi signs method+path WITHOUT the query string, even though the
        # actual request URL still needs it.
        signed_path = "/trade-api/v2" + path.split("?")[0]
        req = urllib.request.Request(BASE + path, headers=_signed_headers("GET", signed_path))
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    return _with_retry(_do)


def get_balance():
    """Real account balance in DOLLARS (Kalshi returns cents)."""
    d = _signed_get("/portfolio/balance")
    cents = d.get("balance")
    if cents is None:
        raise ValueError(f"unexpected /portfolio/balance response: {d}")
    return cents / 100.0


def get_shard_balance(exchange_index):
    """Real balance in DOLLARS on ONE specific exchange shard -- confirmed by
    Kalshi support: an order auto-routes to the shard the market lives on,
    and the balance check at order time only looks at THAT shard, not your
    aggregate total (the web/app auto-consolidates funds between shards when
    you place an order there; the raw API does not). Returns None if
    exchange_index is None (can't scope the check) or the shard isn't found
    in the breakdown (treat as $0 available on that shard)."""
    if exchange_index is None:
        return None
    d = _signed_get(f"/portfolio/balance?exchange_index={exchange_index}")
    for row in d.get("balance_breakdown", []):
        if row.get("exchange_index") == exchange_index:
            try:
                return float(row.get("balance", 0))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def get_positions():
    """Real open positions (market_positions list), raw from Kalshi."""
    d = _signed_get("/portfolio/positions")
    return d.get("market_positions", [])


def get_open_orders():
    """Real resting orders, raw from Kalshi."""
    d = _signed_get("/portfolio/orders?status=resting")
    return d.get("orders", [])


def _submit_live_order(ticker, side, price, real_bal):
    """Size off the real balance and place one real order. Raises on failure
    (caller decides how to handle); returns (count, order) on success."""
    count = max(1, int((real_bal * FRAC) / price))
    order = place_order_live(ticker, side, price, count)
    return count, order


def _http_err_detail(e):
    """Read Kalshi's actual JSON error body off an HTTPError, if any -- the
    bare exception repr ("HTTP Error 400: Bad Request") hides the real
    reason. Safe to call even if the body's already been consumed or is
    empty; never raises."""
    try:
        body = e.read()
        return body.decode(errors="replace") if body else "(no response body)"
    except Exception:
        return "(could not read response body)"


def _order_id(order):
    if not isinstance(order, dict):
        return None
    # V2 returns order_id at the top level; keep the old nested lookup as a
    # fallback in case a response shape ever differs from the docs.
    return order.get("order_id") or order.get("order", {}).get("order_id")


def _fill_count(order):
    """Parse Kalshi's fill_count (a decimal string like '1.00') safely.
    Kalshi accepting an order (HTTP success, an order_id back) is NOT the
    same as it actually filling -- an immediate_or_cancel order can be
    accepted and still match zero contracts if the book moved, leaving no
    trace in the real account (confirmed twice live: once on our own BTC
    entry, once on a subscriber's ETH entry). Only a fill_count > 0 means
    real money actually moved."""
    try:
        return float(order.get("fill_count", 0))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _get_real_fill_price(order_id, ticker, side):
    """Kalshi's own confirmed average fill price for a just-placed order, via
    GET /portfolio/fills -- NOT the price we requested. We submit a padded
    ceiling (entries) or shaded floor (exits) and Kalshi fills at whatever
    the book actually offered at match time; in a fast-moving 15m crypto
    window the real match can land meaningfully away from the pre-order
    snapshot we based `price` on. Confirmed live: a real ETH stop logged as
    a 57% drawdown off our snapshot-based entry while the subscriber's own
    Kalshi account showed a 24% loss on the same position -- the snapshot,
    not the real fill, was wrong. Returns None (caller falls back to the
    requested price) if the lookup fails or Kalshi hasn't indexed the fill
    yet; this is a correction on top of a trade that already happened, never
    a reason to block or retry the trade itself."""
    try:
        d = _signed_get(f"/portfolio/fills?order_id={order_id}&ticker={ticker}")
    except Exception as e:
        print(f"{ts()} fill-price lookup failed for order {order_id}: {e}")
        return None
    field = "no_price_dollars" if side == "no" else "yes_price_dollars"
    total_cost = total_count = 0.0
    for f in d.get("fills", []):
        price, count = _f(f.get(field)), _f(f.get("count_fp"))
        if price is None or count is None:
            continue
        total_cost += price * count
        total_count += count
    return round(total_cost / total_count, 4) if total_count > 0 else None


def _record_live_entry(asset, tkr, side, ask, count, order, real_bal, retried=False):
    audit({"ev": "order", "asset": asset, "ticker": tkr, "side": side,
           "price": ask, "count": count, "balance_before": real_bal,
           "retry_after_410": retried, "order_id": _order_id(order)})
    tag = " (after 410 retry on a fresh book)" if retried else ""
    alert(f"🟢 REAL ENTRY{tag} — {asset} {side.upper()} @ {ask:.2f} "
          f"x{count} contract(s) ({tkr})")


_ORDERS_PATH = "/trade-api/v2/portfolio/events/orders"


def _book_side_and_price(side, price, closing=False):
    """Kalshi's V2 order API dropped side=yes/no + action=buy/sell for a
    single BookSide (bid/ask), always quoted in YES terms: bid=buy YES,
    ask=sell YES (selling YES == buying NO at 1-price). `side`/`price` here
    are our internal yes/no-denominated values from current_market(); this
    translates them to Kalshi's book_side + YES-denominated price. Closing
    an existing position means doing the OPPOSITE book action (close a YES
    long by selling YES; close a NO long by buying YES back)."""
    opening_bid = (side == "yes")       # would this side normally be a "bid" to OPEN?
    book_side = "ask" if (opening_bid == closing) else "bid"
    book_price = price if side == "yes" else round(1 - price, 4)
    return book_side, book_price


def _post_order(body):
    def _do():
        req = urllib.request.Request(BASE + "/portfolio/events/orders", method="POST",
            data=json.dumps(body).encode(),
            headers={**_signed_headers("POST", _ORDERS_PATH), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    return _with_retry(_do)


_entry_attempt_seq = {}  # (ticker, side) -> count of real entry attempts made this process run

def place_order_live(ticker, side, price, count):
    """Open a REAL position (buy `side` at `price`, YES/NO in our internal
    sense). Signs with the configured Kalshi key. Only called when
    EXEC_LIVE=1 and only after the startup balance check has already proven
    the key works."""
    book_side, book_price = _book_side_and_price(side, price, closing=False)
    # Deterministic id per (ticker, side, attempt#), not just (ticker, side) --
    # the old scheme reused the exact same id for the entire 15m window, which
    # is safe for a crash/restart mid-entry (same attempt, same id, Kalshi
    # treats a retry as a no-op dup) but silently breaks a legitimate SECOND
    # real entry into the same ticker+side later in the same window -- e.g.
    # STOP clears open_pos[asset] and the entry gate only checks
    # `asset not in open_pos`, so re-entering the same ticker+side after a
    # stop (or any reversal re-entry logic) is a real, reachable path, and it
    # would have been rejected as a duplicate of the first entry's order.
    # The attempt counter is in-memory only and resets with the process, same
    # as open_pos/done/books -- consistent with this bot's existing
    # restart model (positions are reconciled from Kalshi's live state, not
    # from replaying in-memory counters).
    key = (ticker, side)
    attempt = _entry_attempt_seq.get(key, 0)
    _entry_attempt_seq[key] = attempt + 1
    client_order_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"kalshi-bot:{ticker}:{side}:{attempt}"))
    body = {"ticker": ticker, "side": book_side, "count": f"{count:.2f}",
            "price": f"{book_price:.4f}", "client_order_id": client_order_id,
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross"}
    return _post_order(body)


def close_live_position(ticker, side, price, count):
    """Close a REAL position opened via place_order_live (same `side` in our
    internal yes/no sense as the original entry) by crossing the opposite
    book side. `price` is our internal yes/no-denominated exit price (e.g.
    the current yes_bid to close a YES long, or the current no_bid to close
    a NO long) — NOT required for the live strategy's own ride-to-settlement
    design, but used for manual verification and any future exit tooling."""
    book_side, book_price = _book_side_and_price(side, price, closing=True)
    client_order_id = str(uuid.uuid4())
    body = {"ticker": ticker, "side": book_side, "count": f"{count:.2f}",
            "price": f"{book_price:.4f}", "client_order_id": client_order_id,
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross", "reduce_only": True}
    return _post_order(body)


# ---------------------------------------------------------------- startup safety (live only)
def startup_check():
    """Confirm the configured key actually works and return the real balance,
    BEFORE the main loop ever tries to place an order. Returns None (and logs
    why) if the key/path is missing or Kalshi rejects it — callers should fall
    back to paper mode rather than limp into the loop and fail cryptically on
    the first live trade."""
    has_key = "KALSHI_PRIVATE_KEY_PEM" in os.environ or "KALSHI_PRIVATE_KEY_PATH" in os.environ
    if "KALSHI_API_KEY_ID" not in os.environ or not has_key:
        alert("EXEC_LIVE=1 but KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PEM(or _PATH) not set — "
              "falling back to PAPER mode.")
        return None
    try:
        bal = get_balance()
    except urllib.error.HTTPError as e:
        alert(f"EXEC_LIVE=1 but the Kalshi key was rejected (HTTP {e.code}) — "
              f"check KALSHI_API_KEY_ID/KALSHI_PRIVATE_KEY_PEM(or _PATH). Falling back to PAPER mode.")
        return None
    except Exception as e:
        alert(f"EXEC_LIVE=1 but startup balance check failed ({e}) — falling back to PAPER mode.")
        return None
    print(f"{ts()} startup check OK — real Kalshi balance ${bal:,.2f}")
    return bal


def reconcile_positions(assets):
    """On startup (live mode only), re-attach to any REAL position that's
    still open from before a restart, and immediately grade any that already
    settled while the process was down. Returns a dict asset -> position (for
    still-open ones) to seed open_pos with, so a restart never loses track of
    real exposure."""
    reattached = {}
    try:
        positions = get_positions()
        orders = get_open_orders()
    except Exception as e:
        alert(f"position reconciliation failed ({e}) — proceeding with no known open "
              f"positions; if a real position exists, verify manually on kalshi.com.")
        return reattached

    our_tickers = set()
    for o in orders:
        our_tickers.add(o.get("ticker"))
    for p in positions:
        tkr = p.get("ticker", "")
        qty = p.get("position", 0) or 0
        if qty == 0:
            continue
        asset = next((a for a in assets if SERIES.get(a) and tkr.startswith(SERIES[a])), None)
        if not asset:
            continue
        res = settle_result(tkr)
        if res is not None:
            # already settled while we were down — grade it and move on
            side = "yes" if qty > 0 else "no"
            won = (res == side)
            audit({"ev": "settle_reconciled", "asset": asset, "ticker": tkr,
                   "side": side, "result": res, "won": won})
            print(f"{ts()} RECONCILE {asset} {tkr} already settled ({res}) while down — logged.")
        else:
            side = "yes" if qty > 0 else "no"
            reattached[asset] = {"asset": asset, "ticker": tkr, "side": side,
                                  "entry": None, "strike": None, "close_time": None,
                                  "count": abs(qty), "live_order": None, "reconciled": True}
            audit({"ev": "reconciled_open_position", "asset": asset, "ticker": tkr,
                   "side": side, "count": abs(qty)})
            print(f"{ts()} RECONCILE {asset} {tkr} still open ({side} x{abs(qty)}) — re-attached.")
    return reattached


# ---------------------------------------------------------------- first-run setup
def _first_run_wizard():
    """One-time interactive setup for a totally fresh install (no .env yet).
    Asks a couple of plain-language questions, tests a live key immediately
    (the same check that would otherwise require debugging by hand), and
    writes a .env so this never runs again. No-ops instantly if .env already
    exists, or if there's no real terminal attached (e.g. launched as a
    background/scheduled job) — it never hangs waiting for input it can't get."""
    global EXEC_LIVE, ALERT_WEBHOOK_URL
    if os.path.exists(".env"):
        return
    if not sys.stdin.isatty():
        # Headless launch (background job, or a cloud deploy with real env
        # vars already set — e.g. Railway). The wizard itself can't run
        # without a real terminal, but EXEC_LIVE/credentials may already be
        # correctly configured via real environment variables rather than a
        # .env file, so don't claim "starting in PAPER mode" when that may
        # not be true — the actual startup_check() right after this is what
        # determines the real mode.
        if EXEC_LIVE:
            print(f"{ts()} no .env file (fine — EXEC_LIVE=1 was set via real "
                  f"environment variables, e.g. a cloud deploy). Skipping the "
                  f"interactive setup wizard, which needs a real terminal.")
        else:
            print(f"{ts()} no .env found and not running in a terminal — starting "
                  f"in PAPER mode with defaults. Run `python3 kalshi_bot.py` directly "
                  f"in a terminal once to configure live trading.")
        return

    print("=" * 60)
    print("Judy's Cabinet Kalshi bot — first-time setup")
    print("This only happens once. It writes a .env file so you won't see")
    print("this again after today.")
    print("=" * 60)

    env = {}
    mode = input("\nRun in PAPER mode (safe, no real trades) or LIVE mode "
                 "(real money)? [paper/live, default paper]: ").strip().lower()
    if mode not in ("live", "l"):
        EXEC_LIVE = False
        env["EXEC_LIVE"] = "0"
        print("\nStarting in PAPER mode. Switch to live later by editing .env "
              "and setting EXEC_LIVE=1.")
    else:
        print("\nLIVE mode places real trades with real money.")
        print("If you haven't already, generate a key at kalshi.com -> "
              "Settings -> API Keys.\n")
        while True:
            key_id = input("Kalshi Key ID: ").strip()
            key_path = input("Path to your private key FILE — not the key itself, "
                             "just where it's saved (drag the file's icon into this "
                             "window to fill this in automatically): ").strip()
            key_path = key_path.strip("'\"").replace("\\ ", " ")
            # Catch the single most common mix-up here: pasting the key's actual
            # contents instead of a file path. Left unchecked, this silently
            # writes the key material into os.environ and fails later with a
            # confusing, unrelated-looking error -- and worse, it means the key
            # text just got pasted into a terminal that might get screenshotted.
            if "BEGIN" in key_path.upper() or len(key_path) > 300:
                print("\n⚠️  That looks like the key's CONTENTS, not a file path —")
                print("please don't paste the key text here. If you still have it")
                print("in your clipboard, DON'T screenshot or paste it anywhere else")
                print("either; rotate the key on kalshi.com if it's been exposed.")
                print("This field wants the location of the file on your computer,")
                print("e.g. C:\\Users\\you\\Downloads\\kalshi_key.txt — drag the file's")
                print("icon into this window and it'll fill in the path for you.\n")
                continue
            os.environ["KALSHI_API_KEY_ID"] = key_id
            os.environ["KALSHI_PRIVATE_KEY_PATH"] = key_path
            print("\nTesting this key against Kalshi...")
            try:
                bal = get_balance()
                print(f"✅ It works — real balance ${bal:,.2f}\n")
                env["KALSHI_API_KEY_ID"] = key_id
                env["KALSHI_PRIVATE_KEY_PATH"] = key_path
                EXEC_LIVE = True
                env["EXEC_LIVE"] = "1"
                break
            except Exception as e:
                print(f"❌ That didn't work ({e}).")
                print("Most common cause: more than one API key exists on your")
                print("Kalshi account and this ID/file don't match. Go to")
                print("kalshi.com -> Settings -> API Keys, delete ALL your keys,")
                print("create exactly ONE, and copy the ID + download the file")
                print("from that same screen.\n")
                if input("Try again? [y/n]: ").strip().lower() != "y":
                    print("\nFalling back to PAPER mode for now. Fix this later "
                          "by editing .env and setting EXEC_LIVE=1.")
                    EXEC_LIVE = False
                    env["EXEC_LIVE"] = "0"
                    break

    webhook = input("\nDiscord webhook URL for phone alerts (optional, press "
                    "enter to skip): ").strip()
    if webhook:
        ALERT_WEBHOOK_URL = webhook
        env["ALERT_WEBHOOK_URL"] = webhook

    with open(".env", "w") as f:
        f.write("# Written by the first-run setup wizard.\n")
        for k, v in env.items():
            f.write(f"{k}={v}\n")
    print(f"\nSaved to .env. Starting the bot now...\n")


# ---------------------------------------------------------------- main loop
def run():
    global EXEC_LIVE
    books = Books(START_BANK)
    auth_fail_count = 0

    if EXEC_LIVE:
        bal = startup_check()
        if bal is None:
            EXEC_LIVE = False  # degrade to paper rather than crash a 24/7 process
        else:
            open_pos = reconcile_positions(ASSETS)
    if not EXEC_LIVE:
        open_pos = {}

    mode = "LIVE" if EXEC_LIVE else "PAPER"
    entry_src = "SIGNAL (mirrors Judy's Cabinet's live builder calls)" if SIGNAL_MODE \
                else f"independent book (band={BAND_LO}-{BAND_HI} floors={FLOORS})"
    print(f"{ts()} kalshi_bot [{mode}] assets={ASSETS} frac={FRAC} stop={STOP_FRAC} "
          f"entry_source={entry_src}")
    if EXEC_LIVE:
        if STOP_ENABLED:
            print(f"{ts()} NOTE: STOP_ENABLED is ON — a losing live position gets a REAL "
                  f"exit order once it's down {int(STOP_FRAC*100)}% from entry. See the "
                  f"module docstring.")
        else:
            print(f"{ts()} NOTE: STOP_ENABLED is OFF — live losing positions ride to "
                  f"settlement (flat-{int(FRAC*100)}% sizing is the only risk control).")
        alert(f"🟢 kalshi_bot started in LIVE mode — balance ${bal:,.2f}, "
              f"assets={ASSETS}, flat-{int(FRAC*100)}% sizing.")
    done = set()    # tickers already settled
    last_heartbeat = 0.0

    while True:
        if time.time() - last_heartbeat >= 300:
            last_heartbeat = time.time()
            print(f"KB_HEARTBEAT {ts()} mode={mode} assets={ASSETS} open={len(open_pos)} "
                  f"| {books.line()}", flush=True)
        for asset in ASSETS:
            m = current_market(asset)
            if not m or not m["strike"]:
                # Previously silent — if Kalshi's market fetch failed or
                # returned no strike, this entire asset was skipped for the
                # cycle with zero trace. Now logged so a missed window can
                # actually be explained instead of guessed at.
                log({"ev": "no_market", "asset": asset})
                print(f"{ts()} {asset} no open market / no strike this cycle — skip")
                continue
            tkr = m["ticker"]
            tte = (datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")) - now()).total_seconds()

            # ---- ENTRY ----------------------------------------------------
            # SIGNAL_MODE: mirror Judy's Cabinet's own live builder call
            # exactly — Kalshi's book is used only to execute (price +
            # liquidity), never as a second independent gate. Falls back to
            # the original independent band/floor logic when SIGNAL_URL isn't
            # configured (the standalone/no-signal-access mode). Uses a flag
            # (not `continue`) for "nothing to enter" so STOP/SETTLE below
            # still always run for assets that already have an open position.
            entry_side = entry_ask = None
            if asset not in open_pos:
                if SIGNAL_MODE and tte <= max(ENTRY_TTE_HI, 240) and tte > 0:
                    call = latest_builder_call(asset, m["close_time"])
                    if call is None:
                        # Full per-poll trace while watching for a signal — this
                        # used to be completely silent, which is exactly the gap
                        # that made the last two missed calls undiagnosable.
                        log({"ev": "watching_for_signal", "asset": asset, "ticker": tkr,
                             "tte": round(tte)})
                        print(f"{ts()} {asset} {tkr} tte={tte:.0f}s watching for a "
                              f"Judy's Cabinet call — none yet this window")
                    else:
                        side, call_ts = call
                        if m.get("empty", True) or m.get(f"{side}_ask") is None:
                            log({"ev": "signal_no_liquidity", "asset": asset, "ticker": tkr,
                                 "tte": round(tte), "signal_side": side})
                            print(f"{ts()} {asset} {tkr} tte={tte:.0f}s Judy called {side.upper()} "
                                  f"but Kalshi has no liquidity on that side — skip")
                        else:
                            entry_side, entry_ask = side, m[f"{side}_ask"]
                elif not SIGNAL_MODE and ENTRY_TTE_LO <= tte <= ENTRY_TTE_HI:
                    if m.get("empty", True):
                        log({"ev": "no_liquidity", "asset": asset, "ticker": tkr, "tte": round(tte)})
                        print(f"{ts()} {asset} {tkr} tte={tte:.0f}s NO LIQUIDITY (empty book) — skip")
                    else:
                        side, ask = pick_side(m, asset)
                        if side is None:
                            # Previously silent — no record existed of what the book
                            # actually showed when nothing qualified, which made it
                            # impossible to explain after the fact why a window was
                            # skipped. Now logged every poll in-window so there's a
                            # real trail of the price action, not just a guess.
                            log({"ev": "no_qualifying_side", "asset": asset, "ticker": tkr,
                                 "tte": round(tte), "yes_ask": m.get("yes_ask"),
                                 "no_ask": m.get("no_ask"), "floor": _floor(asset)})
                            print(f"{ts()} {asset} {tkr} tte={tte:.0f}s no side in band "
                                  f"(yes_ask={m.get('yes_ask')} no_ask={m.get('no_ask')} "
                                  f"floor={_floor(asset)}) — skip")
                        else:
                            entry_side, entry_ask = side, ask

            if entry_side is not None:
                side, ask = entry_side, entry_ask
                pos = {"asset": asset, "ticker": tkr, "side": side, "entry": ask,
                       "strike": m["strike"], "close_time": m["close_time"],
                       "count": None, "live_order": None}
                if EXEC_LIVE and auth_fail_count < AUTH_FAIL_LIMIT:
                    shard = m.get("exchange_index")
                    try:
                        real_bal = get_balance()   # size off REAL balance, not paper books.stop
                        # Pre-check balance on the SPECIFIC shard this order will route to
                        # (confirmed by Kalshi support: an order only checks balance on the
                        # shard its market lives on, not your aggregate total -- the web/app
                        # auto-moves funds between shards when placing an order, the raw API
                        # doesn't). Compared against the FULL intended stake (real_bal*FRAC),
                        # not just one contract's price (`ask`) -- comparing against `ask`
                        # alone missed a real insufficient_balance rejection the moment FRAC
                        # went from 0.20 to 0.50 and one contract's price stopped being a
                        # meaningful proxy for the whole order's cost.
                        intended_stake = real_bal * FRAC
                        shard_bal = get_shard_balance(shard) if shard is not None else None
                        if shard_bal is not None and shard_bal < intended_stake:
                            log({"ev": "shard_balance_insufficient", "asset": asset, "ticker": tkr,
                                 "exchange_index": shard, "shard_balance": shard_bal,
                                 "needed": intended_stake})
                            alert(f"⚠️ {asset} {side.upper()} ({tkr}) skipped — shard {shard} only "
                                  f"has ${shard_bal:.2f} available (needs ${intended_stake:.2f} for "
                                  f"this stake), even though total balance is higher. Kalshi's API "
                                  f"doesn't auto-move funds between shards the way their app does; "
                                  f"recorded as paper-only this window.")
                            open_pos[asset] = pos
                            log({"ev": "entry", **{k: pos[k] for k in ("asset", "ticker", "side", "entry", "strike")},
                                 "tte": round(tte), "live": False})
                            print(f"{ts()} {asset} ENTRY {side.upper()} @ {ask:.2f} ({tkr}) tte={tte:.0f}s")
                            continue
                        # Pad the REAL order's limit price (only) by FILL_SLIPPAGE so a
                        # small move between our snapshot and Kalshi receiving the order
                        # doesn't turn into a miss -- Kalshi still fills at the best
                        # available price if it's better than this ceiling.
                        live_price = min(round(ask + FILL_SLIPPAGE, 4), 0.99)
                        count, order = _submit_live_order(tkr, side, live_price, real_bal)
                        auth_fail_count = 0
                        filled = _fill_count(order)
                        if filled > 0:
                            real_price = _get_real_fill_price(_order_id(order), tkr, side)
                            fill_price = real_price if real_price is not None else live_price
                            pos["entry"] = ask = fill_price
                            pos["live_order"], pos["count"] = order, int(filled)
                            _record_live_entry(asset, tkr, side, fill_price, int(filled), order, real_bal)
                        else:
                            # Accepted but matched nothing -- price moved before the
                            # order landed. Refetch the book and retry ONCE at the
                            # fresh price (confirmed worth doing: a real miss on both
                            # BTC and ETH in the same tick, both padded by the normal
                            # FILL_SLIPPAGE and still missed -- a fast enough move can
                            # exceed a single static buffer). Bounded by RETRY_FLOOR so
                            # a price that's dropped a lot isn't blindly chased down.
                            retried_ok = False
                            m2 = current_market(asset)
                            if (m2 and m2.get("ticker") == tkr and not m2.get("empty", True)
                                    and m2.get(f"{side}_ask") is not None
                                    and m2[f"{side}_ask"] >= RETRY_FLOOR):
                                fresh_ask = m2[f"{side}_ask"]
                                fresh_price = min(round(fresh_ask + FILL_SLIPPAGE, 4), 0.99)
                                try:
                                    count2, order2 = _submit_live_order(tkr, side, fresh_price, real_bal)
                                    filled2 = _fill_count(order2)
                                    if filled2 > 0:
                                        real_price = _get_real_fill_price(_order_id(order2), tkr, side)
                                        fill_price = real_price if real_price is not None else fresh_price
                                        pos["live_order"], pos["count"] = order2, int(filled2)
                                        pos["entry"] = ask = fill_price
                                        retried_ok = True
                                        _record_live_entry(asset, tkr, side, fill_price, int(filled2),
                                                            order2, real_bal, retried=True)
                                except Exception as e2:
                                    print(f"{ts()} {asset} retry after zero-fill also failed: {e2}")
                            if not retried_ok:
                                # No real position, no real money moved either attempt.
                                # The paper-bookkeeping comparison below still grades
                                # this window same as always.
                                log({"ev": "real_order_zero_fill", "asset": asset, "ticker": tkr,
                                     "side": side, "requested_count": count,
                                     "order_id": _order_id(order)})
                                alert(f"⚠️ Real order accepted but got ZERO fill (retry also missed "
                                      f"or price fell below ${RETRY_FLOOR:.2f}) — {asset} "
                                      f"{side.upper()} @ {live_price:.2f} ({tkr}). No real "
                                      f"position opened; recorded as paper-only this window.")
                    except urllib.error.HTTPError as e:
                        if e.code in (401, 403):
                            auth_fail_count += 1
                            print(f"{ts()} {asset} LIVE auth error ({e.code}), "
                                  f"{auth_fail_count}/{AUTH_FAIL_LIMIT}")
                            if auth_fail_count >= AUTH_FAIL_LIMIT:
                                EXEC_LIVE = False
                                alert(f"{AUTH_FAIL_LIMIT} consecutive Kalshi auth failures — "
                                      f"disabling live trading for the rest of this run. "
                                      f"Check KALSHI_API_KEY_ID/KALSHI_PRIVATE_KEY_PATH, then restart.")
                        elif e.code == 410:
                            # Kalshi's crypto 15m markets have can_close_early=true and can
                            # stop accepting new orders before their listed close_time — often
                            # right when the price move that triggered our signal happens.
                            # The snapshot we decided on may already be stale. Refetch the
                            # live book once and retry against current price/liquidity before
                            # giving up, instead of a blind resend of the same stale request.
                            print(f"{ts()} {asset} LIVE order got 410 (Gone) — Kalshi likely "
                                  f"closed {tkr} to new orders early; refetching book for one retry")
                            m2 = current_market(asset)
                            retried_ok = False
                            if (m2 and m2.get("ticker") == tkr and not m2.get("empty", True)
                                    and m2.get(f"{side}_ask") is not None):
                                fresh_ask = m2[f"{side}_ask"]
                                live_fresh_price = min(round(fresh_ask + FILL_SLIPPAGE, 4), 0.99)
                                try:
                                    count, order = _submit_live_order(tkr, side, live_fresh_price, real_bal)
                                    filled = _fill_count(order)
                                    if filled > 0:
                                        real_price = _get_real_fill_price(_order_id(order), tkr, side)
                                        fill_price = real_price if real_price is not None else live_fresh_price
                                        pos["live_order"], pos["count"] = order, int(filled)
                                        pos["entry"] = ask = fill_price
                                        auth_fail_count = 0
                                        retried_ok = True
                                        _record_live_entry(asset, tkr, side, fill_price, int(filled),
                                                            order, real_bal, retried=True)
                                    else:
                                        log({"ev": "real_order_zero_fill", "asset": asset, "ticker": tkr,
                                             "side": side, "requested_count": count,
                                             "order_id": _order_id(order), "retry_after_410": True})
                                except Exception as e2:
                                    print(f"{ts()} {asset} LIVE retry after 410 also failed: {e2}")
                            if not retried_ok:
                                log({"ev": "market_closed_early", "asset": asset, "ticker": tkr,
                                     "side": side, "tte": round(tte)})
                                alert(f"⚠️ MISSED real entry — {asset} {side.upper()} ({tkr}) — "
                                      f"Kalshi either closed this market to new orders before we "
                                      f"could fire, or the price moved past what we could fill at. "
                                      f"Recorded as paper-only this window; no real position opened.")
                        else:
                            # Read Kalshi's actual error body -- the bare HTTPError repr
                            # ("HTTP Error 400: Bad Request") hides the real reason, the
                            # same blind spot that hid the true cause of the 410 bug
                            # earlier (turned out to be a dead V1 endpoint, not what the
                            # generic message implied). Confirmed live: a subscriber hit
                            # a 400 with no visibility into why.
                            detail = _http_err_detail(e)
                            print(f"{ts()} {asset} LIVE order HTTP err: {e} {detail} — "
                                  f"falling back to paper record")
                    except Exception as e:
                        print(f"{ts()} {asset} LIVE order err: {e} — falling back to paper record")
                open_pos[asset] = pos
                log({"ev": "entry", **{k: pos[k] for k in ("asset", "ticker", "side", "entry", "strike")},
                     "tte": round(tte), "live": EXEC_LIVE})
                print(f"{ts()} {asset} ENTRY {side.upper()} @ {ask:.2f} ({tkr}) tte={tte:.0f}s")

            # ---- STOP: cut a loser at -STOP_FRAC or best available bid. Places a
            # REAL exit order when a real position exists (default ON, see the
            # STOP_ENABLED config comment above for why this changed from
            # paper-only). Always updates the paper comparison ledger either way.
            if STOP_ENABLED and asset in open_pos:
                pos = open_pos[asset]
                if pos["ticker"] == tkr and not m.get("empty", True) and pos.get("entry"):
                    our_bid = m.get(f"{pos['side']}_bid")
                    if our_bid is not None and pos["entry"] > 0:
                        dd = (pos["entry"] - our_bid) / pos["entry"]
                        if dd >= STOP_FRAC:
                            has_real_position = (EXEC_LIVE and pos.get("live_order") is not None
                                                  and pos.get("count"))
                            if has_real_position:
                                attempts = pos.get("stop_attempts", 0)
                                if attempts >= STOP_ESCALATE_AFTER:
                                    # Already missed STOP_ESCALATE_AFTER fills in a row while
                                    # shading a fixed nickel off a bid that kept falling --
                                    # stop re-quoting politely and take whatever bid exists now.
                                    exit_price = 0.01
                                else:
                                    # Shade the exit price down (same spirit as FILL_SLIPPAGE on
                                    # entries, opposite direction) -- getting out reliably matters
                                    # more than the exact price when deliberately cutting a loser.
                                    exit_price = max(round(our_bid - FILL_SLIPPAGE, 4), 0.01)
                                try:
                                    order = close_live_position(tkr, pos["side"], exit_price, pos["count"])
                                    filled = _fill_count(order)
                                except Exception as e:
                                    filled = 0
                                    print(f"{ts()} {asset} STOP exit attempt err: {e} — will retry next poll")
                                if filled <= 0:
                                    # Didn't fill (price moved) -- real position is still open.
                                    # Leave it in open_pos so this retries every poll until it
                                    # either fills or the window closes and SETTLE grades it.
                                    pos["stop_attempts"] = attempts + 1
                                    print(f"{ts()} {asset} STOP exit tried @ {exit_price:.2f} but got "
                                          f"ZERO fill (dd {dd*100:.0f}%) — attempt {attempts + 1}, "
                                          f"still holding, retrying")
                                else:
                                    real_exit = _get_real_fill_price(_order_id(order), tkr, pos["side"])
                                    exit_fill = real_exit if real_exit is not None else exit_price
                                    audit({"ev": "stop", "asset": asset, "ticker": tkr, "side": pos["side"],
                                           "entry": pos["entry"], "exit": exit_fill, "dd": round(dd, 3),
                                           "count": filled, "order_id": _order_id(order)})
                                    alert(f"🛑 REAL STOP — {asset} {pos['side'].upper()} x{int(filled)} "
                                          f"({tkr}) cut @ {exit_fill:.2f}, entry was {pos['entry']:.2f} "
                                          f"(down {dd*100:.0f}%)")
                                    books.apply(pos["entry"], won=False, exit_px=our_bid)
                                    log({"ev": "stop", "asset": asset, "ticker": tkr,
                                         "entry": pos["entry"], "exit": exit_fill, "dd": round(dd, 3), "live": True})
                                    print(f"{ts()} {asset} STOP {tkr} @ {exit_fill:.2f} (dd {dd*100:.0f}%) | {books.line()}")
                                    del open_pos[asset]
                                    done.add(tkr)
                                    continue
                            else:
                                # No real position (paper-only entry, e.g. a shard-balance skip
                                # or zero-fill) -- same as before, just the comparison ledger.
                                books.apply(pos["entry"], won=False, exit_px=our_bid)
                                log({"ev": "stop", "asset": asset, "ticker": tkr,
                                     "entry": pos["entry"], "exit": our_bid, "dd": round(dd, 3), "live": False})
                                print(f"{ts()} {asset} STOP {tkr} @ {our_bid:.2f} (dd {dd*100:.0f}%) | {books.line()}")
                                del open_pos[asset]
                                done.add(tkr)
                                continue

            # ---- SETTLE: grade whatever is still open once the window closes
            # Uses the POSITION's own close_time, not the shared `tte` above --
            # `tte` belongs to whatever ticker current_market() returned THIS
            # poll, which is only ever the soonest-closing market with
            # status="open". The instant Kalshi flips pos["ticker"]'s status
            # away from "open" (which can happen within one POLL_SEC of its
            # close_time), current_market() silently stops returning it at
            # all and starts returning the NEXT window instead -- so the
            # shared `tte` jumps straight from a small positive number to a
            # large positive number for an unrelated future window, and
            # `tte < -5` never becomes true for the position that actually
            # just closed. Confirmed empirically: this orphaned 5 real
            # positions since Friday, each silently riding to a full,
            # un-stopped loss with no settle/stop event ever logged, because
            # this check depended on a market object that had already moved
            # on to a different window.
            if asset in open_pos:
                pos = open_pos[asset]
                pos_tte = (datetime.fromisoformat(pos["close_time"].replace("Z", "+00:00")) - now()).total_seconds()
                if pos_tte < -5:
                    res = settle_result(pos["ticker"])
                    if res is not None:
                        won = (res == pos["side"])
                        if pos.get("entry") is not None:
                            books.apply(pos["entry"], won=won)   # paper comparison ledger
                        log({"ev": "settle", "asset": asset, "ticker": pos["ticker"],
                             "side": pos["side"], "result": res, "won": won, "entry": pos.get("entry")})
                        if pos.get("live_order") is not None or pos.get("reconciled"):
                            audit({"ev": "settle", "asset": asset, "ticker": pos["ticker"],
                                   "side": pos["side"], "result": res, "won": won,
                                   "entry": pos.get("entry"), "count": pos.get("count")})
                            outcome = "✅ WIN" if won else "❌ LOSS"
                            alert(f"{outcome} — {asset} {pos['side'].upper()} x{pos.get('count') or '?'} "
                                  f"({pos['ticker']}), entry {pos.get('entry')}")
                        print(f"{ts()} {asset} SETTLE {pos['ticker']} result={res} "
                              f"{'WIN' if won else 'LOSS'} | {books.line()}")
                        del open_pos[asset]
                        done.add(pos["ticker"])

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    try:
        _first_run_wizard()
        run()
    except KeyboardInterrupt:
        print("\nbye")
