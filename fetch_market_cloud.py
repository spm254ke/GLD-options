#!/usr/bin/env python3
"""
fetch_market_cloud.py — runs inside GitHub Actions (no PC involved).

Data sources (all light-signup or keyless):
  - GLD / GLDM / IBIT prices : stooq.com CSV (keyless)
                               -> MarketData.app -> Alpha Vantage fallbacks
  - ATM implied volatility   : MarketData.app (free tier, email signup)
                               -> Alpha Vantage HISTORICAL_OPTIONS (prev close)
  - XAU spot                 : stooq -> goldprice.org -> gold-api.com (keyless)
  - BTC spot                 : Coinbase -> Kraken (keyless)

Env vars (set as GitHub repository secrets):
  MARKETDATA_TOKEN   recommended — free at marketdata.app (100 credits/day)
  ALPHAVANTAGE_KEY   alternative — free at alphavantage.co (25 calls/day;
                     use the hourly cron variant, see README)
  IV_DAYS            optional — expiry distance for the IV read (default 12)

At least one of the two keys is needed for IV; prices work with none.
Missing fields inherit the previous run's value (merge-with-previous), so a
flaky source degrades gracefully instead of blanking the app.
"""
import datetime as dt
import json
import os
import sys

import requests

IV_DAYS = int(os.environ.get("IV_DAYS", "12"))
MD_TOKEN = os.environ.get("MARKETDATA_TOKEN", "")
AV_KEY = os.environ.get("ALPHAVANTAGE_KEY", "")
UNDERLYINGS = ["GLD", "GLDM", "IBIT"]


# ---------------------------------------------------------------- helpers
def jget(url, headers=None, **params):
    r = requests.get(url, params=params or None, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()


def stooq_close(symbol):
    """Keyless CSV: Symbol,Date,Time,Open,High,Low,Close,Volume"""
    rows = requests.get(
        f"https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv",
        timeout=15).text.splitlines()
    v = float(rows[1].split(",")[6])
    return round(v, 2) if v > 0 else None


# ---------------------------------------------------------------- prices
def price_of(sym):
    try:                                   # 1) stooq, keyless
        p = stooq_close(sym.lower() + ".us")
        if p:
            return p
    except Exception:
        pass
    if MD_TOKEN:                           # 2) MarketData (1 credit)
        try:
            j = jget(f"https://api.marketdata.app/v1/stocks/quotes/{sym}/",
                     headers={"Authorization": "Bearer " + MD_TOKEN})
            if j.get("s") == "ok":
                p = (j.get("last") or j.get("mid") or [None])[0]
                if p:
                    return round(float(p), 2)
        except Exception:
            pass
    if AV_KEY:                             # 3) Alpha Vantage
        try:
            j = jget("https://www.alphavantage.co/query",
                     function="GLOBAL_QUOTE", symbol=sym, apikey=AV_KEY)
            p = (j.get("Global Quote") or {}).get("05. price")
            if p:
                return round(float(p), 2)
        except Exception:
            pass
    return None


# ---------------------------------------------------------------- IV
def norm_iv(v):
    if v is None:
        return None
    v = float(v)
    if v <= 0:
        return None
    return round(v * 100, 1) if v < 3 else round(v, 1)


def md_iv(sym, spot):
    """MarketData.app: single ATM contract via delta≈.5 (1 credit),
    fallback to a 4-strike slice (≤4 credits)."""
    hdr = {"Authorization": "Bearer " + MD_TOKEN}
    base = f"https://api.marketdata.app/v1/options/chain/{sym}/"
    try:
        j = jget(base, headers=hdr, dte=IV_DAYS, side="call", delta=".5")
        if j.get("s") == "ok" and j.get("iv"):
            exp = dt.date.fromtimestamp(j["expiration"][0]).isoformat()
            return norm_iv(j["iv"][0]), exp
    except Exception:
        pass
    try:
        j = jget(base, headers=hdr, dte=IV_DAYS, side="call", strikeLimit=4)
        if j.get("s") == "ok" and j.get("iv"):
            ks = j["strike"]
            i = min(range(len(ks)), key=lambda i: abs(float(ks[i]) - spot))
            exp = dt.date.fromtimestamp(j["expiration"][i]).isoformat()
            return norm_iv(j["iv"][i]), exp
    except Exception:
        pass
    return None, None


def av_iv(sym, spot):
    """Alpha Vantage HISTORICAL_OPTIONS: previous-close chain with IV."""
    try:
        j = jget("https://www.alphavantage.co/query",
                 function="HISTORICAL_OPTIONS", symbol=sym, apikey=AV_KEY)
        calls = [d for d in (j.get("data") or []) if d.get("type") == "call"
                 and d.get("implied_volatility")]
        if not calls:
            return None, None
        want = dt.date.today() + dt.timedelta(days=IV_DAYS)
        exp = min({c["expiration"] for c in calls},
                  key=lambda e: abs((dt.date.fromisoformat(e) - want).days))
        pool = [c for c in calls if c["expiration"] == exp]
        c = min(pool, key=lambda c: abs(float(c["strike"]) - spot))
        return norm_iv(c["implied_volatility"]), exp
    except Exception:
        return None, None


def iv_of(sym, spot):
    if MD_TOKEN:
        iv, exp = md_iv(sym, spot)
        if iv:
            return iv, exp
    if AV_KEY:
        return av_iv(sym, spot)
    return None, None


# ---------------------------------------------------------------- spots
def xau_spot():
    try:
        return stooq_close("xauusd")
    except Exception:
        pass
    try:
        j = jget("https://data-asg.goldprice.org/dbXRates/USD",
                 headers={"User-Agent": "Mozilla/5.0"})
        return round(float(j["items"][0]["xauPrice"]), 2)
    except Exception:
        pass
    try:
        j = jget("https://api.gold-api.com/price/XAU")
        return round(float(j["price"]), 2)
    except Exception:
        return None


def btc_spot():
    try:
        j = jget("https://api.coinbase.com/v2/prices/BTC-USD/spot")
        return round(float(j["data"]["amount"]), 2)
    except Exception:
        pass
    try:
        j = jget("https://api.kraken.com/0/public/Ticker?pair=XBTUSD")
        return round(float(list(j["result"].values())[0]["c"][0]), 2)
    except Exception:
        return None


# ---------------------------------------------------------------- main
def load_previous():
    try:
        t = open("market.js").read()
        return json.loads(t[t.index("{"): t.rindex("}") + 1])
    except Exception:
        return {}


def main():
    if not (MD_TOKEN or AV_KEY):
        print("NOTE: no MARKETDATA_TOKEN / ALPHAVANTAGE_KEY — prices only, no IV.")
    old = load_previous()
    old_und = old.get("und") or {}
    print(f"Fetching (IV expiry ≈ {IV_DAYS} days out)…")

    und = {}
    fresh_iv = {}                       # sym -> (iv, exp) fetched THIS run
    for sym in UNDERLYINGS:
        p = price_of(sym)
        iv, exp = (None, None)
        # GLDM's IV is proxied from GLD below (same underlying, identical vol),
        # so never spend MarketData credits on GLDM's thin/flaky chain.
        if p and sym != "GLDM":
            iv, exp = iv_of(sym, p)
        fresh_iv[sym] = (iv, exp)
        prev = old_und.get(sym) or {}
        und[sym] = {"px": p or prev.get("px"),
                    "iv": iv or prev.get("iv"),
                    "ivExp": exp or prev.get("ivExp")}
        stale = " (kept previous)" if (not p or not iv) and prev else ""
        print(f"  {sym:<5} px {und[sym]['px']}   IV {und[sym]['iv']}%"
              f"  ({und[sym]['ivExp']}){stale}")

    # GLDM tracks the same underlying (gold) as GLD, so their implied vol is
    # the same. GLDM's own options chain is thin and usually returns no IV,
    # which would leave it frozen at the previous value. When this run got a
    # fresh GLD IV but no fresh GLDM IV, proxy GLD's onto GLDM.
    gld_iv, gld_exp = fresh_iv.get("GLD", (None, None))
    gldm_iv, _ = fresh_iv.get("GLDM", (None, None))
    if gld_iv and not gldm_iv and "GLDM" in und:
        und["GLDM"]["iv"] = gld_iv
        und["GLDM"]["ivExp"] = gld_exp
        print(f"  GLDM  IV proxied from GLD -> {gld_iv}% ({gld_exp})")

    xau = xau_spot() or old.get("xau")
    btc = btc_spot() or old.get("btc")
    print(f"  XAU spot: {xau}   BTC spot: {btc}")

    data = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "xau": xau, "btc": btc, "und": und}
    with open("market.js", "w") as f:
        f.write("window.MARKET=" + json.dumps(data) + ";")
    print("Wrote market.js")

    if not any(v["px"] for v in und.values()):
        sys.exit("No prices from any source — investigate before trusting output.")


if __name__ == "__main__":
    main()
