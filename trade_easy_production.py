import hashlib
import hmac
import os
import base64
import math
import json
import uuid
import time
import webbrowser
import threading
import unicodedata
from pathlib import Path
from hashlib import sha256
from collections import deque
from datetime import datetime, timezone, timedelta

import pandas as pd
import numpy as np
import requests
import streamlit as st
import streamlit.components.v1 as components
from supabase import create_client, Client

try:
    from streamlit_autorefresh import st_autorefresh
    AUTO_REFRESH_OK = True
except Exception:
    st_autorefresh = None
    AUTO_REFRESH_OK = False

try:
    from fyers_apiv3 import fyersModel
    from fyers_apiv3.FyersWebsocket import data_ws
    FYERS_SDK_OK = True
except Exception:
    fyersModel = None
    data_ws = None
    FYERS_SDK_OK = False

# ============================================================
# TRADE EASY - INDEX TRADING CONFIRMATION SOFTWARE - V11 PRODUCTION
# 5/8 EMA + stable no-blink + production deployment revision
# Built from the supplied PDF functional specification.
#
# IMPORTANT:
# - This is NOT the old 4-Factor application.
# - The four output states are: BUY, SELL, WAIT, BLOCKED.
# - Live broker/exchange data is intentionally an adapter boundary.
# - No automatic live order placement is enabled in this build.
# ============================================================

st.set_page_config(
    page_title="Trade Easy - Index Trading Confirmation",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

def _config_value(name: str, default: str = "") -> str:
    """Read deployment settings from Streamlit secrets first, then env vars."""
    try:
        if name in st.secrets:
            value = st.secrets[name]
            if value is not None:
                return str(value).strip()
    except Exception:
        pass
    return str(os.environ.get(name, default) or "").strip()


SUPABASE_URL = _config_value("SUPABASE_URL")
SUPABASE_PUBLISHABLE_KEY = _config_value("SUPABASE_PUBLISHABLE_KEY")
# Server-only Supabase secret key. Never expose this in the UI or GitHub.
SUPABASE_SECRET_KEY = _config_value("SUPABASE_SECRET_KEY")
# One-time admin bootstrap gate. Keep both values in Streamlit Secrets only.
TRADE_EASY_ADMIN_EMAIL = _config_value("TRADE_EASY_ADMIN_EMAIL", "markam296@gmail.com")
TRADE_EASY_ADMIN_BOOTSTRAP_TOKEN = _config_value("TRADE_EASY_ADMIN_BOOTSTRAP_TOKEN")

TRADE_EASY_PUBLIC_URL = _config_value("TRADE_EASY_PUBLIC_URL")

REDIRECT_URL = _config_value(
    "SUPABASE_REDIRECT_URL",
    TRADE_EASY_PUBLIC_URL
)

FYERS_CONFIG_APP_ID = _config_value("FYERS_APP_ID")
FYERS_CONFIG_SECRET = _config_value("FYERS_SECRET_ID")
FYERS_REDIRECT_URI = _config_value("FYERS_REDIRECT_URI")

if not FYERS_REDIRECT_URI and TRADE_EASY_PUBLIC_URL:
    FYERS_REDIRECT_URI = TRADE_EASY_PUBLIC_URL.rstrip("/") + "/"

if not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY:
    raise RuntimeError(
        "Production configuration missing: set SUPABASE_URL and "
        "SUPABASE_PUBLISHABLE_KEY in Streamlit Secrets/environment."
    )

if not TRADE_EASY_PUBLIC_URL:
    st.warning(
        "TRADE_EASY_PUBLIC_URL is not configured. Set it to the deployed "
        "https://*.streamlit.app URL before using Google/FYERS OAuth."
    )
@st.cache_resource
def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY)

supabase = get_supabase()


# ============================================================
# FYERS AUTH + LIVE DATA ADAPTER
# The PDF architecture keeps broker connectivity behind an adapter.
# This adapter uses the official FYERS v3 OAuth flow and data WebSocket.
# No order placement is enabled here.
# ============================================================

# FYERS_REDIRECT_URI is configured from deployment secrets above.
DEFAULT_FYERS_SYMBOL = "NSE:NIFTY50-INDEX"
INDIA_VIX_SYMBOL = "NSE:INDIAVIX-INDEX"

# Multi-index selector. FYERS recommends using its current Symbol Master files
# for authoritative symbols; these are the standard index symbols used by the app.
INDEX_SYMBOLS = {
    "NIFTY 50": "NSE:NIFTY50-INDEX",
    "BANK NIFTY": "NSE:NIFTYBANK-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCAP NIFTY": "NSE:MIDCPNIFTY-INDEX",
}

# Meaningful-move settings are volatility based, not a fixed 50 points.
# ATR multiplier creates an index/timeframe-specific move threshold while the
# strategy still reports a clear-path opportunity in points.
MEANINGFUL_MOVE_ATR_MULTIPLIER = 2.5
MEANINGFUL_MOVE_MIN_POINTS = {
    "NIFTY 50": 40.0,
    "BANK NIFTY": 90.0,
    "SENSEX": 80.0,
    "FINNIFTY": 45.0,
    "MIDCAP NIFTY": 55.0,
}

# Phase-3 paper trading quantity is permanently fixed.
# This affects PAPER trades only; no FYERS BUY/SELL order is sent.
PAPER_FIXED_QUANTITY = 65
FIXED_TARGET_POINTS = 62.0
FIXED_STOP_LOSS_POINTS = 25.0


def fyers_credentials_present():
    return bool(
        st.session_state.get("fyers_app_id", "").strip()
        and st.session_state.get("fyers_secret", "").strip()
    )


# ============================================================
# CLOUD-SAFE FYERS SESSION HELPERS
# No server-side session / local token files are used in production.
# Access tokens live only in the current Streamlit session.
# ============================================================

def _save_fyers_session(access_token, app_id):
    token = str(access_token or "").strip()
    app = str(app_id or "").strip()
    if not token or not app:
        return False
    st.session_state["fyers_access_token"] = token
    st.session_state["fyers_app_id"] = app
    st.session_state["fyers_login_status"] = "connected"
    return True


def _load_fyers_session():
    token = str(st.session_state.get("fyers_access_token", "") or "").strip()
    app_id = str(st.session_state.get("fyers_app_id", "") or FYERS_CONFIG_APP_ID or "").strip()
    if token and app_id:
        return {"app_id": app_id, "access_token": token, "saved_at": datetime.now(timezone.utc).isoformat()}
    return None


def _clear_fyers_session():
    st.session_state.pop("fyers_access_token", None)
    st.session_state.pop("fyers_login_url", None)
    st.session_state["fyers_login_status"] = "disconnected"


def _restore_fyers_session():
    """Cloud-safe restore: use only the current Streamlit session, never local files."""
    return bool(st.session_state.get("fyers_access_token"))


def _save_fyers_auth_material(app_id, secret_key):
    app_id = str(app_id or "").strip()
    secret_key = str(secret_key or "").strip()
    if not app_id or not secret_key:
        return ""
    app_hash = sha256(f"{app_id}:{secret_key}".encode("utf-8")).hexdigest()
    st.session_state["fyers_app_id"] = app_id
    # Secret is kept only for the current server session; it is never written to disk.
    st.session_state["fyers_secret"] = secret_key
    return app_hash


def _load_fyers_auth_material():
    app_id = str(st.session_state.get("fyers_app_id") or FYERS_CONFIG_APP_ID or "").strip()
    secret = str(st.session_state.get("fyers_secret") or FYERS_CONFIG_SECRET or "").strip()
    if not app_id or not secret:
        return None
    return {
        "app_id": app_id,
        "secret": secret,
        "app_id_hash": sha256(f"{app_id}:{secret}".encode("utf-8")).hexdigest(),
    }


def fyers_make_auth_url(app_id, secret_key):
    if not FYERS_SDK_OK:
        raise RuntimeError("FYERS SDK उपलब्ध नहीं है।")
    if not FYERS_REDIRECT_URI or not FYERS_REDIRECT_URI.startswith("https://"):
        raise RuntimeError(
            "Production में FYERS_REDIRECT_URI को exact HTTPS deployed URL पर सेट करें।"
        )
    _save_fyers_auth_material(app_id, secret_key)
    session = fyersModel.SessionModel(
        client_id=app_id.strip(),
        secret_key=secret_key.strip(),
        redirect_uri=FYERS_REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code",
        state="trade_easy_fyers",
    )
    return session.generate_authcode()


def fyers_exchange_auth_code(app_id, secret_key, auth_code):
    """Exchange a one-time FYERS auth code for an access token."""
    app_id = str(app_id or "").strip()
    secret_key = str(secret_key or "").strip()
    if not app_id or not secret_key:
        raise RuntimeError("FYERS App ID / Secret ID उपलब्ध नहीं हैं।")
    app_hash = sha256(f"{app_id}:{secret_key}".encode("utf-8")).hexdigest()
    response = requests.post(
        "https://api-t1.fyers.in/api/v3/validate-authcode",
        json={
            "grant_type": "authorization_code",
            "appIdHash": app_hash,
            "code": auth_code,
        },
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    try:
        data = response.json()
    except Exception:
        data = {"s": "error", "code": response.status_code, "message": response.text}
    if response.status_code != 200 or not isinstance(data, dict) or data.get("s") != "ok":
        return data
    return data


def fyers_model(access_token, app_id):
    if not FYERS_SDK_OK:
        return None
    return fyersModel.FyersModel(
        client_id=app_id,
        token=access_token,
        is_async=False,
        log_path="",
    )


def fyers_fetch_history(access_token, app_id, symbol, resolution=5, days=5):
    fy = fyers_model(access_token, app_id)
    if fy is None:
        raise RuntimeError("FYERS SDK उपलब्ध नहीं है।")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    data = {
        "symbol": symbol,
        "resolution": str(resolution),
        # FYERS date_format=1 requires calendar dates in YYYY-MM-DD format.
        # Unix timestamps are valid only when date_format=0.
        "date_format": "1",
        "range_from": start.strftime("%Y-%m-%d"),
        "range_to": end.strftime("%Y-%m-%d"),
        "cont_flag": "1",
    }
    response = fy.history(data=data)
    if not isinstance(response, dict) or response.get("s") not in ("ok", "success"):
        raise RuntimeError(f"FYERS history error: {response}")

    candles = response.get("candles") or []
    if not candles:
        raise RuntimeError("FYERS ने कोई candle data नहीं दिया।")

    rows = []
    for row in candles:
        if len(row) >= 6:
            rows.append({
                "timestamp": pd.to_datetime(row[0], unit="s", utc=True),
                "open": row[1],
                "high": row[2],
                "low": row[3],
                "close": row[4],
                "volume": row[5],
            })
    return pd.DataFrame(rows)


def fyers_fetch_quote(access_token, app_id, symbol):
    """Fetch latest LTP through FYERS Quotes API."""
    fy = fyers_model(access_token, app_id)
    if fy is None:
        return None, "FYERS SDK उपलब्ध नहीं है।"
    try:
        response = fy.quotes(data={"symbols": symbol})
        if not isinstance(response, dict) or response.get("s") not in ("ok", "success"):
            return None, f"FYERS quotes error: {response}"
        items = response.get("d") or []
        if isinstance(items, dict):
            items = [items]
        for item in items:
            if not isinstance(item, dict):
                continue
            value = item.get("v") if isinstance(item.get("v"), dict) else item
            for key in ("lp", "ltp", "LTP"):
                if key in value:
                    try:
                        return float(value[key]), None
                    except (TypeError, ValueError):
                        pass
        return None, f"FYERS quotes response में LTP नहीं मिला: {response}"
    except Exception as exc:
        return None, f"FYERS quotes exception: {exc}"


def india_market_status(now=None):
    """Return Indian cash-market session status in Asia/Kolkata time.

    India VIX is labelled LIVE only during the regular NSE cash session
    (weekdays 09:15-15:30 IST). Outside that window a cached value is
    explicitly labelled LAST AVAILABLE rather than being presented as live.
    """
    try:
        from datetime import datetime, time as dt_time
        from zoneinfo import ZoneInfo
        if now is None:
            now = datetime.now(ZoneInfo("Asia/Kolkata"))
        elif now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        if now.weekday() >= 5:
            return False, "MARKET CLOSED"
        open_t = dt_time(9, 15)
        close_t = dt_time(15, 30)
        if open_t <= now.time() <= close_t:
            return True, "LIVE"
        if now.time() < open_t:
            return False, "PRE-MARKET"
        return False, "MARKET CLOSED"
    except Exception:
        return False, "MARKET CLOSED"


def india_vix_snapshot(access_token, app_id, min_interval=5.0):
    """Return cached India VIX quote, refreshed at most once per interval."""
    now = time.time()
    cached = st.session_state.get("trade_easy_vix_value")
    cached_at = float(st.session_state.get("trade_easy_vix_at", 0.0))
    if cached is not None and now - cached_at < min_interval:
        value = float(cached)
        previous = st.session_state.get("trade_easy_vix_previous")
        trend = "RISING" if previous is not None and value > float(previous) + 0.01 else ("FALLING" if previous is not None and value < float(previous) - 0.01 else "FLAT")
        return value, trend, st.session_state.get("trade_easy_vix_error")
    value, err = fyers_fetch_quote(access_token, app_id, INDIA_VIX_SYMBOL)
    if value is not None and np.isfinite(value):
        if cached is not None:
            st.session_state["trade_easy_vix_previous"] = float(cached)
        st.session_state["trade_easy_vix_value"] = float(value)
        st.session_state["trade_easy_vix_at"] = now
        st.session_state["trade_easy_vix_error"] = None
        previous = st.session_state.get("trade_easy_vix_previous")
        trend = "RISING" if previous is not None and value > float(previous) + 0.01 else ("FALLING" if previous is not None and value < float(previous) - 0.01 else "FLAT")
        return float(value), trend, None
    st.session_state["trade_easy_vix_at"] = now
    st.session_state["trade_easy_vix_error"] = err
    if cached is not None:
        value = float(cached)
        previous = st.session_state.get("trade_easy_vix_previous")
        trend = "RISING" if previous is not None and value > float(previous) + 0.01 else ("FALLING" if previous is not None and value < float(previous) - 0.01 else "FLAT")
        return value, trend, err
    return None, "—", err



def fyers_history_snapshot(access_token, app_id, symbol, resolution=5, days=5, min_interval=4.0):
    """Cache completed-candle history between strategy-fragment ticks."""
    now = time.time()
    cache = st.session_state.setdefault("trade_easy_history_cache", {})
    at = st.session_state.setdefault("trade_easy_history_at", {})
    errors = st.session_state.setdefault("trade_easy_history_error", {})
    key = f"{symbol}|{int(resolution)}|{int(days)}"
    cached = cache.get(key)
    if cached is not None and now - float(at.get(key, 0.0)) < float(min_interval):
        return cached.copy(), errors.get(key)
    try:
        raw = fyers_fetch_history(access_token, app_id, symbol, resolution=resolution, days=days)
        if raw is None or raw.empty:
            raise RuntimeError("FYERS history returned no candles.")
        cache[key] = raw.copy(); at[key] = now; errors[key] = None
        return cache[key].copy(), None
    except Exception as exc:
        errors[key] = str(exc)
        if cached is not None:
            return cached.copy(), str(exc)
        raise


def fyers_fetch_option_chain(access_token, app_id, symbol, strikecount=20):
    """Read-only FYERS option-chain adapter with SDK + REST fallback.

    The option-chain endpoint is a snapshot endpoint. Continuous LTP is supplied
    separately by the WebSocket subscriptions below; OI/volume update whenever
    FYERS publishes a newer value.
    """
    payload = {"symbol": symbol, "strikecount": int(strikecount), "timestamp": "", "greeks": "1"}
    sdk_error = None

    def parse_response(response):
        if not isinstance(response, dict):
            return None, f"Unexpected FYERS option-chain response: {response!r}"
        if response.get("s") == "error":
            return None, f"FYERS option-chain error: {response}"
        data = response.get("data") or {}
        chain = data.get("optionsChain") or []
        rows = []
        for item in chain:
            if not isinstance(item, dict):
                continue
            typ = item.get("option_type")
            if typ not in ("CE", "PE"):
                continue
            def nf(v):
                try:
                    return float(v) if v not in (None, "") else np.nan
                except Exception:
                    return np.nan
            greeks = item.get("greeks") if isinstance(item.get("greeks"), dict) else {}
            rows.append({
                "strike": nf(item.get("strike_price")),
                "type": typ,
                "symbol": item.get("symbol") or item.get("fyToken") or item.get("option_symbol"),
                "ltp": nf(item.get("ltp")),
                "ltp_change": nf(item.get("ltpch")),
                "volume": nf(item.get("volume") if item.get("volume") is not None else item.get("vol_traded_today")),
                "oi": nf(item.get("oi")),
                "oi_change": nf(item.get("oich") if item.get("oich") is not None else item.get("oi_change")),
                "iv": nf(item.get("iv") if item.get("iv") is not None else greeks.get("iv")),
                "delta": nf(item.get("delta") if item.get("delta") is not None else greeks.get("delta")),
                "theta": nf(item.get("theta") if item.get("theta") is not None else greeks.get("theta")),
            })
        df = pd.DataFrame(rows)
        if df.empty:
            return None, "FYERS option-chain returned no CE/PE rows."
        return df, None

    # Primary: installed FYERS SDK.
    try:
        fy = fyers_model(access_token, app_id)
        method = getattr(fy, "optionchain", None) if fy is not None else None
        if callable(method):
            df, err = parse_response(method(data=payload))
            if df is not None:
                return df, None
            sdk_error = err
        else:
            sdk_error = "Installed FYERS SDK does not expose optionchain()."
    except Exception as exc:
        sdk_error = f"FYERS SDK option-chain exception: {exc}"

    # Fallback: the documented FYERS v3 option-chain endpoint. This is still
    # read-only and is used only when the SDK adapter cannot return the snapshot.
    try:
        resp = requests.get(
            "https://api-t1.fyers.in/data/options-chain-v3",
            params=payload,
            headers={"Authorization": f"{app_id}:{access_token}", "Content-Type": "application/json"},
            timeout=5,
        )
        df, err = parse_response(resp.json())
        if df is not None:
            return df, None
        return None, err or sdk_error
    except Exception as exc:
        return None, f"SDK: {sdk_error} | REST fallback: {exc}"



_OPTION_WS_CACHE = {}
_OPTION_WS_LOCK = threading.Lock()

def fyers_option_live_feed(access_token, app_id, chain_df):
    """Persistent FYERS WebSocket for option-contract ticks.
    LTP is updated tick-by-tick; OI/OI-change/volume remain from the latest
    option-chain snapshot unless FYERS sends a newer value for that contract.
    """
    if chain_df is None or chain_df.empty or not FYERS_SDK_OK or data_ws is None:
        return {}
    symbols = [str(x).strip() for x in chain_df.get("symbol", pd.Series(dtype=str)).dropna().tolist() if str(x).strip()]
    if not symbols:
        return {}
    key = f"{app_id}:{hashlib.sha256(str(access_token).encode()).hexdigest()[:12]}"
    with _OPTION_WS_LOCK:
        item = _OPTION_WS_CACHE.get(key)
        if item and item.get("thread") and item["thread"].is_alive():
            missing = [x for x in symbols if x not in item["symbols"]]
            if missing:
                try:
                    item["socket"].subscribe(symbols=missing, data_type="SymbolUpdate")
                    item["symbols"].update(missing)
                except Exception:
                    pass
            return item["ticks"]
        ticks = {}
        lock = threading.Lock()

    def on_message(message):
        if not isinstance(message, dict):
            return
        sym = message.get("symbol") or message.get("fyToken") or message.get("symbol_name")
        if not sym:
            return
        ltp = parse_live_price(message)
        with lock:
            ticks[str(sym)] = {
                **ticks.get(str(sym), {}),
                "ltp": ltp if ltp is not None else ticks.get(str(sym), {}).get("ltp"),
                "volume": message.get("volume", message.get("vol_traded_today", ticks.get(str(sym), {}).get("volume"))),
                "oi": message.get("oi", ticks.get(str(sym), {}).get("oi")),
                "oi_change": message.get("oich", message.get("oi_change", ticks.get(str(sym), {}).get("oi_change"))),
                "received_at": time.time(),
            }

    def on_error(message):
        with lock:
            ticks["__socket_error__"] = {"error": str(message), "received_at": time.time()}
    def on_close(message):
        with lock:
            ticks["__socket_status__"] = {"status": "CLOSED", "message": str(message) if message else "closed", "received_at": time.time()}
    def on_connect():
        try:
            socket.subscribe(symbols=symbols, data_type="SymbolUpdate")
        except Exception:
            pass

    socket = data_ws.FyersDataSocket(
        access_token=f"{app_id}:{access_token}", log_path="", litemode=False,
        write_to_file=False, reconnect=True, on_connect=on_connect,
        on_close=on_close, on_error=on_error, on_message=on_message,
    )
    def runner():
        try:
            socket.connect()
            socket.keep_running()
        except Exception:
            pass
    thread = threading.Thread(target=runner, daemon=True, name="trade-easy-option-ws")
    with _OPTION_WS_LOCK:
        _OPTION_WS_CACHE[key] = {"ticks": ticks, "thread": thread, "socket": socket, "symbols": set(symbols)}
    thread.start()
    return ticks


def merge_option_ticks(chain_df, ticks):
    if chain_df is None or chain_df.empty or not ticks:
        return chain_df
    df = chain_df.copy()
    for i, row in df.iterrows():
        sym = row.get("symbol")
        tick = ticks.get(str(sym)) if sym is not None else None
        if not tick:
            continue
        for field in ("ltp", "volume", "oi", "oi_change"):
            val = tick.get(field)
            if val is not None:
                try:
                    df.at[i, field] = float(val)
                except Exception:
                    pass
    return df


def _fmt_chain_num(v):
    try:
        x=float(v)
        if not np.isfinite(x): return "—"
        ax=abs(x)
        if ax>=1e7: return f"{x/1e7:.2f}Cr"
        if ax>=1e5: return f"{x/1e5:.2f}L"
        if ax>=1e3: return f"{x/1e3:.1f}K"
        return f"{x:.0f}"
    except Exception:
        return "—"


def _option_chain_max_pain(chain_df):
    if chain_df is None or chain_df.empty:
        return np.nan
    try:
        ce=chain_df[chain_df["type"]=="CE"].dropna(subset=["strike","oi"])
        pe=chain_df[chain_df["type"]=="PE"].dropna(subset=["strike","oi"])
        strikes=sorted(set(chain_df["strike"].dropna().astype(float)))
        if not strikes or ce.empty or pe.empty: return np.nan
        losses=[]
        for k in strikes:
            call_loss=float(((k-ce["strike"]).clip(lower=0)*ce["oi"]).sum())
            put_loss=float(((pe["strike"]-k).clip(lower=0)*pe["oi"]).sum())
            losses.append((call_loss+put_loss,k))
        return float(min(losses)[1])
    except Exception:
        return np.nan


def render_professional_option_chain(chain_df, live_price, index_name, strike_count=10):
    """Render a stable, read-only FYERS-style CE/PE option chain.
    It never sends an order. Missing Greeks are displayed as — rather than invented.
    """
    if chain_df is None or chain_df.empty:
        st.caption("Option-chain data अभी उपलब्ध नहीं है।")
        return

    df=chain_df.copy()
    df["strike"]=pd.to_numeric(df["strike"],errors="coerce")
    df=df.dropna(subset=["strike"])
    if df.empty: return
    atm=(round(float(live_price)/50.0)*50.0) if live_price is not None else float(df["strike"].median())
    strikes=sorted(df["strike"].unique(), key=lambda x: abs(float(x)-atm))[:(strike_count*2+1)]
    strikes=sorted(strikes)
    view=df[df["strike"].isin(strikes)].copy()
    if view.empty: return

    # If the feed ever supplies Greeks, preserve them; otherwise do not fabricate values.
    for c in ("delta","theta"):
        if c not in view.columns: view[c]=np.nan
    max_oi=float(pd.to_numeric(view["oi"],errors="coerce").fillna(0).max() or 0)
    call_wall=float(df.loc[df["type"].eq("CE")].sort_values("oi",ascending=False).iloc[0]["strike"]) if not df.loc[df["type"].eq("CE")].empty else np.nan
    put_wall=float(df.loc[df["type"].eq("PE")].sort_values("oi",ascending=False).iloc[0]["strike"]) if not df.loc[df["type"].eq("PE")].empty else np.nan
    max_pain=_option_chain_max_pain(df)

    def side_row(row, side):
        if row is None: return ["—"]*7
        ltp=row.get("ltp",np.nan); chg=row.get("oi_change",np.nan); oi=row.get("oi",np.nan)
        delta=row.get("delta",np.nan); theta=row.get("theta",np.nan)
        bar=(max(0,min(100,float(oi)/max_oi*100)) if max_oi>0 and pd.notna(oi) else 0)
        chg_cls="pos" if pd.notna(chg) and float(chg)>0 else ("neg" if pd.notna(chg) and float(chg)<0 else "")
        ltp_s=f"{float(ltp):,.2f}" if pd.notna(ltp) else "—"
        chg_s=f"{float(chg):,.0f}" if pd.notna(chg) else "—"
        return [f"{float(theta):.2f}" if pd.notna(theta) else "—", f"{float(delta):.2f}" if pd.notna(delta) else "—", chg_s, ltp_s, _fmt_chain_num(oi), f"<span class='oi-bar'><i style='width:{bar:.0f}%'></i></span>", chg_cls]

    rows=[]
    for strike in strikes:
        ce=view[(view["strike"]==strike)&(view["type"]=="CE")]
        pe=view[(view["strike"]==strike)&(view["type"]=="PE")]
        cer=ce.iloc[0] if not ce.empty else None; per=pe.iloc[0] if not pe.empty else None
        c=side_row(cer,"CE"); p=side_row(per,"PE")
        badges=[]
        if abs(float(strike)-atm)<0.001: badges.append("ATM")
        if pd.notna(call_wall) and abs(float(strike)-call_wall)<0.001: badges.append("OI Resistance")
        if pd.notna(put_wall) and abs(float(strike)-put_wall)<0.001: badges.append("OI Support")
        if pd.notna(max_pain) and abs(float(strike)-max_pain)<0.001: badges.append("Max Pain")
        badge=" ".join(f"<span class='badge'>{b}</span>" for b in badges)
        atm_cls=" atm" if abs(float(strike)-atm)<0.001 else ""
        rows.append(f"<tr class='{atm_cls}'><td class='call'>{c[0]}</td><td class='call'>{c[1]}</td><td class='call {c[6]}'>{c[2]}</td><td class='call ltp'>{c[3]}</td><td class='strike'>{float(strike):,.0f}<div>{badge}</div></td><td class='put ltp'>{p[3]}</td><td class='put {p[6]}'>{p[2]}</td><td class='put'>{p[1]}</td><td class='put'>{p[0]}</td></tr>")

    html=f"""
    <style>
    .te-oc-wrap{{border:1px solid rgba(148,163,184,.18);border-radius:12px;overflow:hidden;background:#0e131b;box-shadow:0 8px 24px rgba(0,0,0,.16)}}
    .te-oc-head{{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;background:#121923;border-bottom:1px solid rgba(148,163,184,.14)}}
    .te-oc-title{{font-weight:700;font-size:15px}} .te-oc-meta{{font-size:11px;color:#9aa7b8}}
    .te-oc-table{{width:100%;border-collapse:collapse;font-size:12px}} .te-oc-table th{{padding:8px 6px;color:#9aa7b8;font-weight:600;border-bottom:1px solid rgba(148,163,184,.13)}}
    .te-oc-table td{{padding:7px 6px;text-align:right;border-bottom:1px solid rgba(148,163,184,.08);white-space:nowrap}}
    .te-oc-table .strike{{text-align:center;font-weight:700;background:#202631;color:#e8edf5;border-left:1px solid rgba(148,163,184,.14);border-right:1px solid rgba(148,163,184,.14);min-width:105px}}
    .te-oc-table .call{{color:#e6b4bf}} .te-oc-table .put{{color:#7bd5df}} .te-oc-table .ltp{{font-weight:700}} .te-oc-table .pos{{color:#31c48d}} .te-oc-table .neg{{color:#ff657c}}
    .te-oc-table tr.atm td{{border-top:1px solid #fff;border-bottom:1px solid #fff;background:rgba(148,163,184,.045)}}
    .badge{{display:inline-block;font-size:8px;padding:2px 4px;border-radius:4px;background:#273142;color:#aab8ca;margin-top:3px;font-weight:600}}
    .oc-sub{{font-size:10px;color:#78879a;margin-top:2px}} .oc-summary{{display:flex;gap:8px;padding:8px;background:#0b1017;border-top:1px solid rgba(148,163,184,.12);font-size:11px;color:#aab5c4;flex-wrap:wrap}}
    .oc-pill{{padding:5px 8px;border-radius:6px;background:#151d28}} .oc-pill b{{color:#f1f5f9}}
    </style>
    <div class='te-oc-wrap'>
      <div class='te-oc-head'><div class='te-oc-title'>📊 {index_name} Option Chain <span style='color:#5eead4'>• LIVE READ-ONLY</span></div><div class='te-oc-meta'>CALL ← &nbsp; STRIKE &nbsp; → PUT &nbsp; | ATM ±{strike_count}</div></div>
      <table class='te-oc-table'><thead><tr><th colspan='4'>CALL</th><th>STRIKE</th><th colspan='4'>PUT</th></tr><tr><th>Theta</th><th>Delta</th><th>OI Chg</th><th>LTP</th><th>OI / Level</th><th>LTP</th><th>OI Chg</th><th>Delta</th><th>Theta</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
      <div class='oc-summary'><span class='oc-pill'>Spot <b>{f'{float(live_price):,.2f}' if live_price is not None else '—'}</b></span><span class='oc-pill'>OI Resistance <b>{f'{call_wall:,.0f}' if pd.notna(call_wall) else '—'}</b></span><span class='oc-pill'>OI Support <b>{f'{put_wall:,.0f}' if pd.notna(put_wall) else '—'}</b></span><span class='oc-pill'>Max Pain <b>{f'{max_pain:,.0f}' if pd.notna(max_pain) else '—'}</b></span></div>
    </div>
    """
    st.markdown(html, unsafe_allow_html=True)


def option_chain_snapshot(access_token, app_id, symbol, min_interval=3.0):
    """Return a symbol-scoped last-good option-chain snapshot."""
    now = time.time()
    cache = st.session_state.setdefault("trade_easy_option_chain_cache_by_symbol", {})
    at_cache = st.session_state.setdefault("trade_easy_option_chain_at_by_symbol", {})
    err_cache = st.session_state.setdefault("trade_easy_option_chain_error_by_symbol", {})
    last_good_cache = st.session_state.setdefault("trade_easy_option_chain_last_good_by_symbol", {})

    cached = cache.get(symbol)
    cached_at = float(at_cache.get(symbol, 0.0))
    if cached is not None and now - cached_at < float(min_interval):
        return cached, err_cache.get(symbol)

    df, err = fyers_fetch_option_chain(access_token, app_id, symbol, strikecount=20)
    if df is not None and not df.empty:
        cache[symbol] = df.copy()
        at_cache[symbol] = now
        err_cache[symbol] = None
        last_good_cache[symbol] = now
        return cache[symbol], None

    err_cache[symbol] = err
    return cached, err


def summarize_option_chain(chain_df):
    empty = {"available": False, "call_oi": np.nan, "put_oi": np.nan, "call_oi_change": np.nan,
             "put_oi_change": np.nan, "pcr": np.nan, "call_wall": np.nan, "put_wall": np.nan}
    if chain_df is None or chain_df.empty:
        return empty
    ce, pe = chain_df[chain_df.type == "CE"], chain_df[chain_df.type == "PE"]
    call_oi, put_oi = float(ce.oi.sum()), float(pe.oi.sum())
    call_chg, put_chg = float(ce.oi_change.sum()), float(pe.oi_change.sum())
    call_wall = float(ce.loc[ce.oi.idxmax(), "strike"]) if not ce.oi.dropna().empty else np.nan
    put_wall = float(pe.loc[pe.oi.idxmax(), "strike"]) if not pe.oi.dropna().empty else np.nan
    return {"available": True, "call_oi": call_oi, "put_oi": put_oi,
            "call_oi_change": call_chg, "put_oi_change": put_chg,
            "pcr": put_oi / call_oi if call_oi > 0 else np.nan,
            "call_wall": call_wall, "put_wall": put_wall}



def _tf_clean_delta(value):
    """Normalize broker delta to a 0..100 percentage-like value."""
    try:
        x=float(value)
        if not np.isfinite(x):
            return np.nan
        # FYERS-style Greeks are normally decimal (-1..1); tolerate percent-like feeds too.
        if abs(x) <= 1.5:
            return x * 100.0
        return x
    except Exception:
        return np.nan


def _tf_theta_ok(value, low=-10.0, high=-5.0):
    try:
        x=float(value)
        return bool(np.isfinite(x) and low <= x <= high)
    except Exception:
        return False


def _tf_delta_ok(value, minimum=70.0):
    try:
        x=_tf_clean_delta(value)
        return bool(np.isfinite(x) and x >= minimum)
    except Exception:
        return False


def _tf_bool(value):
    return bool(value) if value is not None else False


def _tf_safe_float(value):
    try:
        x=float(value)
        return x if np.isfinite(x) else np.nan
    except Exception:
        return np.nan


def _tf_option_history_record(chain_df, timestamp=None):
    """Create a compact immutable-in-session OI snapshot for 15/30/60m comparisons."""
    if chain_df is None or chain_df.empty:
        return None
    try:
        ce=chain_df[chain_df["type"].eq("CE")]
        pe=chain_df[chain_df["type"].eq("PE")]
        ts=float(timestamp if timestamp is not None else time.time())
        return {
            "ts": ts,
            "call_oi": float(pd.to_numeric(ce.get("oi", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()),
            "put_oi": float(pd.to_numeric(pe.get("oi", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()),
            "call_oi_change": float(pd.to_numeric(ce.get("oi_change", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()),
            "put_oi_change": float(pd.to_numeric(pe.get("oi_change", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()),
        }
    except Exception:
        return None


def record_option_oi_history(symbol, chain_df, max_age_seconds=3900.0):
    """Persist only compact option OI totals; bounded to about one trading hour."""
    rec=_tf_option_history_record(chain_df)
    if rec is None:
        return []
    store=st.session_state.setdefault("trade_easy_oi_history_v2", {})
    rows=list(store.get(symbol, []))
    # Avoid adding identical snapshots every fragment cycle.
    if rows:
        last=rows[-1]
        same=(abs(float(last.get("call_oi", 0))-rec["call_oi"]) < 0.5
              and abs(float(last.get("put_oi", 0))-rec["put_oi"]) < 0.5
              and abs(float(last.get("call_oi_change", 0))-rec["call_oi_change"]) < 0.5
              and abs(float(last.get("put_oi_change", 0))-rec["put_oi_change"]) < 0.5)
        if same and rec["ts"]-float(last.get("ts", 0)) < 60:
            return rows
    rows.append(rec)
    cutoff=rec["ts"]-float(max_age_seconds)
    rows=[r for r in rows if float(r.get("ts", 0)) >= cutoff]
    store[symbol]=rows[-240:]
    return store[symbol]


def _tf_history_at_or_before(rows, seconds_back, now=None):
    if not rows:
        return None
    now=float(now if now is not None else time.time())
    target=now-float(seconds_back)
    eligible=[r for r in rows if float(r.get("ts", 0)) <= target]
    return eligible[-1] if eligible else None


def option_oi_history_v2(symbol, chain_df):
    """Return current OI and 15/30/60 minute changes where history exists."""
    rows=record_option_oi_history(symbol, chain_df)
    current=rows[-1] if rows else _tf_option_history_record(chain_df)
    out={"available": bool(current), "current": current, "rows": len(rows)}
    now=float(current["ts"]) if current else time.time()
    for label, seconds in (("15m",900),("30m",1800),("60m",3600)):
        base=_tf_history_at_or_before(rows, seconds, now=now)
        if base is None or current is None:
            out[label]={"available":False,"call_oi_change":np.nan,"put_oi_change":np.nan,
                        "call_oi_change_pct":np.nan,"put_oi_change_pct":np.nan}
            continue
        c0=float(base.get("call_oi",0)); p0=float(base.get("put_oi",0))
        cc=float(current.get("call_oi",0)); pp=float(current.get("put_oi",0))
        out[label]={
            "available":True,
            "call_oi_change":cc-c0,
            "put_oi_change":pp-p0,
            "call_oi_change_pct":((cc-c0)/c0*100.0 if c0 else np.nan),
            "put_oi_change_pct":((pp-p0)/p0*100.0 if p0 else np.nan),
            "age_minutes":(now-float(base.get("ts",now)))/60.0,
        }
    return out


def _tf_direction_from_result(result):
    if not result:
        return None
    d=result.get("direction")
    return d if d in ("LONG","SHORT") else None


def _tf_evaluate_timeframe(df, timeframe, index_name, option_summary=None, live_price=None):
    """Evaluate one completed timeframe without paper execution or broker orders."""
    empty={"timeframe":timeframe,"available":False,"direction":None,"bias":"UNKNOWN","structure":"UNKNOWN",
           "score":0,"ema_confirmed":False,"vwap":False,"volume":False,"sweep":False,"bos":False,"retest":False,
           "data_ok":False,"level_status":"WAITING","clear_path":np.nan,"meaningful_move":np.nan,"reasons":[]}
    try:
        if df is None or df.empty or len(df)<30:
            return empty
        work=normalize_candles(df)
        if work.empty or len(work)<30:
            return empty
        now=pd.Timestamp.now(tz="UTC")
        interval=pd.Timedelta(minutes=int(timeframe))
        while len(work) and work["timestamp"].iloc[-1]+interval>now:
            work=work.iloc[:-1].reset_index(drop=True)
        if len(work)<30:
            return empty
        data_ok,data_reasons=validate_candles(work,int(timeframe),max_stale_minutes=max(20,int(timeframe)*2))
        work=add_indicators(work)
        levels=key_levels(work)
        pa=price_action_checks(work,levels)
        score,direction,bias,structure,reasons,invalidations=score_signal(work,levels,pa,int(timeframe))
        ema_state=ema_5_8_confirmation(work,direction)
        confirmations=sum(bool(pa.get(k)) for k in ("sweep_confirmed","structure_break","retest_confirmed"))
        px=float(live_price) if live_price is not None else float(work["close"].iloc[-1])
        level_setup=level_setup_engine(px,direction,bias,levels,score,confirmations,option_summary,df=work,index_name=index_name)
        return {
            "timeframe":int(timeframe),"available":True,"direction":direction,"bias":bias,"structure":structure,
            "score":int(score),"ema_confirmed":bool(ema_state.get("confirmed")),
            "vwap":bool(direction=="LONG" and work["close"].iloc[-1]>work["vwap"].iloc[-1] or direction=="SHORT" and work["close"].iloc[-1]<work["vwap"].iloc[-1]),
            "volume":bool(pd.notna(work["volume_ma"].iloc[-1]) and work["volume"].iloc[-1]>work["volume_ma"].iloc[-1]),
            "sweep":bool(pa.get("sweep_confirmed")),"bos":bool(pa.get("structure_break")),"retest":bool(pa.get("retest_confirmed")),
            "confirmation_count":confirmations,"data_ok":bool(data_ok),"data_reasons":data_reasons,
            "level_status":level_setup.get("status","WAITING"),"clear_path":level_setup.get("path",{}).get("clear_path",np.nan),
            "meaningful_move":level_setup.get("path",{}).get("meaningful_move",np.nan),
            "ema_trend":ema_state.get("trend"),"ema_cross":ema_state.get("cross"),
            "reasons":reasons,"invalidations":invalidations,
            "timestamp":str(work["timestamp"].iloc[-1]),
        }
    except Exception as exc:
        empty["error"]=str(exc)
        return empty


def _tf_best_option(chain_df, direction):
    """Find a real broker-supplied option satisfying the requested Greek filters."""
    result={"available":False,"reason":"NO_MATCH","type":None,"strike":np.nan,"ltp":np.nan,
            "delta":np.nan,"theta":np.nan,"oi":np.nan,"oi_change":np.nan,"volume":np.nan}
    if chain_df is None or chain_df.empty or direction not in ("LONG","SHORT"):
        result["reason"]="NO_DIRECTION_OR_CHAIN"
        return result
    try:
        typ="CE" if direction=="LONG" else "PE"
        df=chain_df[chain_df["type"].eq(typ)].copy()
        if df.empty:
            result["reason"]="NO_DIRECTION_OPTION"
            return result
        for c in ("strike","ltp","delta","theta","oi","oi_change","volume"):
            if c in df.columns:
                df[c]=pd.to_numeric(df[c],errors="coerce")
            else:
                df[c]=np.nan
        df["delta_pct"]=df["delta"].map(_tf_clean_delta)
        # Calls need positive delta; puts need magnitude of negative delta.
        df["delta_abs_pct"]=df["delta"].abs().map(_tf_clean_delta)
        delta_mask=df["delta_abs_pct"]>=70.0
        theta_mask=df["theta"].between(-10.0,-5.0,inclusive="both")
        candidates=df[delta_mask & theta_mask].copy()
        if candidates.empty:
            result["reason"]="NO_GREEK_MATCH_DELTA70_THETA_5_10"
            return result
        # Prefer liquid contracts, then delta closest to 70 without violating the floor.
        candidates["_liq"]=candidates["volume"].fillna(0)+candidates["oi"].fillna(0)*0.001
        candidates["_delta_gap"]=(candidates["delta_abs_pct"]-70.0).abs()
        candidates=candidates.sort_values(["_liq","_delta_gap"],ascending=[False,True])
        row=candidates.iloc[0]
        result.update({"available":True,"reason":"GREEKS_MATCH","type":typ,
                       "strike":_tf_safe_float(row.get("strike")),"ltp":_tf_safe_float(row.get("ltp")),
                       "delta":_tf_safe_float(row.get("delta")),"delta_pct":_tf_safe_float(row.get("delta_abs_pct")),
                       "theta":_tf_safe_float(row.get("theta")),"oi":_tf_safe_float(row.get("oi")),
                       "oi_change":_tf_safe_float(row.get("oi_change")),"volume":_tf_safe_float(row.get("volume"))})
        return result
    except Exception as exc:
        result["reason"]="OPTION_FILTER_ERROR:"+str(exc)
        return result


def trade_finder_v2(timeframe_results, option_summary, option_history, chain_df, live_price, index_name):
    """Unified Trade Finder V2: MTF + price action + OI/PCR + Greek-filtered contract."""
    out={"status":"WAIT","direction":None,"score":0,"mtf_alignment":"0/3","option":{"available":False},
         "oi_history":option_history or {},"reasons":[],"blocks":[],"clear_path":np.nan,"entry":np.nan,
         "stop_loss":np.nan,"target":np.nan,"risk_reward":np.nan}
    try:
        usable=[timeframe_results.get(k,{}) for k in (15,30,60)]
        dirs=[_tf_direction_from_result(x) for x in usable]
        bull=sum(d=="LONG" for d in dirs); bear=sum(d=="SHORT" for d in dirs)
        if bull>=2 and bull>bear:
            direction="LONG"
        elif bear>=2 and bear>bull:
            direction="SHORT"
        else:
            direction=None
        out["direction"]=direction
        out["mtf_alignment"]=f"{max(bull,bear)}/3"
        # Score is transparent and bounded; it is not a prediction probability.
        base=0
        if direction:
            for r in usable:
                if _tf_direction_from_result(r)==direction:
                    base+=15
                    if r.get("score",0)>=75: base+=5
                    if r.get("ema_confirmed"): base+=4
                    if r.get("vwap"): base+=3
                    if r.get("volume"): base+=2
                    if r.get("sweep"): base+=2
                    if r.get("bos"): base+=2
                    if r.get("retest"): base+=2
        pcr=_tf_safe_float((option_summary or {}).get("pcr"))
        call_chg=_tf_safe_float((option_summary or {}).get("call_oi_change"))
        put_chg=_tf_safe_float((option_summary or {}).get("put_oi_change"))
        if np.isfinite(pcr):
            if direction=="LONG" and pcr>1: base+=5
            elif direction=="SHORT" and pcr<1: base+=5
        if direction=="LONG" and np.isfinite(put_chg) and np.isfinite(call_chg) and put_chg>call_chg: base+=5
        if direction=="SHORT" and np.isfinite(call_chg) and np.isfinite(put_chg) and call_chg>put_chg: base+=5
        out["score"]=int(min(100,max(0,base)))
        contract=_tf_best_option(chain_df,direction)
        out["option"]=contract
        if direction is None:
            out["blocks"].append("MTF_NOT_ALIGNED")
        if not contract.get("available"):
            out["blocks"].append(contract.get("reason","OPTION_NOT_FOUND"))
        # Use the selected timeframe's latest completed price for a consistent plan.
        primary=next((r for r in usable if r.get("available")),{})
        if direction and primary.get("available"):
            px=float(live_price) if live_price is not None else np.nan
            if not np.isfinite(px):
                # Only use a completed candle price if available from the result context.
                px=np.nan
            # The existing plan function uses the dataframe; this V2 layer intentionally
            # uses the configured fixed exits to remain compatible with the paper engine.
            if np.isfinite(px):
                if direction=="LONG":
                    out["entry"]=px; out["stop_loss"]=px-FIXED_STOP_LOSS_POINTS; out["target"]=px+FIXED_TARGET_POINTS
                else:
                    out["entry"]=px; out["stop_loss"]=px+FIXED_STOP_LOSS_POINTS; out["target"]=px-FIXED_TARGET_POINTS
                out["risk_reward"]=FIXED_TARGET_POINTS/FIXED_STOP_LOSS_POINTS
        # Final state is intentionally conservative: MTF 2/3 is necessary but not sufficient.
        hard_ok=(direction is not None and contract.get("available") and out["score"]>=70)
        if hard_ok:
            out["status"]="CANDIDATE"
        else:
            out["status"]="WAIT"
        return out
    except Exception as exc:
        out["status"]="WAIT"; out["blocks"]=["ENGINE_ERROR:"+str(exc)]
        return out


def render_trade_finder_v2(result=None, timeframe_results=None, option_history=None):
    """Render a persistent V2 panel even when live data is unavailable.

    The panel is a permanent UI shell. Live values are filled only when the
    required market snapshot is available; otherwise the cards remain visible
    with WAITING / — states.
    """
    result = result or {}
    timeframe_results = timeframe_results or {}
    option_history = option_history or {}
    st.markdown('<div class="section-head">🧠 Trade Finder Engine V2</div>', unsafe_allow_html=True)
    status=result.get("status","WAITING FOR LIVE DATA")
    direction=result.get("direction") or "—"
    status_icon={"CANDIDATE":"🟢","WAIT":"🟡","BLOCKED":"⛔"}.get(status,"🟡")
    st.markdown(
        f'<div class="state-box" style="padding:14px 18px;">'
        f'<div class="state-title" style="font-size:30px;">{status_icon} {status} • {direction}</div>'
        f'<div class="state-sub">MTF Alignment: {result.get("mtf_alignment","0/3")} • Engine Score: {int(result.get("score",0))}/100</div>'
        f'</div>',unsafe_allow_html=True)
    c1,c2,c3,c4,c5=st.columns(5)
    c1.metric("15m", (timeframe_results.get(15) or {}).get("bias","—"))
    c2.metric("30m", (timeframe_results.get(30) or {}).get("bias","—"))
    c3.metric("60m", (timeframe_results.get(60) or {}).get("bias","—"))
    c4.metric("MTF", result.get("mtf_alignment","0/3"))
    c5.metric("Score", f'{int(result.get("score",0))}/100')
    st.markdown('<div class="section-head">MTF Confirmation Matrix</div>', unsafe_allow_html=True)
    rows=[]
    for tf in (15,30,60):
        r=timeframe_results.get(tf,{})
        rows.append({"TF":f"{tf}m","Direction":r.get("direction") or "—","Bias":r.get("bias","—"),
                     "Score":r.get("score",0),"EMA 5/8":"PASS" if r.get("ema_confirmed") else "WAIT",
                     "VWAP":"PASS" if r.get("vwap") else "WAIT","Volume":"PASS" if r.get("volume") else "WAIT",
                     "Sweep":"PASS" if r.get("sweep") else "WAIT","BOS":"PASS" if r.get("bos") else "WAIT",
                     "Retest":"PASS" if r.get("retest") else "WAIT"})
    st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)
    st.markdown('<div class="section-head">OI / PCR Intelligence</div>', unsafe_allow_html=True)
    oh=option_history or {}
    cur=oh.get("current") or {}
    oi1,oi2,oi3,oi4,oi5=st.columns(5)
    oi1.metric("CALL OI",_fmt_chain_num(cur.get("call_oi")) if cur else "—")
    oi2.metric("PUT OI",_fmt_chain_num(cur.get("put_oi")) if cur else "—")
    oi3.metric("CALL OI Chg",_fmt_chain_num(cur.get("call_oi_change")) if cur else "—")
    oi4.metric("PUT OI Chg",_fmt_chain_num(cur.get("put_oi_change")) if cur else "—")
    oi5.metric("PCR",f'{_tf_safe_float(st.session_state.get("trade_easy_v2_pcr", np.nan)):.2f}' if np.isfinite(_tf_safe_float(st.session_state.get("trade_easy_v2_pcr", np.nan))) else "—")
    hist_rows=[]
    for label in ("15m","30m","60m"):
        h=oh.get(label,{})
        hist_rows.append({"Window":label,"History":"READY" if h.get("available") else "WARMING",
                          "CALL OI Δ":_fmt_chain_num(h.get("call_oi_change")) if h.get("available") else "—",
                          "PUT OI Δ":_fmt_chain_num(h.get("put_oi_change")) if h.get("available") else "—",
                          "CALL %":f'{h.get("call_oi_change_pct",np.nan):+.2f}%' if np.isfinite(_tf_safe_float(h.get("call_oi_change_pct"))) else "—",
                          "PUT %":f'{h.get("put_oi_change_pct",np.nan):+.2f}%' if np.isfinite(_tf_safe_float(h.get("put_oi_change_pct"))) else "—"})
    st.dataframe(pd.DataFrame(hist_rows),use_container_width=True,hide_index=True)
    opt=result.get("option") or {}
    st.markdown('<div class="section-head">Filtered Option Contract</div>',unsafe_allow_html=True)
    o1,o2,o3,o4,o5,o6=st.columns(6)
    o1.metric("Type",opt.get("type") or "—")
    o2.metric("Strike",f'{opt.get("strike"):.0f}' if np.isfinite(_tf_safe_float(opt.get("strike"))) else "—")
    o3.metric("LTP",f'{opt.get("ltp"):.2f}' if np.isfinite(_tf_safe_float(opt.get("ltp"))) else "—")
    o4.metric("Delta",f'{opt.get("delta_pct"):.1f}' if np.isfinite(_tf_safe_float(opt.get("delta_pct"))) else "—")
    o5.metric("Theta",f'{opt.get("theta"):.2f}' if np.isfinite(_tf_safe_float(opt.get("theta"))) else "—")
    o6.metric("OI",_fmt_chain_num(opt.get("oi")) if np.isfinite(_tf_safe_float(opt.get("oi"))) else "—")
    if not opt.get("available"):
        st.info("कोई वास्तविक option contract अभी Delta ≥ 70% और Theta -5 से -10 की दोनों शर्तें एक साथ पूरी नहीं कर रहा है।")
    else:
        st.success("Greek filter PASS • Delta ≥ 70% • Theta -5 से -10")
    p1,p2,p3,p4=st.columns(4)
    for col,label,key in ((p1,"Entry","entry"),(p2,"Stop Loss","stop_loss"),(p3,"Target","target"),(p4,"Risk/Reward","risk_reward")):
        val=_tf_safe_float(result.get(key))
        col.metric(label,f'{val:.2f}' if np.isfinite(val) else "—")
    blocks=result.get("blocks") or []
    if blocks:
        st.caption("V2 waiting reasons: " + " • ".join(str(x) for x in blocks[:8]))

def meaningful_move_threshold(df, index_name):
    """Return an index/timeframe-specific meaningful move in points.

    The threshold follows recent ATR and is floored by an index-specific
    minimum so unusually quiet data does not make the filter meaningless.
    """
    try:
        atr_value = float(df["atr"].iloc[-1]) if "atr" in df.columns else np.nan
    except Exception:
        atr_value = np.nan
    floor = float(MEANINGFUL_MOVE_MIN_POINTS.get(index_name, 50.0))
    if not np.isfinite(atr_value) or atr_value <= 0:
        return floor
    return max(floor, atr_value * MEANINGFUL_MOVE_ATR_MULTIPLIER)


def level_path_analysis(price, direction, levels, option_summary=None, meaningful_move=None):
    if price is None or direction not in ("LONG", "SHORT"):
        return {"eligible": False, "raw_distance": np.nan, "clear_path": np.nan,
                "first_obstacle": np.nan, "obstacle_name": "NO_DIRECTION",
                "target_path_clear": False, "meaningful_move": meaningful_move or np.nan}
    price = float(price)
    candidates = []
    for name, value in levels.items():
        if not isinstance(value, (int, float, np.floating)) or not np.isfinite(value):
            continue
        value = float(value)
        if direction == "LONG" and value > price + 0.5:
            candidates.append((value-price, value, name))
        if direction == "SHORT" and value < price - 0.5:
            candidates.append((price-value, value, name))
    if option_summary and option_summary.get("available"):
        wall = option_summary.get("call_wall") if direction == "LONG" else option_summary.get("put_wall")
        try:
            wall_ok = np.isfinite(float(wall))
        except Exception:
            wall_ok = False
        if wall_ok:
            wall = float(wall)
            if direction == "LONG" and wall > price + 0.5:
                candidates.append((wall-price, wall, "CALL_OI_WALL"))
            if direction == "SHORT" and wall < price - 0.5:
                candidates.append((price-wall, wall, "PUT_OI_WALL"))
    candidates.sort(key=lambda x: x[0])
    if candidates:
        clear_path, obstacle, obstacle_name = candidates[0]
    else:
        # No known obstacle: the configured fixed target is the minimum
        # concrete path, but the meaningful-move threshold remains separate.
        clear_path, obstacle, obstacle_name = FIXED_TARGET_POINTS, np.nan, "NONE"
    threshold = float(meaningful_move if meaningful_move is not None else 50.0)
    return {"eligible": bool(clear_path >= threshold), "raw_distance": float(clear_path),
            "clear_path": float(clear_path), "first_obstacle": obstacle,
            "obstacle_name": obstacle_name, "target_path_clear": bool(clear_path >= FIXED_TARGET_POINTS),
            "meaningful_move": threshold}


def level_setup_engine(price, direction, bias, levels, score, confirmation_count, option_summary,
                       df=None, index_name="NIFTY 50"):
    if price is None or direction not in ("LONG", "SHORT"):
        return {"status":"WAITING","trade_type":"NONE","reason":"NO_DIRECTION","path":{}}
    price=float(price)
    if direction == "LONG":
        near_level = any(np.isfinite(float(levels.get(k, np.nan))) and abs(price-float(levels[k])) <= 15 for k in ("swing_low","previous_day_low"))
        with_trend = bias == "BULLISH"
    else:
        near_level = any(np.isfinite(float(levels.get(k, np.nan))) and abs(price-float(levels[k])) <= 15 for k in ("swing_high","previous_day_high"))
        with_trend = bias == "BEARISH"
    trade_type = "WITH-TREND" if with_trend else "COUNTER-TREND"
    move_threshold = meaningful_move_threshold(df, index_name) if df is not None else float(MEANINGFUL_MOVE_MIN_POINTS.get(index_name, 50.0))
    path = level_path_analysis(price, direction, levels, option_summary, move_threshold)
    if not path["eligible"]:
        return {"status":"NO TRADE","trade_type":trade_type,
                "reason":f"CLEAR_PATH_LT_MEANINGFUL_{move_threshold:.1f}","path":path}
    required_score, required_conf = ((75,2) if with_trend else (85,3))
    if score < required_score or confirmation_count < required_conf:
        return {"status":"ENTRY ZONE" if near_level else "SETUP BUILDING","trade_type":trade_type,
                "reason":f"CONFIRMATION_PENDING_{required_conf}","path":path}
    if not near_level:
        return {"status":"ENTRY ZONE","trade_type":trade_type,"reason":"LEVEL_NOT_RETESTED","path":path}
    return {"status":"CONFIRMED","trade_type":trade_type,"reason":"ALL_LEVEL_FILTERS_PASS","path":path}

_WS_CACHE = {}
_WS_CACHE_LOCK = threading.Lock()

def fyers_live_feed(access_token, app_id, symbol):
    """Return one persistent daemon WebSocket state per token/symbol key.

    IMPORTANT: this function must not create a new FYERS socket on every
    Streamlit fragment rerun; doing so causes duplicate sockets, excess CPU,
    and visible dashboard instability.
    """
    cache_key = f"{app_id}:{symbol}:{hashlib.sha256(str(access_token).encode()).hexdigest()[:12]}"
    with _WS_CACHE_LOCK:
        cached = _WS_CACHE.get(cache_key)
        if cached and cached.get("thread") and cached["thread"].is_alive():
            return cached["state"]

        state = {"latest": None, "error": None, "connected": False, "started": time.time(),
                 "last_tick_received": None, "last_tick_price": None, "tick_count": 0}
        lock = threading.Lock()

    if not FYERS_SDK_OK or data_ws is None:
        state["error"] = "FYERS WebSocket SDK उपलब्ध नहीं है।"
        return state

    def on_message(message):
        price = parse_live_price(message)
        with lock:
            state["latest"] = message
            state["connected"] = True
            state["last_tick_received"] = time.time()
            if price is not None:
                state["last_tick_price"] = float(price)
            state["tick_count"] = int(state.get("tick_count", 0)) + 1
            state["error"] = None

    def on_error(message):
        with lock:
            state["error"] = str(message)
            state["connected"] = False

    def on_close(message):
        with lock:
            state["connected"] = False
            state["error"] = str(message) if message else "WebSocket closed; reconnecting"

    def on_connect():
        try:
            socket.subscribe(symbols=[symbol], data_type="SymbolUpdate")
            with lock:
                state["connected"] = True
                state["error"] = None
        except Exception as exc:
            on_error(exc)

    socket = data_ws.FyersDataSocket(
        access_token=f"{app_id}:{access_token}",
        log_path="",
        litemode=False,
        write_to_file=False,
        reconnect=True,
        on_connect=on_connect,
        on_close=on_close,
        on_error=on_error,
        on_message=on_message,
    )

    def runner():
        try:
            socket.connect()
            socket.keep_running()
        except Exception as exc:
            on_error(exc)

    thread = threading.Thread(target=runner, daemon=True, name="trade-easy-fyers-ws")
    with _WS_CACHE_LOCK:
        _WS_CACHE[cache_key] = {"state": state, "thread": thread, "socket": socket}
    thread.start()
    return state


def parse_live_price(message):
    """Extract FYERS LTP from SymbolUpdate/lite-mode payloads."""
    if isinstance(message, list):
        for item in message:
            px = parse_live_price(item)
            if px is not None:
                return px
        return None
    if not isinstance(message, dict):
        return None
    for key in ("ltp", "lp", "LTP"):
        if key in message:
            try:
                return float(message[key])
            except Exception:
                pass
    d = message.get("d")
    if isinstance(d, dict):
        for key in ("ltp", "lp", "LTP"):
            if key in d:
                try:
                    return float(d[key])
                except Exception:
                    pass
    return None


_LTP_WS_CACHE = {}
_LTP_WS_LOCK = threading.Lock()


def fyers_ltp_feed(access_token, app_id, symbol):
    """Persistent FYERS LTP-only websocket for the trader-facing live ticker.

    Lite mode is intentionally used here because the ticker needs only LTP and
    tick timestamps. The heavier SymbolUpdate feed remains available separately
    for option contracts/analytics. This avoids starving the LTP ticker when the
    option chain has many subscribed contracts.
    """
    if not FYERS_SDK_OK or data_ws is None or not access_token or not app_id or not symbol:
        return {
            "latest": None,
            "last_tick_price": None,
            "last_tick_received": None,
            "tick_count": 0,
            "connected": False,
            "error": "FYERS WebSocket unavailable",
        }

    key = f"{app_id}:{symbol}:{hashlib.sha256(str(access_token).encode()).hexdigest()[:16]}"
    with _LTP_WS_LOCK:
        cached = _LTP_WS_CACHE.get(key)
        if cached and cached.get("thread") and cached["thread"].is_alive():
            return cached["state"]
        state = {
            "latest": None,
            "last_tick_price": None,
            "last_tick_received": None,
            "tick_count": 0,
            "connected": False,
            "error": None,
        }
        lock = threading.Lock()

    def on_message(message):
        if not isinstance(message, dict):
            return
        price = parse_live_price(message)
        with lock:
            state["latest"] = message
            state["connected"] = True
            state["last_tick_received"] = time.time()
            if price is not None:
                state["last_tick_price"] = float(price)
            state["tick_count"] = int(state.get("tick_count", 0)) + 1
            state["error"] = None

    def on_error(message):
        with lock:
            state["connected"] = False
            state["error"] = str(message)

    def on_close(message):
        with lock:
            state["connected"] = False
            state["error"] = str(message) if message else "WebSocket closed; reconnecting"

    def on_connect():
        try:
            socket.subscribe(symbols=[symbol], data_type="SymbolUpdate")
            with lock:
                state["connected"] = True
                state["error"] = None
        except Exception as exc:
            on_error(exc)

    socket = data_ws.FyersDataSocket(
        access_token=f"{app_id}:{access_token}",
        log_path="",
        litemode=True,
        write_to_file=False,
        reconnect=True,
        on_connect=on_connect,
        on_close=on_close,
        on_error=on_error,
        on_message=on_message,
    )

    def runner():
        try:
            socket.connect()
            socket.keep_running()
        except Exception as exc:
            on_error(exc)

    thread = threading.Thread(target=runner, daemon=True, name="trade-easy-ltp-ws")
    with _LTP_WS_LOCK:
        _LTP_WS_CACHE[key] = {"state": state, "thread": thread, "socket": socket}
    thread.start()
    return state


# ============================================================
# AUTHENTICATION
# ============================================================

def get_current_user():
    try:
        response = supabase.auth.get_user()
        return getattr(response, "user", None)
    except Exception:
        return None


def clear_oauth_params():
    try:
        st.query_params.clear()
    except Exception:
        pass


def get_or_create_workspace(user):
    if not user or not getattr(user, "id", None):
        return None, "Authenticated user ID नहीं मिला।"

    user_id = user.id
    email = getattr(user, "email", "") or ""
    metadata = getattr(user, "user_metadata", {}) or {}
    display_name = (
        metadata.get("display_name")
        or metadata.get("full_name")
        or metadata.get("name")
        or email.split("@")[0]
        or "User"
    )

    try:
        result = (
            supabase.table("workspaces")
            .select("id,user_id,workspace_name,created_at")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )

        if result.data:
            return result.data[0], None

        workspace_name = f"{display_name} - Trading Workspace"
        created = (
            supabase.table("workspaces")
            .insert({"user_id": user_id, "workspace_name": workspace_name})
            .execute()
        )

        if created.data:
            return created.data[0], None

        result = (
            supabase.table("workspaces")
            .select("id,user_id,workspace_name,created_at")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0], None

        return None, "Workspace record नहीं मिला।"
    except Exception as e:
        return None, str(e)


# ============================================================
# ADMIN + SUBSCRIPTION SYSTEM
# Uses Supabase tables protected by RLS. No payment gateway is assumed here;
# payments can be recorded manually or connected later through webhooks.
# ============================================================

SUBSCRIPTION_ENFORCEMENT = str(
    os.environ.get("TRADE_EASY_SUBSCRIPTION_ENFORCEMENT", "true")
).strip().lower() not in {"0", "false", "no", "off"}
DEFAULT_TRIAL_DAYS = max(1, int(os.environ.get("TRADE_EASY_TRIAL_DAYS", "7")))


def _utc_now():
    return datetime.now(timezone.utc)


def _parse_dt(value):
    if not value:
        return None
    try:
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.to_pydatetime()
    except Exception:
        return None


def _profile_for_user(user_id):
    try:
        result = (
            supabase.table("profiles")
            .select("id,email,full_name,role,status,created_at,updated_at")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception:
        return None


def ensure_user_profile(user):
    """Ensure a profile exists for pre-existing auth users; signup trigger handles new users."""
    if not user or not getattr(user, "id", None):
        return None, "Authenticated user ID नहीं मिला।"
    user_id = str(user.id)
    email = getattr(user, "email", "") or ""
    metadata = getattr(user, "user_metadata", {}) or {}
    name = (
        metadata.get("display_name")
        or metadata.get("full_name")
        or metadata.get("name")
        or email.split("@")[0]
        or "User"
    )
    existing = _profile_for_user(user_id)
    if existing:
        return existing, None
    try:
        created = (
            supabase.table("profiles")
            .insert({"id": user_id, "email": email, "full_name": name, "role": "user", "status": "active"})
            .execute()
        )
        if created.data:
            return created.data[0], None
        existing = _profile_for_user(user_id)
        if existing:
            return existing, None
        return None, "Profile create नहीं हुआ।"
    except Exception as exc:
        return None, str(exc)


def _load_plan(plan_id):
    """Load plan by UUID from public.plans."""
    if not plan_id:
        return None
    try:
        result = (
            supabase.table("plans")
            .select("id,name,price,duration_days,features,is_active,created_at")
            .eq("id", plan_id)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception:
        return None


def _load_plan_from_subscription_value(value):
    """Resolve legacy subscriptions.plan text to the current plans row.

    Existing project schema uses subscriptions.plan (TEXT), while V5 originally
    expected subscriptions.plan_id (UUID). We keep the existing database schema
    and resolve common legacy codes/names without adding destructive migrations.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    aliases = {
        "FREE": "Free Trial",
        "TRIAL": "Free Trial",
        "BASIC": "Basic Monthly",
        "BASIC MONTHLY": "Basic Monthly",
        "PRO": "Pro Monthly",
        "PRO MONTHLY": "Pro Monthly",
        "PRO QUARTERLY": "Pro Quarterly",
        "PREMIUM": "Premium Yearly",
        "PREMIUM YEARLY": "Premium Yearly",
    }
    target = aliases.get(raw.upper(), raw)

    try:
        result = (
            supabase.table("plans")
            .select("id,name,price,duration_days,features,is_active,created_at")
            .ilike("name", target)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception:
        return None


def _load_trial_plan():
    try:
        result = (
            supabase.table("plans")
            .select("id,name,price,duration_days,features,is_active,created_at")
            .eq("name", "Free Trial")
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception:
        return None


def _normalize_subscription_row(raw):
    """Normalize the existing subscriptions schema to the app's internal shape."""
    sub = dict(raw or {})
    # Existing DB: plan TEXT, starts_at, ends_at.
    plan_value = sub.get("plan")
    plan = _load_plan_from_subscription_value(plan_value)
    sub["plan_id"] = plan.get("id") if plan else None
    sub["start_at"] = sub.get("starts_at")
    sub["end_at"] = sub.get("ends_at")
    return sub, plan


def get_user_subscription(user_id):
    """Return the newest subscription using the project's existing schema."""
    try:
        result = (
            supabase.table("subscriptions")
            .select("id,user_id,plan,provider,provider_customer_id,provider_subscription_id,provider_plan_id,status,starts_at,ends_at,payment_id,notes,created_at,updated_at")
            .eq("user_id", user_id)
            .order("ends_at", desc=True)
            .limit(1)
            .execute()
        )
        if not result.data:
            return None
        sub, plan = _normalize_subscription_row(result.data[0])
        now = _utc_now()
        end_dt = _parse_dt(sub.get("end_at"))
        status = str(sub.get("status") or "UNKNOWN").upper()
        effective = status
        if status in {"ACTIVE", "TRIAL"} and end_dt and end_dt <= now:
            effective = "EXPIRED"
        if status == "CANCELLED":
            effective = "CANCELLED"
        if status == "SUSPENDED":
            effective = "SUSPENDED"
        return {"subscription": sub, "plan": plan, "status": effective}
    except Exception as exc:
        return {"error": str(exc), "subscription": None, "plan": None, "status": "SETUP_ERROR"}


def ensure_trial_subscription(user_id):
    """Create a 7-day Free Trial through the SECURITY DEFINER RPC."""
    try:
        result = supabase.rpc("ensure_trade_easy_trial", {}).execute()
        if result.data:
            return get_user_subscription(user_id)
    except Exception:
        pass
    return get_user_subscription(user_id)


def subscription_access_state(user_id):
    """Return whether a normal user may access the trading dashboard."""
    if not SUBSCRIPTION_ENFORCEMENT:
        return True, {"status": "LEGACY_ACCESS", "subscription": None, "plan": None}
    data = ensure_trial_subscription(user_id)
    status = str((data or {}).get("status") or "SETUP_ERROR").upper()
    return status in {"ACTIVE", "TRIAL"}, data


def is_admin_profile(profile):
    return bool(
        profile
        and str(profile.get("role", "user")).lower() == "admin"
        and str(profile.get("status", "active")).lower() == "active"
    )


def _admin_rows(table, columns="*"):
    try:
        return supabase.table(table).select(columns).execute().data or []
    except Exception:
        return []


def admin_load_all():
    profiles = _admin_rows("profiles", "id,email,full_name,role,status,created_at,updated_at")
    plans = _admin_rows("plans", "id,name,price,duration_days,features,is_active,created_at")
    raw_subscriptions = _admin_rows(
        "subscriptions",
        "id,user_id,plan,provider,provider_customer_id,provider_subscription_id,provider_plan_id,status,starts_at,ends_at,payment_id,notes,created_at,updated_at",
    )
    subscriptions = []
    for raw in raw_subscriptions:
        sub, plan = _normalize_subscription_row(raw)
        subscriptions.append(sub)
    payments = _admin_rows("payments", "id,user_id,subscription_id,amount,currency,payment_gateway,payment_id,status,paid_at,created_at")
    return profiles, plans, subscriptions, payments


@st.cache_resource
def get_supabase_admin() -> Client:
    """Create a stateless, server-only Supabase Admin client.

    Auth Admin methods must run on a trusted server with the project's
    secret key. Disabling session persistence/auto-refresh prevents the
    normal user-auth client from replacing the Admin Authorization header.
    """
    if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
        raise RuntimeError("Supabase Admin configuration is missing.")
    # Keep this Admin client completely separate from the normal user client.
    # We intentionally do not pass ClientOptions here because deployed
    # supabase-py installations can have incompatible ClientOptions models
    # (which can raise: 'ClientOptions' object has no attribute 'storage').
    # A dedicated client instance already prevents the normal user's session
    # from being mixed into the Admin client.
    return create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)


def admin_reset_user_password(user_id, new_password):
    """Reset any user's Supabase Auth password from the trusted server."""
    if not SUPABASE_SECRET_KEY:
        return False, "SUPABASE_SECRET_KEY Streamlit Secrets में configured नहीं है।"
    if not user_id:
        return False, "User ID missing है।"
    if not new_password or len(new_password) < 8:
        return False, "Password कम-से-कम 8 characters का होना चाहिए।"

    try:
        admin_client = get_supabase_admin()
        response = admin_client.auth.admin.update_user_by_id(
            str(user_id),
            {"password": str(new_password)},
        )
        updated_user = getattr(response, "user", None)
        if updated_user is not None:
            return True, None

        # Some supabase-py versions expose the response as a dict-like object.
        if isinstance(response, dict) and response.get("user"):
            return True, None

        return False, "Supabase Auth ने password update का user response नहीं लौटाया।"
    except Exception as exc:
        detail = str(exc)
        return False, f"Supabase Auth Admin password update failed: {detail}"


def admin_bootstrap_admin_password(email, bootstrap_token, new_password):
    """Pre-login first-admin password bootstrap using Supabase Auth Admin SDK.

    The configured public.profiles row is the authoritative application-side
    link to auth.users because profiles.id == auth.users.id in this project.
    We therefore resolve the admin user ID from profiles first instead of
    relying on list_users() pagination/email matching.
    """
    email = str(email or "").strip().lower()
    bootstrap_token = str(bootstrap_token or "")
    new_password = str(new_password or "")

    if not SUPABASE_SECRET_KEY:
        return False, "SUPABASE_SECRET_KEY Streamlit Secrets में configured नहीं है।"
    if not TRADE_EASY_ADMIN_BOOTSTRAP_TOKEN:
        return False, "TRADE_EASY_ADMIN_BOOTSTRAP_TOKEN Streamlit Secrets में configured नहीं है।"
    if not email or not hmac.compare_digest(
        email, TRADE_EASY_ADMIN_EMAIL.strip().lower()
    ):
        return False, "यह email configured first-admin email नहीं है।"
    if not hmac.compare_digest(
        bootstrap_token, TRADE_EASY_ADMIN_BOOTSTRAP_TOKEN
    ):
        return False, "Admin bootstrap token गलत है।"
    if len(new_password) < 8:
        return False, "Password कम-से-कम 8 characters का होना चाहिए।"

    try:
        admin_client = get_supabase_admin()

        # The project's profile row is already known to exist for this admin,
        # and profiles.id is the same UUID as auth.users.id. Resolve that UUID
        # directly through the trusted server-side client.
        profile_result = (
            admin_client.table("profiles")
            .select("id,email,role,status")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        profiles = profile_result.data or []

        if not profiles:
            return False, (
                f"Supabase public.profiles में {email} नहीं मिला। "
                "यह Streamlit app जिस Supabase project से जुड़ा है, उसमें "
                "admin profile मौजूद है या नहीं जाँचें।"
            )

        profile = profiles[0]
        user_id = str(profile.get("id") or "").strip()
        if not user_id:
            return False, "Admin profile में user ID नहीं मिली।"

        if str(profile.get("role", "")).lower() != "admin" or str(
            profile.get("status", "")
        ).lower() != "active":
            return False, (
                "Admin profile का role='admin' और status='active' होना चाहिए।"
            )

        # Confirm that this UUID actually exists in Supabase Auth, then update
        # the password using the official Auth Admin API.
        try:
            auth_lookup = admin_client.auth.admin.get_user_by_id(user_id)
            auth_user = getattr(auth_lookup, "user", None)
            if auth_user is None and isinstance(auth_lookup, dict):
                auth_user = auth_lookup.get("user")
        except Exception as lookup_exc:
            return False, (
                "Supabase profile मिल गया, लेकिन Auth user lookup failed: "
                f"{lookup_exc}"
            )

        if auth_user is None:
            return False, (
                f"Supabase Auth में user ID {user_id} नहीं मिला। "
                "यह profile और auth.users के बीच mismatch है।"
            )

        response = admin_client.auth.admin.update_user_by_id(
            user_id,
            {
                "password": new_password,
                "email_confirm": True,
            },
        )
        updated_user = getattr(response, "user", None)
        if updated_user is None and not (
            isinstance(response, dict) and response.get("user")
        ):
            return False, "Supabase Auth ने password update confirm नहीं किया।"

        return True, None

    except Exception as exc:
        return False, f"Admin bootstrap error: {exc}"


def admin_update_profile(user_id, *, role=None, status=None):
    payload = {}
    if role is not None:
        payload["role"] = str(role).lower()
    if status is not None:
        payload["status"] = str(status).lower()
    if not payload:
        return False, "कोई बदलाव नहीं।"
    payload["updated_at"] = _utc_now().isoformat()
    try:
        result = supabase.table("profiles").update(payload).eq("id", user_id).execute()
        return bool(result.data), None if result.data else "Profile update नहीं हुआ।"
    except Exception as exc:
        return False, str(exc)


def admin_create_plan(name, price, duration_days, features, is_active=True):
    payload = {
        "name": str(name).strip(),
        "price": float(price),
        "duration_days": int(duration_days),
        "features": features if isinstance(features, dict) else {},
        "is_active": bool(is_active),
    }
    try:
        result = supabase.table("plans").insert(payload).execute()
        return bool(result.data), None if result.data else "Plan create नहीं हुआ।"
    except Exception as exc:
        return False, str(exc)


def admin_update_plan(plan_id, *, price=None, duration_days=None, is_active=None, features=None):
    payload = {"updated_at": _utc_now().isoformat()}
    if price is not None:
        payload["price"] = float(price)
    if duration_days is not None:
        payload["duration_days"] = int(duration_days)
    if is_active is not None:
        payload["is_active"] = bool(is_active)
    if features is not None:
        payload["features"] = features
    try:
        result = supabase.table("plans").update(payload).eq("id", plan_id).execute()
        return bool(result.data), None if result.data else "Plan update नहीं हुआ।"
    except Exception as exc:
        return False, str(exc)


def _subscription_plan_code(plan_name):
    """Map UI plan names to the legacy subscriptions.plan CHECK values."""
    raw = str(plan_name or "").strip().upper()
    if raw in {"FREE", "FREE TRIAL", "TRIAL"}:
        return "FREE"
    if raw in {"BASIC", "BASIC MONTHLY"}:
        return "BASIC"
    if raw in {"PRO", "PRO MONTHLY", "PRO QUARTERLY"}:
        return "PRO"
    if raw in {"PREMIUM", "PREMIUM YEARLY"}:
        return "PREMIUM"
    return raw


def admin_grant_subscription(email, plan_id, days_override=None, notes="", payment_id=None):
    """Grant a subscription using the project's legacy subscriptions schema."""
    try:
        prof = (
            supabase.table("profiles")
            .select("id,email,full_name,status")
            .eq("email", str(email).strip().lower())
            .limit(1)
            .execute()
        )
        if not prof.data:
            return False, "User नहीं मिला।"
        user_id = prof.data[0]["id"]
        plan = _load_plan(plan_id)
        if not plan:
            return False, "Plan नहीं मिला।"
        now = _utc_now()
        existing = get_user_subscription(user_id)
        current_end = _parse_dt((existing or {}).get("subscription", {}).get("end_at")) if existing else None
        start = current_end if current_end and current_end > now else now
        duration = int(days_override) if days_override else int(plan.get("duration_days") or 30)
        end = start + timedelta(days=duration)
        payload = {
            "user_id": user_id,
            "plan": _subscription_plan_code(plan.get("name")),
            "provider": "MANUAL",
            "status": "ACTIVE",
            "starts_at": start.isoformat(),
            "ends_at": end.isoformat(),
            "payment_id": payment_id or None,
            "notes": str(notes or "").strip() or None,
            "updated_at": now.isoformat(),
        }
        result = supabase.table("subscriptions").insert(payload).execute()
        if not result.data:
            return False, "Subscription create नहीं हुआ।"
        created_sub = result.data[0]
        if payment_id:
            try:
                supabase.table("payments").insert({
                    "user_id": user_id,
                    "subscription_id": created_sub.get("id"),
                    "amount": float(plan.get("price") or 0),
                    "currency": "INR",
                    "payment_gateway": "MANUAL",
                    "payment_id": payment_id,
                    "status": "SUCCESS",
                    "paid_at": now.isoformat(),
                }).execute()
            except Exception:
                pass
        return True, f"Subscription ACTIVE • {plan['name']} • {duration} days"
    except Exception as exc:
        return False, str(exc)


def admin_extend_subscription(subscription_id, days):
    try:
        rows = (
            supabase.table("subscriptions")
            .select("id,ends_at,status")
            .eq("id", subscription_id)
            .limit(1)
            .execute()
            .data or []
        )
        if not rows:
            return False, "Subscription नहीं मिला।"
        old_end = _parse_dt(rows[0].get("ends_at")) or _utc_now()
        base = max(old_end, _utc_now())
        new_end = base + timedelta(days=int(days))
        result = (
            supabase.table("subscriptions")
            .update({"ends_at": new_end.isoformat(), "status": "ACTIVE", "updated_at": _utc_now().isoformat()})
            .eq("id", subscription_id)
            .execute()
        )
        return bool(result.data), None if result.data else "Extend नहीं हुआ।"
    except Exception as exc:
        return False, str(exc)


def admin_set_subscription_status(subscription_id, status):
    try:
        status = str(status).upper()
        result = (
            supabase.table("subscriptions")
            .update({"status": status, "updated_at": _utc_now().isoformat()})
            .eq("id", subscription_id)
            .execute()
        )
        return bool(result.data), None if result.data else "Status update नहीं हुआ।"
    except Exception as exc:
        return False, str(exc)


def _subscription_card(data):
    if not data:
        return
    status = str(data.get("status") or "UNKNOWN").upper()
    sub = data.get("subscription") or {}
    plan = data.get("plan") or {}
    end_dt = _parse_dt(sub.get("end_at"))
    now = _utc_now()
    days_left = max(0, math.ceil((end_dt - now).total_seconds() / 86400)) if end_dt else 0
    status_label = "🟢 ACTIVE" if status == "ACTIVE" else ("🟡 TRIAL" if status == "TRIAL" else ("🔴 EXPIRED" if status == "EXPIRED" else f"⚪ {status}"))
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Subscription", status_label)
    p2.metric("Plan", plan.get("name", "—"))
    p3.metric("Days Remaining", days_left if end_dt else "—")
    p4.metric("Valid Until", end_dt.astimezone().strftime("%d-%m-%Y %H:%M") if end_dt else "—")


def subscription_block_page(data, user):
    st.markdown("## Subscription Required")
    st.warning("आपका Trade Easy subscription अभी active नहीं है। Trading dashboard access बंद है।")
    if data and data.get("status") == "EXPIRED":
        st.error("Subscription expired हो चुका है।")
    elif data and data.get("status") == "CANCELLED":
        st.error("Subscription cancelled है।")
    elif data and data.get("status") == "SUSPENDED":
        st.error("Account/subscription suspended है।")
    else:
        st.info("Admin से plan activate/extend करवाएँ।")
    _subscription_card(data)
    st.caption(f"User: {getattr(user, 'email', '')}")
    if st.button("Logout", key="subscription_logout", use_container_width=True):
        try:
            supabase.auth.sign_out()
        except Exception:
            pass
        clear_oauth_params()
        st.rerun()


def render_admin_fyers_settings():
    """Admin-only FYERS connection screen for Community Cloud / production."""
    st.markdown("# Trade Easy — FYERS Connection")
    st.caption("Admin-only broker/data connection. Normal users never see FYERS credentials or controls.")

    app_id = FYERS_CONFIG_APP_ID.strip()
    secret = FYERS_CONFIG_SECRET.strip()
    status = str(st.session_state.get("fyers_login_status") or "disconnected").lower()

    if not app_id or not secret:
        st.error("FYERS production credentials configured नहीं हैं।")
        st.info("Streamlit Cloud → App Settings → Secrets में FYERS_APP_ID और FYERS_SECRET_KEY सेट करें।")
        return

    if not FYERS_REDIRECT_URI or not FYERS_REDIRECT_URI.startswith("https://"):
        st.error("FYERS_REDIRECT_URI अभी configured नहीं है।")
        st.info("Deploy होने के बाद exact https://...streamlit.app/ URL को FYERS app और Streamlit Secrets दोनों में सेट करें।")
        return

    if status == "connected" and st.session_state.get("fyers_access_token"):
        st.success("🟢 FYERS Connected")
    elif status == "error":
        st.error("FYERS authentication failed")
        st.code(st.session_state.get("fyers_login_error", "Unknown error"))
    else:
        st.info("FYERS connected नहीं है। नीचे Connect दबाकर Admin FYERS login करें।")

    st.markdown("### Connection")
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("🔑 Connect / Reconnect FYERS", use_container_width=True, type="primary", key="admin_connect_fyers"):
            try:
                # Credentials come only from Streamlit Secrets/environment.
                st.session_state["fyers_app_id"] = app_id
                st.session_state["fyers_secret"] = secret
                login_url = fyers_make_auth_url(app_id, secret)
                st.session_state["fyers_login_status"] = "waiting"
                st.session_state["fyers_login_error"] = ""
                st.session_state["fyers_login_url"] = login_url
            except Exception as exc:
                st.session_state["fyers_login_status"] = "error"
                st.session_state["fyers_login_error"] = str(exc)
                st.error(f"FYERS login URL error: {exc}")

    with c2:
        if st.button("⏏️ Disconnect FYERS", use_container_width=True, key="admin_disconnect_fyers"):
            _clear_fyers_session()
            st.session_state["fyers_login_error"] = ""
            st.success("FYERS session disconnected for this app session.")
            st.rerun()

    with c3:
        connected = bool(st.session_state.get("fyers_access_token"))
        st.metric("Status", "CONNECTED" if connected else "DISCONNECTED")

    login_url = st.session_state.get("fyers_login_url")
    if login_url and status == "waiting" and not st.session_state.get("fyers_access_token"):
        st.markdown("### FYERS Login")
        st.link_button("Open FYERS Login", login_url, use_container_width=True)
        st.caption("Login complete होने के बाद FYERS आपको इसी deployed app पर वापस भेजेगा।")

    st.markdown("### Production settings")
    st.code(
        f"Redirect URI: {FYERS_REDIRECT_URI}\n"
        f"App ID: {'configured' if app_id else 'missing'}\n"
        f"Secret: {'configured' if secret else 'missing'}",
        language="text",
    )
    st.caption("FYERS credentials are never displayed or stored in GitHub by this app.")


def admin_dashboard(user, profile, workspace):
    """Admin console with separate FYERS settings and the same trading dashboard available to the admin."""
    st.markdown("# Trade Easy — Admin Console")
    st.caption("Users • Plans • Subscriptions • Manual payments • Access control • FYERS • Trading")

    admin_section = st.radio(
        "Admin Workspace",
        ["Admin Console", "FYERS Connection", "Trading Dashboard"],
        horizontal=True,
        key="admin_workspace_section",
    )

    if admin_section == "FYERS Connection":
        render_admin_fyers_settings()
        return

    if admin_section == "Trading Dashboard":
        if not st.session_state.get("fyers_access_token"):
            st.warning("पहले **FYERS Connection** में जाकर Admin FYERS account connect करें।")
            st.info("Connection होने के बाद इसी Admin Workspace में **Trading Dashboard** खोलें।")
            return
        st.session_state["fyers_admin_trading_mode"] = True
        dashboard(user, workspace)
        return

    st.session_state["fyers_admin_trading_mode"] = False
    top1, top2 = st.columns([5, 1])
    with top2:
        if st.button("Logout", key="admin_logout", use_container_width=True):
            try:
                supabase.auth.sign_out()
            except Exception:
                pass
            clear_oauth_params()
            st.rerun()

    profiles, plans, subscriptions, payments = admin_load_all()
    now = _utc_now()
    active_subs = 0
    expiring = 0
    expired = 0
    for sub in subscriptions:
        status = str(sub.get("status") or "").upper()
        end_dt = _parse_dt(sub.get("end_at"))
        effective = "EXPIRED" if status in {"ACTIVE", "TRIAL"} and end_dt and end_dt <= now else status
        if effective in {"ACTIVE", "TRIAL"}:
            active_subs += 1
            if end_dt and 0 <= (end_dt - now).total_seconds() <= 7 * 86400:
                expiring += 1
        elif effective == "EXPIRED":
            expired += 1

    a1, a2, a3, a4, a5 = st.columns(5)
    a1.metric("Users", len(profiles))
    a2.metric("Active / Trial", active_subs)
    a3.metric("Expiring ≤ 7d", expiring)
    a4.metric("Expired", expired)
    a5.metric("Plans", len(plans))

    tabs = st.tabs(["Users", "Plans", "Subscriptions", "Payments", "Setup"])

    with tabs[0]:
        st.markdown("### User Management")
        if profiles:
            user_df = pd.DataFrame(profiles)
            st.dataframe(user_df[[c for c in ["email", "full_name", "role", "status", "created_at"] if c in user_df.columns]], use_container_width=True, hide_index=True)
            options = sorted([f"{p.get('email','')} | {p.get('id','')}" for p in profiles if p.get("email")])
            selected = st.selectbox("Select user", options, key="admin_selected_user") if options else None
            if selected:
                uid = selected.split(" | ")[-1]
                current = next((p for p in profiles if p.get("id") == uid), None)
                if current:
                    c1, c2 = st.columns(2)
                    with c1:
                        new_role = st.selectbox("Role", ["user", "admin"], index=0 if current.get("role", "user") == "user" else 1, key=f"role_{uid}")
                    with c2:
                        new_status = st.selectbox("Account Status", ["active", "blocked"], index=0 if current.get("status", "active") == "active" else 1, key=f"status_{uid}")
                    if st.button("Save User Access", key=f"save_user_{uid}", use_container_width=True):
                        ok, err = admin_update_profile(uid, role=new_role, status=new_status)
                        st.success("User access updated.") if ok else st.error(err or "Update failed")
                        if ok:
                            st.rerun()

                    st.markdown("#### 🔐 Admin Password Management")
                    st.caption("Admin यहाँ से किसी selected user का Supabase login password सीधे बदल सकता है। Password केवल server-side Auth Admin API को भेजा जाता है।")
                    with st.form(f"admin_password_form_{uid}", clear_on_submit=True):
                        pw1 = st.text_input("New Password", type="password", key=f"admin_pw1_{uid}")
                        pw2 = st.text_input("Confirm New Password", type="password", key=f"admin_pw2_{uid}")
                        change_pw = st.form_submit_button("Change Password", use_container_width=True, type="primary")
                    if change_pw:
                        if not SUPABASE_SECRET_KEY:
                            st.error("SUPABASE_SECRET_KEY configured नहीं है। पहले Streamlit Secrets में इसे जोड़ें।")
                        elif len(pw1) < 8:
                            st.error("Password कम-से-कम 8 characters का होना चाहिए।")
                        elif pw1 != pw2:
                            st.error("दोनों passwords match नहीं कर रहे हैं।")
                        else:
                            ok, err = admin_reset_user_password(uid, pw1)
                            if ok:
                                st.success(f"✅ Password changed successfully for {current.get('email', 'selected user')}.")
                            else:
                                st.error(err or "Password update failed.")
        else:
            st.info("अभी कोई profile नहीं मिली। पहले subscription setup SQL चलाएँ।")

    with tabs[1]:
        st.markdown("### Plans")
        if plans:
            plan_rows = []
            for p in plans:
                plan_rows.append({"Name": p.get("name"), "Price": p.get("price"), "Days": p.get("duration_days"), "Active": p.get("is_active")})
            st.dataframe(pd.DataFrame(plan_rows), use_container_width=True, hide_index=True)
            pnames = [f"{p.get('name')} | {p.get('id')}" for p in plans]
            pick = st.selectbox("Manage plan", pnames, key="admin_plan_pick")
            pid = pick.split(" | ")[-1] if pick else None
            selected_plan = next((p for p in plans if p.get("id") == pid), None)
            if selected_plan:
                c1, c2, c3 = st.columns(3)
                price = c1.number_input("Price", min_value=0.0, value=float(selected_plan.get("price") or 0), step=100.0, key=f"plan_price_{pid}")
                days = c2.number_input("Duration days", min_value=1, value=int(selected_plan.get("duration_days") or 30), step=1, key=f"plan_days_{pid}")
                active = c3.checkbox("Active", value=bool(selected_plan.get("is_active")), key=f"plan_active_{pid}")
                if st.button("Update Plan", key=f"update_plan_{pid}", use_container_width=True):
                    ok, err = admin_update_plan(pid, price=price, duration_days=days, is_active=active)
                    st.success("Plan updated.") if ok else st.error(err or "Plan update failed")
                    if ok:
                        st.rerun()
        with st.expander("Create New Plan", expanded=False):
            with st.form("create_plan_form"):
                name = st.text_input("Plan name", value="Pro Monthly")
                price = st.number_input("Price", min_value=0.0, value=999.0, step=100.0)
                days = st.number_input("Duration days", min_value=1, value=30, step=1)
                feature_text = st.text_area("Features JSON", value=json.dumps({"live_price": True, "option_chain": True, "strategy": True, "paper_trading": True}))
                active = st.checkbox("Active", value=True)
                submitted = st.form_submit_button("Create Plan", use_container_width=True)
            if submitted:
                try:
                    features = json.loads(feature_text or "{}")
                    ok, err = admin_create_plan(name, price, days, features, active)
                    st.success("Plan created.") if ok else st.error(err or "Plan create failed")
                    if ok:
                        st.rerun()
                except Exception as exc:
                    st.error(f"Invalid Features JSON: {exc}")

    with tabs[2]:
        st.markdown("### Subscription Management")
        if subscriptions:
            profile_map = {p.get("id"): p.get("email", "") for p in profiles}
            plan_map = {p.get("id"): p.get("name", "") for p in plans}
            rows = []
            for sub in subscriptions:
                end_dt = _parse_dt(sub.get("end_at"))
                status = str(sub.get("status") or "").upper()
                if status in {"ACTIVE", "TRIAL"} and end_dt and end_dt <= now:
                    status = "EXPIRED"
                rows.append({"Email": profile_map.get(sub.get("user_id"), sub.get("user_id")), "Plan": plan_map.get(sub.get("plan_id"), sub.get("plan_id")), "Status": status, "Start": sub.get("start_at"), "End": sub.get("end_at"), "Subscription ID": sub.get("id")})
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("कोई subscription नहीं है।")

        with st.expander("Grant / Renew Subscription", expanded=True):
            emails = sorted([p.get("email") for p in profiles if p.get("email") and str(p.get("status", "active")) == "active"])
            plan_options = [f"{p.get('name')} | {p.get('id')}" for p in plans if p.get("is_active")]
            if emails and plan_options:
                with st.form("grant_subscription_form"):
                    email = st.selectbox("User", emails)
                    psel = st.selectbox("Plan", plan_options)
                    days_override = st.number_input("Duration override (days, 0 = plan default)", min_value=0, value=0, step=1)
                    payment_id = st.text_input("Payment ID / Reference (optional)")
                    notes = st.text_input("Admin note")
                    submit = st.form_submit_button("Activate / Extend Subscription", use_container_width=True)
                if submit:
                    pid = psel.split(" | ")[-1]
                    ok, err = admin_grant_subscription(email, pid, days_override or None, notes, payment_id or None)
                    st.success(err or "Subscription updated.") if ok else st.error(err or "Subscription failed")
                    if ok:
                        st.rerun()
            else:
                st.warning("कम से कम एक active user और एक active plan चाहिए।")

        if subscriptions:
            sid_options = [f"{profile_map.get(s.get('user_id'), s.get('user_id'))} | {plan_map.get(s.get('plan_id'), s.get('plan_id'))} | {s.get('id')}" for s in subscriptions]
            selected_sub = st.selectbox("Select existing subscription", sid_options, key="admin_subscription_pick")
            selected_sid = selected_sub.split(" | ")[-1] if selected_sub else None
            e1, e2, e3 = st.columns(3)
            extend_days = e1.number_input("Extend by days", min_value=1, value=30, step=1, key="extend_days")
            if e2.button("Extend", use_container_width=True):
                ok, err = admin_extend_subscription(selected_sid, extend_days)
                st.success("Subscription extended.") if ok else st.error(err or "Extend failed")
                if ok:
                    st.rerun()
            cancel_status = e3.selectbox("Set status", ["ACTIVE", "SUSPENDED", "CANCELLED"], key="subscription_status")
            if st.button("Apply Status", use_container_width=True):
                ok, err = admin_set_subscription_status(selected_sid, cancel_status)
                st.success("Subscription status updated.") if ok else st.error(err or "Status update failed")
                if ok:
                    st.rerun()

    with tabs[3]:
        st.markdown("### Payments")
        if payments:
            st.dataframe(pd.DataFrame(payments), use_container_width=True, hide_index=True)
        else:
            st.info("Payment table में अभी कोई record नहीं है। यह module manual/webhook integrations के लिए तैयार है।")

    with tabs[4]:
        st.markdown("### Supabase Setup")
        st.info("आपके मौजूदा Supabase subscriptions schema के लिए compatibility setup SQL नीचे दिया गया है।")
        st.code(SUBSCRIPTION_SCHEMA_SQL, language="sql")
        st.caption("पहले admin user को SQL में अपनी email के लिए role='admin' देकर bootstrap करें।")


SUBSCRIPTION_SCHEMA_SQL = r"""
-- Trade Easy V6: compatibility setup for the EXISTING subscriptions schema.
-- Existing columns used by this project:
--   user_id, plan (text), provider, provider_customer_id,
--   provider_subscription_id, provider_plan_id, status,
--   starts_at, ends_at, payment_id, notes, created_at, updated_at

-- Required read/write grants for authenticated users/admin console.
grant usage on schema public to authenticated;
grant select on public.profiles, public.plans, public.subscriptions, public.payments to authenticated;
grant update on public.profiles to authenticated;
grant insert, update, delete on public.plans to authenticated;
grant insert, update, delete on public.subscriptions to authenticated;
grant insert, update, delete on public.payments to authenticated;

-- Ensure Free Trial exists.
insert into public.plans(name, price, duration_days, features, is_active)
values (
  'Free Trial', 0, 7,
  '{"live_price":true,"option_chain":true,"strategy":true,"paper_trading":true}'::jsonb,
  true
)
on conflict (name) do nothing;

-- Secure automatic trial creator using the EXISTING subscription schema.
create or replace function public.ensure_trade_easy_trial()
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  uid uuid := auth.uid();
  existing_id uuid;
  existing_status text;
  trial_days integer;
  new_end timestamptz;
  new_id uuid;
begin
  if uid is null then
    raise exception 'Not authenticated';
  end if;

  select s.id, s.status
  into existing_id, existing_status
  from public.subscriptions s
  where s.user_id = uid
  order by s.ends_at desc nulls last
  limit 1;

  if existing_id is not null then
    return jsonb_build_object('subscription_id', existing_id, 'status', existing_status);
  end if;

  select duration_days
  into trial_days
  from public.plans
  where lower(name) = 'free trial'
    and is_active = true
  order by created_at
  limit 1;

  if trial_days is null then
    raise exception 'Free Trial plan is not configured';
  end if;

  new_end := now() + make_interval(days => trial_days);

  insert into public.subscriptions
  (
    user_id, plan, provider, status, starts_at, ends_at, notes, updated_at
  )
  values
  (
    uid, 'FREE', 'INTERNAL', 'TRIAL', now(), new_end,
    'Automatic first-login trial', now()
  )
  returning id into new_id;

  return jsonb_build_object('subscription_id', new_id, 'status', 'TRIAL');
end;
$$;

revoke all on function public.ensure_trade_easy_trial() from public;
grant execute on function public.ensure_trade_easy_trial() to authenticated;

-- Keep normal users limited to their own subscription rows; active admin can see all.
alter table public.subscriptions enable row level security;

drop policy if exists subscriptions_select_self_or_admin on public.subscriptions;
create policy subscriptions_select_self_or_admin
on public.subscriptions
for select to authenticated
using (user_id = auth.uid() or public.trade_easy_is_admin());

-- Admin writes are allowed by the admin predicate.
drop policy if exists subscriptions_admin_write on public.subscriptions;
create policy subscriptions_admin_write
on public.subscriptions
for all to authenticated
using (public.trade_easy_is_admin())
with check (public.trade_easy_is_admin());
"""




def login_page():
    """Compact centered popup-style authentication screen."""
    st.markdown("""
    <style>
    /* Compact popup: logo and form live in the same visual card. */
    .stApp {
        background:
            radial-gradient(circle at 15% 18%, rgba(38,99,235,.20), transparent 30%),
            radial-gradient(circle at 85% 18%, rgba(139,92,246,.16), transparent 28%),
            linear-gradient(135deg,#050b16 0%,#0a1222 48%,#060b14 100%);
    }
    [data-testid="stHeader"] { background: transparent; }
    [data-testid="stToolbar"] { display:none; }

    /* The authentication row itself becomes the popup. */
    .stApp .stHorizontalBlock {
        max-width: 520px !important;
        margin: 7vh auto 0 auto !important;
        align-items: stretch !important;
    }
    .stApp .stHorizontalBlock > div[data-testid="column"] {
        display: none;
    }
    .stApp .stHorizontalBlock > div[data-testid="column"]:nth-child(2) {
        display: block !important;
        flex: 0 0 100% !important;
        width: 100% !important;
        max-width: 100% !important;
        padding: 24px 34px 24px !important;
        border-radius: 22px !important;
        background: rgba(12,20,35,.92) !important;
        border: 1px solid rgba(148,163,184,.20) !important;
        box-shadow: 0 24px 80px rgba(0,0,0,.48), inset 0 1px 0 rgba(255,255,255,.05) !important;
        backdrop-filter: blur(18px);
        box-sizing: border-box !important;
    }

    .te-auth-brand { text-align:center; margin:0 0 12px 0; }
    .te-auth-logo {
        width:52px;height:52px;margin:0 auto 7px;border-radius:15px;
        display:flex;align-items:center;justify-content:center;
        font-size:25px;font-weight:900;
        background:linear-gradient(135deg,#2563eb,#7c3aed);
        color:#fff;box-shadow:0 9px 25px rgba(37,99,235,.25);
    }
    .te-auth-title { color:#f8fafc;font-size:24px;font-weight:850;line-height:1.05;letter-spacing:-.4px; }
    .te-auth-sub { color:#94a3b8;font-size:11px;margin-top:4px; }

    div[data-testid="stTabs"] { margin-top: 2px !important; }
    div[data-testid="stTabs"] button { font-size:13px !important; font-weight:700 !important; }
    div[data-testid="stTabsContent"] { padding-top: 10px !important; }

    /* White input boxes with dark text for clear typing. */
    div[data-testid="stTextInput"] { margin-bottom: 7px !important; }
    div[data-testid="stTextInput"] label {
        color:#cbd5e1 !important;
        font-size:12px !important;
        font-weight:600 !important;
        margin-bottom:3px !important;
    }
    div[data-testid="stTextInput"] input,
    div[data-testid="stTextInput"] input:focus {
        background:#ffffff !important;
        color:#111827 !important;
        -webkit-text-fill-color:#111827 !important;
        caret-color:#111827 !important;
        border:1px solid #cbd5e1 !important;
        border-radius:10px !important;
        box-shadow:none !important;
        min-height:40px !important;
    }
    div[data-testid="stTextInput"] input::placeholder {
        color:#6b7280 !important;
        opacity:1 !important;
    }
    div[data-testid="stTextInput"] input:focus {
        border-color:#64748b !important;
        box-shadow:0 0 0 2px rgba(59,130,246,.14) !important;
    }

    div.stButton { margin-top:7px !important; }
    div.stButton > button {
        border-radius:10px !important;
        min-height:40px !important;
        font-weight:700 !important;
    }
    .te-auth-caption {
        color:#64748b;font-size:10px;text-align:center;margin-top:10px;
    }

    @media (max-width: 640px) {
        .stApp .stHorizontalBlock {
            max-width: calc(100% - 24px) !important;
            margin-top: 4vh !important;
        }
        .stApp .stHorizontalBlock > div[data-testid="column"]:nth-child(2) {
            padding:20px 18px 18px !important;
        }
    }
    </style>
    """, unsafe_allow_html=True)

    left, center, right = st.columns([1.15, 1.7, 1.15])
    with center:
        st.markdown("""
        <div class="te-auth-brand">
          <div class="te-auth-logo">TE</div>
          <div class="te-auth-title">Trade Easy</div>
          <div class="te-auth-sub">Index Trading Confirmation &amp; Risk Control</div>
        </div>
        """, unsafe_allow_html=True)

        login_tab, signup_tab = st.tabs(["🔐 Login", "🆕 Create Account"])

        with login_tab:
            email = st.text_input("Email", key="login_email", placeholder="you@example.com")
            password = st.text_input("Password", type="password", key="login_password", placeholder="Enter your password")

            if st.button("Login", use_container_width=True, type="primary", key="login_btn"):
                try:
                    result = supabase.auth.sign_in_with_password(
                        {"email": email.strip(), "password": password}
                    )
                    if getattr(result, "user", None):
                        st.success("Login successful")
                        st.rerun()
                    else:
                        st.error("Login failed.")
                except Exception as e:
                    st.error(f"Login error: {e}")

            forgot_left, forgot_col, forgot_right = st.columns([1, 1.35, 1])
            with forgot_col:
                if st.button("Forgot Password?", use_container_width=True, key="forgot_password_btn"):
                    if not email.strip():
                        st.warning("पहले अपना email address डालें।")
                    else:
                        try:
                            password_reset_url = f"{TRADE_EASY_PUBLIC_URL.rstrip('/')}/?reset_password=1"
                            supabase.auth.reset_password_for_email(
                                email.strip(),
                                {"redirect_to": password_reset_url},
                            )
                            st.success("Password reset link भेज दिया गया है। Email खोलें और नया password सेट करें।")
                        except Exception as e:
                            st.error(f"Password reset error: {e}")

            if st.button("Continue with Google", use_container_width=True, key="google_login_btn"):
                try:
                    response = supabase.auth.sign_in_with_oauth(
                        {"provider": "google", "options": {"redirect_to": REDIRECT_URL}}
                    )
                    url = getattr(response, "url", None)
                    if url:
                        st.markdown(
                            f'<meta http-equiv="refresh" content="0; url={url}">',
                            unsafe_allow_html=True,
                        )
                        st.info("Google Login खोल रहा है...")
                    else:
                        st.error("Google OAuth URL नहीं मिला।")
                except Exception as e:
                    st.error(f"Google login error: {e}")

        with signup_tab:
            name = st.text_input("Name", key="signup_name", placeholder="Your name")
            email = st.text_input("Email", key="signup_email", placeholder="you@example.com")
            password = st.text_input("Password", type="password", key="signup_password", placeholder="Create a password")

            if st.button("Create Account", use_container_width=True, type="primary", key="signup_btn"):
                try:
                    result = supabase.auth.sign_up(
                        {
                            "email": email.strip(),
                            "password": password,
                            "options": {
                                "data": {
                                    "display_name": name.strip(),
                                    "full_name": name.strip(),
                                }
                            },
                        }
                    )
                    if getattr(result, "user", None):
                        st.success("Account created. अगर email confirmation enabled है तो पहले email confirm करें।")
                    else:
                        st.error("Account creation failed.")
                except Exception as e:
                    st.error(f"Signup error: {e}")

        st.markdown('<div class="te-auth-caption">Secure authentication • Trade Easy</div>', unsafe_allow_html=True)


# ============================================================
# DATA LAYER
# PDF: broker/exchange feed, candle normalization, validation
# ============================================================

REQUIRED_COLUMNS = [
    "timestamp", "open", "high", "low", "close", "volume"
]


def normalize_candles(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    rename_map = {}
    for c in out.columns:
        key = str(c).strip().lower()
        rename_map[c] = {
            "time": "timestamp",
            "datetime": "timestamp",
            "date": "timestamp",
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "volume",
        }.get(key, key)

    out = out.rename(columns=rename_map)

    missing = [c for c in REQUIRED_COLUMNS if c not in out.columns]
    if missing:
        raise ValueError(f"Missing candle columns: {', '.join(missing)}")

    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce", utc=True)

    for c in ["open", "high", "low", "close", "volume"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")

    # FYERS can occasionally return the same candle timestamp more than once.
    # Keep the latest copy so duplicate rows do not falsely block the engine.
    out = (
        out.dropna(subset=REQUIRED_COLUMNS)
        .sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"], keep="last")
    )
    return out.reset_index(drop=True)


def validate_candles(df: pd.DataFrame, timeframe_minutes: int, max_stale_minutes: int = 10):
    reasons = []

    if df.empty:
        return False, ["NO_DATA"]

    if df["timestamp"].duplicated().any():
        reasons.append("DUPLICATE_CANDLE")

    if not (df["high"] >= df[["open", "close"]].max(axis=1)).all():
        reasons.append("INVALID_HIGH")

    if not (df["low"] <= df[["open", "close"]].min(axis=1)).all():
        reasons.append("INVALID_LOW")

    if (df["high"] < df["low"]).any():
        reasons.append("HIGH_BELOW_LOW")

    if (df["volume"] < 0).any():
        reasons.append("NEGATIVE_VOLUME")

    now = pd.Timestamp.now(tz="UTC")
    last_ts = df["timestamp"].iloc[-1]
    age = (now - last_ts).total_seconds() / 60.0

    # Outside the NSE cash-market session, the latest completed candle is
    # expected to be older than max_stale_minutes. Do not falsely block the
    # dashboard after market close; during market hours stale data still blocks.
    ist_now = now.tz_convert("Asia/Kolkata")
    market_open = (
        ist_now.weekday() < 5
        and (ist_now.hour, ist_now.minute) >= (9, 15)
        and (ist_now.hour, ist_now.minute) <= (15, 30)
    )

    if market_open and age > max_stale_minutes:
        reasons.append("STALE_DATA")

    # The last candle is considered incomplete unless its interval has closed.
    interval_end = last_ts + pd.Timedelta(minutes=timeframe_minutes)
    if interval_end > now:
        reasons.append("INCOMPLETE_CANDLE")

    return len(reasons) == 0, reasons


# ============================================================
# INDICATORS
# PDF: VWAP, MA, RSI, MACD, ATR, volume, swings
# ============================================================

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df, n=14):
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def add_indicators(df):
    out = df.copy()
    typical = (out["high"] + out["low"] + out["close"]) / 3
    out["vwap"] = (typical * out["volume"]).cumsum() / out["volume"].replace(0, np.nan).cumsum()
    out["ma_fast"] = out["close"].rolling(9).mean()
    out["ma_slow"] = out["close"].rolling(21).mean()
    out["ema_fast"] = ema(out["close"], 12)
    out["ema_slow"] = ema(out["close"], 26)

    # Fast 5/8 EMA entry-timing layer. The 12/26 pair remains the MACD pair.
    out["ema_5"] = ema(out["close"], 5)
    out["ema_8"] = ema(out["close"], 8)
    out["ema_5_8_spread"] = out["ema_5"] - out["ema_8"]
    out["ema_5_slope"] = out["ema_5"].diff()

    out["macd"] = out["ema_fast"] - out["ema_slow"]
    out["macd_signal"] = ema(out["macd"], 9)
    out["rsi"] = rsi(out["close"], 14)
    out["atr"] = atr(out, 14)
    out["volume_ma"] = out["volume"].rolling(20).mean()
    out["adx"] = adx(out, 14)
    return out


def market_structure(df):
    if len(df) < 10:
        return "UNKNOWN"

    recent = df.tail(10)
    highs = recent["high"].to_numpy()
    lows = recent["low"].to_numpy()

    higher_high = highs[-1] > highs[-4]
    higher_low = lows[-1] > lows[-4]
    lower_high = highs[-1] < highs[-4]
    lower_low = lows[-1] < lows[-4]

    if higher_high and higher_low:
        return "BULLISH"
    if lower_high and lower_low:
        return "BEARISH"
    return "SIDEWAYS"


def higher_timeframe_bias(df):
    if len(df) < 30:
        return "UNKNOWN"

    close = df["close"]
    fast = close.rolling(9).mean().iloc[-1]
    slow = close.rolling(21).mean().iloc[-1]

    if fast > slow and close.iloc[-1] > fast:
        return "BULLISH"
    if fast < slow and close.iloc[-1] < fast:
        return "BEARISH"
    return "SIDEWAYS"


# ============================================================
# KEY LEVELS
# PDF: previous day H/L/C, open/range, weekly levels,
# support/resistance, VWAP, optional gap levels
# ============================================================

def key_levels(df):
    x = df.copy()
    x["date"] = x["timestamp"].dt.date

    latest_date = x["date"].iloc[-1]
    today = x[x["date"] == latest_date]

    prior_days = x[x["date"] < latest_date]
    prev = prior_days.tail(len(prior_days))
    prev_day = None

    if not prior_days.empty:
        pdate = prior_days["date"].iloc[-1]
        prev_day = prior_days[prior_days["date"] == pdate]

    prev_high = float(prev_day["high"].max()) if prev_day is not None else np.nan
    prev_low = float(prev_day["low"].min()) if prev_day is not None else np.nan
    prev_close = float(prev_day["close"].iloc[-1]) if prev_day is not None else np.nan

    day_open = float(today["open"].iloc[0])
    day_high = float(today["high"].max())
    day_low = float(today["low"].min())

    week = x.tail(min(len(x), 5 * 78))
    weekly_high = float(week["high"].max())
    weekly_low = float(week["low"].min())

    swing_high = float(x.tail(20)["high"].max())
    swing_low = float(x.tail(20)["low"].min())

    return {
        "previous_day_high": prev_high,
        "previous_day_low": prev_low,
        "previous_day_close": prev_close,
        "day_open": day_open,
        "opening_range_high": day_high,
        "opening_range_low": day_low,
        "weekly_high": weekly_high,
        "weekly_low": weekly_low,
        "swing_high": swing_high,
        "swing_low": swing_low,
        "vwap": float(x["vwap"].iloc[-1]),
    }


# ============================================================
# PRICE ACTION
# PDF: sweep, rejection, structure break, retest
# ============================================================

def price_action_checks(df, levels):
    if len(df) < 5:
        return {
            "sweep_confirmed": False,
            "structure_break": False,
            "retest_confirmed": False,
            "direction": None,
        }

    a = df.iloc[-1]
    b = df.iloc[-2]
    recent_low = df["low"].tail(10).min()
    recent_high = df["high"].tail(10).max()

    bullish_sweep = b["low"] < recent_low and a["close"] > b["low"]
    bearish_sweep = b["high"] > recent_high and a["close"] < b["high"]

    bullish_structure = a["close"] > df["high"].tail(5).iloc[:-1].max()
    bearish_structure = a["close"] < df["low"].tail(5).iloc[:-1].min()

    # Simple retest definition: current close remains beyond the break level.
    retest_bull = bullish_structure and a["low"] <= b["high"] and a["close"] > b["high"]
    retest_bear = bearish_structure and a["high"] >= b["low"] and a["close"] < b["low"]

    if bullish_sweep or bullish_structure:
        direction = "LONG"
    elif bearish_sweep or bearish_structure:
        direction = "SHORT"
    else:
        direction = None

    return {
        "sweep_confirmed": bool(bullish_sweep or bearish_sweep),
        "structure_break": bool(bullish_structure or bearish_structure),
        "retest_confirmed": bool(retest_bull or retest_bear),
        "direction": direction,
    }


# ============================================================
# SCORE + RISK
# PDF thresholds:
# >=75 + all mandatory checks => trade candidate
# 60-74 => WAIT
# <60 => NO TRADE
# any mandatory risk failure => BLOCKED
# ============================================================

def ema_5_8_confirmation(df, direction):
    """5/8 EMA momentum filter for the latest completed candle."""
    result = {"available": False, "confirmed": False, "trend": "UNKNOWN",
              "cross": "NONE", "price_position": "UNKNOWN", "spread": np.nan,
              "slope": np.nan, "reason": "EMA_DATA_UNAVAILABLE"}
    if df is None or df.empty or len(df) < 2 or direction not in ("LONG", "SHORT"):
        return result
    if not all(c in df.columns for c in ("ema_5", "ema_8", "ema_5_slope")):
        return result
    last, prev = df.iloc[-1], df.iloc[-2]
    if any(pd.isna(last[c]) for c in ("ema_5", "ema_8", "ema_5_slope")):
        return result
    e5, e8 = float(last["ema_5"]), float(last["ema_8"])
    slope, price = float(last["ema_5_slope"]), float(last["close"])
    bullish, bearish = e5 > e8, e5 < e8
    rising, falling = slope > 0, slope < 0
    above, below = price > e5, price < e5
    bull_cross = bool(float(prev["ema_5"]) <= float(prev["ema_8"]) and e5 > e8)
    bear_cross = bool(float(prev["ema_5"]) >= float(prev["ema_8"]) and e5 < e8)
    result.update({
        "available": True,
        "confirmed": bool((bullish and rising and above) if direction == "LONG" else (bearish and falling and below)),
        "trend": "BULLISH" if bullish else ("BEARISH" if bearish else "FLAT"),
        "cross": "BULLISH CROSS" if bull_cross else ("BEARISH CROSS" if bear_cross else "NONE"),
        "price_position": "ABOVE EMA5" if above else ("BELOW EMA5" if below else "ON EMA5"),
        "spread": e5-e8, "slope": slope,
    })
    result["reason"] = ("EMA_5_8_BULLISH_ALIGNMENT" if result["confirmed"] else "EMA_5_8_LONG_PENDING") if direction == "LONG" else ("EMA_5_8_BEARISH_ALIGNMENT" if result["confirmed"] else "EMA_5_8_SHORT_PENDING")
    return result


def score_signal(df, levels, pa, timeframe_minutes):
    last = df.iloc[-1]
    score = 0
    reasons = []
    invalidations = []

    bias = higher_timeframe_bias(df)
    structure = market_structure(df)

    # Direction fallback: when price-action has no explicit sweep/structure-break,
    # use an aligned HTF bias + market structure as the direction. This prevents
    # valid bearish/bullish setups from becoming NO_VALID_DIRECTION simply because
    # the latest candle did not trigger the stricter price-action event.
    direction = pa.get("direction")
    if direction is None and bias == "BULLISH" and structure == "BULLISH":
        direction = "LONG"
        reasons.append("HTF_STRUCTURE_ALIGNED")
    elif direction is None and bias == "BEARISH" and structure == "BEARISH":
        direction = "SHORT"
        reasons.append("HTF_STRUCTURE_ALIGNED")

    if direction == "LONG":
        if bias == "BULLISH":
            score += 20
            reasons.append("HTF_BULLISH")
        elif bias == "BEARISH":
            score -= 10
            invalidations.append("HTF_CONFLICT")

        if last["close"] > last["vwap"]:
            score += 10
            reasons.append("ABOVE_VWAP")

        if last["close"] > last["ma_fast"]:
            score += 10
            reasons.append("ABOVE_FAST_MA")

        if last["volume"] > (last["volume_ma"] if pd.notna(last["volume_ma"]) else 0):
            score += 10
            reasons.append("VOLUME_CONFIRMED")

        if 50 <= last["rsi"] <= 70:
            score += 5
            reasons.append("RSI_SUPPORTIVE")

        if last["macd"] > last["macd_signal"]:
            score += 5
            reasons.append("MACD_SUPPORTIVE")

        if pa["sweep_confirmed"]:
            score += 10
            reasons.append("LOW_SWEEP")

        if pa["structure_break"]:
            score += 10
            reasons.append("BULLISH_STRUCTURE_BREAK")

        if pa["retest_confirmed"]:
            score += 10
            reasons.append("RETEST_CONFIRMED")

        ema_state = ema_5_8_confirmation(df, "LONG")
        if ema_state["confirmed"]:
            score += 10
            reasons.append("EMA_5_8_CONFIRMED")
        elif ema_state["available"]:
            reasons.append(ema_state["reason"])

        return min(score, 100), "LONG", bias, structure, reasons, invalidations

    if direction == "SHORT":
        if bias == "BEARISH":
            score += 20
            reasons.append("HTF_BEARISH")
        elif bias == "BULLISH":
            score -= 10
            invalidations.append("HTF_CONFLICT")

        if last["close"] < last["vwap"]:
            score += 10
            reasons.append("BELOW_VWAP")

        if last["close"] < last["ma_fast"]:
            score += 10
            reasons.append("BELOW_FAST_MA")

        if last["volume"] > (last["volume_ma"] if pd.notna(last["volume_ma"]) else 0):
            score += 10
            reasons.append("VOLUME_CONFIRMED")

        if 30 <= last["rsi"] <= 50:
            score += 5
            reasons.append("RSI_SUPPORTIVE")

        if last["macd"] < last["macd_signal"]:
            score += 5
            reasons.append("MACD_SUPPORTIVE")

        if pa["sweep_confirmed"]:
            score += 10
            reasons.append("HIGH_SWEEP")

        if pa["structure_break"]:
            score += 10
            reasons.append("BEARISH_STRUCTURE_BREAK")

        if pa["retest_confirmed"]:
            score += 10
            reasons.append("RETEST_CONFIRMED")

        ema_state = ema_5_8_confirmation(df, "SHORT")
        if ema_state["confirmed"]:
            score += 10
            reasons.append("EMA_5_8_CONFIRMED")
        elif ema_state["available"]:
            reasons.append(ema_state["reason"])

        return min(score, 100), "SHORT", bias, structure, reasons, invalidations

    return 0, None, bias, structure, reasons, ["NO_VALID_DIRECTION"]


def calculate_trade_plan(df, direction, levels, risk_amount, min_rr=1.5):
    last = df.iloc[-1]
    entry = float(last["close"])
    atr_value = float(last["atr"]) if pd.notna(last["atr"]) else 0.0

    if atr_value <= 0:
        return None

    # Fixed paper-trading exits requested by the user:
    # LONG  -> SL 25 points below entry, Target 62 points above entry
    # SHORT -> SL 25 points above entry, Target 62 points below entry
    if direction == "LONG":
        sl = entry - FIXED_STOP_LOSS_POINTS
        target = entry + FIXED_TARGET_POINTS
    else:
        sl = entry + FIXED_STOP_LOSS_POINTS
        target = entry - FIXED_TARGET_POINTS

    risk_per_unit = FIXED_STOP_LOSS_POINTS

    if risk_per_unit <= 0:
        return None

    # Paper execution quantity is fixed at 65 as requested.
    # risk_amount remains available for the existing risk controls/plan display.
    quantity = PAPER_FIXED_QUANTITY
    rr = abs(target - entry) / risk_per_unit

    return {
        "entry": entry,
        "stop_loss": sl,
        "target": target,
        "risk_per_unit": risk_per_unit,
        "quantity": PAPER_FIXED_QUANTITY,
        "risk_reward": rr,
    }


def adx(df, n=14):
    """Wilder-style ADX used only as a supporting trend-strength filter."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    prev_close = close.shift(1)
    tr = pd.concat([(high-low), (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    atr_w = tr.ewm(alpha=1/n, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/n, adjust=False).mean() / atr_w.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1/n, adjust=False).mean() / atr_w.replace(0, np.nan)
    dx = 100 * (plus_di-minus_di).abs() / (plus_di+minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1/n, adjust=False).mean()


def market_session_status(expiry_block=False, holiday_dates=None):
    """Validate IST trading session without preventing the dashboard from showing data."""
    now = pd.Timestamp.now(tz="Asia/Kolkata")
    date_key = now.strftime("%Y-%m-%d")
    holiday_dates = set(holiday_dates or [])
    weekend = now.weekday() >= 5
    holiday = date_key in holiday_dates
    in_session = (now.hour, now.minute) >= (9, 15) and (now.hour, now.minute) <= (15, 30)
    expiry_day = now.weekday() == 3  # NIFTY weekly expiry convention; keep separately configurable.
    failures = []
    if weekend:
        failures.append("WEEKEND_SESSION")
    if holiday:
        failures.append("MARKET_HOLIDAY")
    if not in_session:
        failures.append("OUTSIDE_MARKET_SESSION")
    if expiry_block and expiry_day and in_session:
        failures.append("EXPIRY_DAY_BLOCK")
    return {
        "now": now,
        "in_session": in_session,
        "weekend": weekend,
        "holiday": holiday,
        "expiry_day": expiry_day,
        "failures": failures,
    }


def entry_trigger_and_distance(df, direction):
    """Return the latest structural trigger and its ATR-normalized distance."""
    if df.empty or direction not in ("LONG", "SHORT"):
        return None, None, None
    last = df.iloc[-1]
    atr_value = float(last["atr"]) if pd.notna(last["atr"]) else 0.0
    if atr_value <= 0:
        return None, None, None
    prior = df.iloc[:-1].tail(5)
    if prior.empty:
        return None, None, None
    trigger = float(prior["high"].max()) if direction == "LONG" else float(prior["low"].min())
    distance = abs(float(last["close"]) - trigger)
    return trigger, distance, distance / atr_value


def setup_expiry_status(df, direction, trigger, max_candles):
    """Track a setup in session state and expire it after N completed candles."""
    if df.empty or direction not in ("LONG", "SHORT") or trigger is None:
        return {"expired": False, "age": 0, "key": None}
    candle_ts = pd.Timestamp(df["timestamp"].iloc[-1])
    key = f"{direction}:{round(float(trigger), 2)}"
    state = st.session_state.get("trade_easy_setup_state")
    if not state or state.get("key") != key:
        state = {"key": key, "first_candle": candle_ts.isoformat(), "last_candle": candle_ts.isoformat(), "age": 0}
    else:
        previous = pd.Timestamp(state.get("last_candle"))
        if candle_ts > previous:
            state["age"] = int(state.get("age", 0)) + 1
            state["last_candle"] = candle_ts.isoformat()
    st.session_state["trade_easy_setup_state"] = state
    expired = int(state.get("age", 0)) >= int(max_candles)
    return {"expired": expired, "age": int(state.get("age", 0)), "key": key}


def duplicate_entry_status(setup_key, direction, trades_taken_today, cooldown_candles=3):
    """Prevent repeated entries for the same setup after a paper entry is recorded."""
    if not setup_key or not direction or trades_taken_today <= 0:
        return False, None
    last = st.session_state.get("trade_easy_last_entry")
    if not last:
        return False, None
    same_setup = last.get("setup_key") == setup_key and last.get("direction") == direction
    if same_setup:
        return True, "DUPLICATE_ENTRY"
    return False, None


def paper_risk_checks(daily_pnl, daily_loss_limit, trades_today, max_trades, open_positions, max_open_positions):
    failures = []
    if daily_pnl <= -abs(float(daily_loss_limit)):
        failures.append("DAILY_LOSS_LIMIT")
    if int(trades_today) >= int(max_trades):
        failures.append("MAX_TRADES_REACHED")
    if int(open_positions) >= int(max_open_positions):
        failures.append("OPEN_POSITION_LIMIT")
    return len(failures) == 0, failures


def mandatory_risk_checks(df, plan, max_spread=999999):
    failures = []

    if plan is None:
        failures.append("TRADE_PLAN_UNAVAILABLE")
        return False, failures

    if not np.isfinite(plan["entry"]):
        failures.append("INVALID_ENTRY")

    if not np.isfinite(plan["stop_loss"]):
        failures.append("INVALID_STOP")

    if not np.isfinite(plan["target"]):
        failures.append("INVALID_TARGET")

    if plan["risk_reward"] < 1.5:
        failures.append("RISK_REWARD_TOO_LOW")

    if plan["quantity"] <= 0:
        failures.append("QUANTITY_ZERO")

    return len(failures) == 0, failures


def decide(score, data_ok, risk_ok, direction, news_block=False):
    if not data_ok:
        return "BLOCKED"

    if news_block:
        return "BLOCKED"

    if not risk_ok:
        return "BLOCKED"

    if not direction:
        return "WAIT"

    if score >= 75:
        return "BUY" if direction == "LONG" else "SELL"

    if score >= 60:
        return "WAIT"

    return "WAIT"


# ============================================================
# AUDIT STORAGE
# ============================================================

def audit_signal(user_id, workspace_id, payload):
    try:
        row = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "workspace_id": workspace_id,
            "symbol": payload.get("symbol", "INDEX"),
            "direction": payload.get("direction"),
            "signal": payload.get("signal"),
            "score": int(payload.get("score", 0)),
            "timeframe": payload.get("timeframe", "5m"),
            "entry": payload.get("entry"),
            "stop_loss": payload.get("stop_loss"),
            "target": payload.get("target"),
            "risk_reward": payload.get("risk_reward"),
            "reasons": payload.get("reasons", []),
            "invalidations": payload.get("invalidations", []),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        supabase.table("signals").insert(row).execute()
    except Exception:
        # The dashboard must remain usable even if the optional audit
        # table has not yet been provisioned.
        pass


# ============================================================
# PHASE 2 — PAPER EXECUTION ENGINE
# Paper-only position lifecycle. No broker order API is called here.
# State is persisted locally so a Streamlit rerun/restart does not erase
# the paper position or today's realized P&L.
# ============================================================

PAPER_STATE_DIR = Path.home() / ".trade_easy_paper"


def _paper_state_path(user_id, workspace_id):
    key = f"{user_id or 'anonymous'}:{workspace_id or 'default'}"
    digest = sha256(key.encode("utf-8")).hexdigest()[:20]
    PAPER_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return PAPER_STATE_DIR / f"state_{digest}.json"


def _paper_default_state():
    return {
        "date": pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d"),
        "daily_realized_pnl": 0.0,
        "trades_today": 0,
        "open_position": None,
        "last_entry_signature": None,
        "trade_history": [],
    }


def load_paper_state(user_id, workspace_id):
    path = _paper_state_path(user_id, workspace_id)
    state = _paper_default_state()
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state.update(raw)
    except Exception:
        pass

    today = pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d")
    if state.get("date") != today:
        # Start a new daily risk bucket. Keep the open paper position visible
        # for safety instead of silently deleting it across a restart/day change.
        state["date"] = today
        state["daily_realized_pnl"] = 0.0
        state["trades_today"] = 0
        state["last_entry_signature"] = None

    state.setdefault("trade_history", [])
    state.setdefault("open_position", None)
    state.setdefault("audit_log", [])
    state.setdefault("paper_kill_switch", False)

    # Keep any persisted open paper position on the same fixed 25/62-point
    # exit rules, including after a browser/app restart.
    pos = state.get("open_position")
    if isinstance(pos, dict) and pos.get("entry_price") is not None:
        entry_price = float(pos["entry_price"])
        direction = pos.get("direction")
        if direction == "LONG":
            pos["stop_loss"] = entry_price - FIXED_STOP_LOSS_POINTS
            pos["target"] = entry_price + FIXED_TARGET_POINTS
        elif direction == "SHORT":
            pos["stop_loss"] = entry_price + FIXED_STOP_LOSS_POINTS
            pos["target"] = entry_price - FIXED_TARGET_POINTS
        pos["quantity"] = PAPER_FIXED_QUANTITY
        pos["risk_reward"] = FIXED_TARGET_POINTS / FIXED_STOP_LOSS_POINTS

    return state


def save_paper_state(user_id, workspace_id, state):
    path = _paper_state_path(user_id, workspace_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def paper_audit_event(state, event_type, **details):
    """Append an immutable-style local audit event for paper execution/monitoring."""
    event = {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": str(event_type),
        **details,
    }
    state.setdefault("audit_log", [])
    state["audit_log"] = (state["audit_log"] + [event])[-500:]
    return event


def paper_unrealized_pnl(position, price):
    if not position or price is None:
        return 0.0
    entry = float(position["entry_price"])
    qty = int(position["quantity"])
    direction = position["direction"]
    return (float(price) - entry) * qty if direction == "LONG" else (entry - float(price)) * qty


def paper_close_position(state, price, reason):
    position = state.get("open_position")
    if not position or price is None:
        return None
    exit_price = float(price)
    pnl = paper_unrealized_pnl(position, exit_price)
    closed = dict(position)
    closed.update({
        "exit_price": exit_price,
        "exit_time": datetime.now(timezone.utc).isoformat(),
        "exit_reason": reason,
        "realized_pnl": round(float(pnl), 2),
        "status": "CLOSED",
    })
    state["daily_realized_pnl"] = round(
        float(state.get("daily_realized_pnl", 0.0)) + float(pnl), 2
    )
    state["trade_history"] = (state.get("trade_history") or []) + [closed]
    state["open_position"] = None
    paper_audit_event(state, "PAPER_EXIT", reason=reason, trade=closed)
    return closed


def paper_execution_engine(
    state,
    *,
    symbol,
    direction,
    signal,
    plan,
    live_price,
    entry_alert,
    risk_ok,
    setup_key,
    candle_timestamp,
    max_trades,
    max_open_positions,
    daily_loss_limit,
):
    """Run one paper-only execution cycle and return event + current state."""
    event = None
    position = state.get("open_position")

    if state.get("paper_kill_switch") and position and live_price is not None:
        closed = paper_close_position(state, float(live_price), "PAPER_KILL_SWITCH")
        if closed:
            paper_audit_event(state, "PAPER_EXIT", reason="PAPER_KILL_SWITCH", trade=closed)
            event = f"Paper position closed by kill switch | P&L ₹{closed['realized_pnl']:,.2f}"
        position = None

    # 1) Manage an existing paper position first.
    if position and live_price is not None:
        px = float(live_price)
        direction_pos = position.get("direction")
        sl = float(position.get("stop_loss"))
        target = float(position.get("target"))

        # If a single live tick cannot tell the path, this uses the first
        # threshold reached by the live price. No candle interpolation is used.
        hit_reason = None
        if direction_pos == "LONG":
            if px <= sl:
                hit_reason = "STOP_LOSS"
            elif px >= target:
                hit_reason = "TARGET"
        else:
            if px >= sl:
                hit_reason = "STOP_LOSS"
            elif px <= target:
                hit_reason = "TARGET"

        if hit_reason:
            closed = paper_close_position(state, px, hit_reason)
            if closed:
                paper_audit_event(state, "PAPER_EXIT", reason=hit_reason, trade=closed)
                event = f"Paper position closed: {hit_reason} | P&L ₹{closed['realized_pnl']:,.2f}"
            position = None

    # 2) Open a new paper position only once per setup/candle.
    if (
        state.get("open_position") is None
        and entry_alert
        and signal in ("BUY", "SELL")
        and direction in ("LONG", "SHORT")
        and plan
        and risk_ok
    ):
        trades_today = int(state.get("trades_today", 0))
        daily_pnl = float(state.get("daily_realized_pnl", 0.0))
        if daily_pnl > -abs(float(daily_loss_limit)) and trades_today < int(max_trades):
            signature = f"{symbol}|{direction}|{setup_key}|{candle_timestamp}"
            if state.get("last_entry_signature") != signature:
                fill_price = float(live_price) if live_price is not None else float(plan["entry"])
                position = {
                    "id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "direction": direction,
                    "signal": signal,
                    "entry_price": fill_price,
                    "planned_entry": float(plan["entry"]),
                    "stop_loss": float(plan["stop_loss"]),
                    "target": float(plan["target"]),
                    "risk_reward": float(plan["risk_reward"]),
                    "quantity": int(PAPER_FIXED_QUANTITY),
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "setup_key": setup_key,
                    "candle_timestamp": str(candle_timestamp),
                    "status": "OPEN",
                }
                state["open_position"] = position
                state["trades_today"] = trades_today + 1
                state["last_entry_signature"] = signature
                event = (
                    f"Paper {signal} opened @ ₹{fill_price:,.2f} | "
                    f"Qty {PAPER_FIXED_QUANTITY} | SL ₹{float(plan['stop_loss']):,.2f} | "
                    f"Target ₹{float(plan['target']):,.2f}"
                )

    return state, event


# ============================================================
# SAMPLE DATA / IMPORT
# The PDF explicitly leaves broker/data source selection open.
# Therefore the first build supports CSV input without pretending
# a broker connection exists.
# ============================================================

def make_demo_data():
    now = pd.Timestamp.now(tz="UTC").floor("5min")
    periods = 180
    idx = pd.date_range(end=now - pd.Timedelta(minutes=5), periods=periods, freq="5min")

    rng = np.random.default_rng(42)
    drift = np.linspace(0, 90, periods)
    noise = rng.normal(0, 12, periods).cumsum()
    close = 24000 + drift + noise

    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.uniform(3, 18, periods)
    low = np.minimum(open_, close) - rng.uniform(3, 18, periods)
    volume = rng.integers(10000, 50000, periods)

    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


# ============================================================
# DASHBOARD
# ============================================================

def render_permanent_dashboard_shell():
    """Render every major dashboard box even without live market data.

    This is intentionally UI-only. It never fabricates market values. Each box
    stays visible and shows WAITING / — until the corresponding live or cached
    dataset becomes available.
    """
    st.markdown('<div class="section-head">📦 Dashboard — All Panels</div>', unsafe_allow_html=True)
    st.caption("सभी panels हमेशा दिखाई देंगे। Live-only values केवल market/live data उपलब्ध होने पर भरेंगी; कोई dummy market value नहीं दिखाई जाएगी।")

    # NOTE: Option Chain and Trade Finder V2 have their own dedicated, stable
    # render roots in dashboard(). They are intentionally NOT rendered here;
    # rendering them in this fallback shell would create duplicate panels.
    # Option Chain remains the only live-data-first section as requested.

    # Strategy / decision boxes
    shell_sections = [
        ("🎯 Entry Confirmation", ["Entry Score", "PA Confirmations", "5/8 EMA", "Entry Alert"]),
        ("📈 5/8 EMA Momentum Filter", ["EMA 5", "EMA 8", "Spread", "Trend", "Cross"]),
        ("🧭 Swing / Level Engine", ["Trend", "Setup", "Type", "Clear Path", "Raw Distance"]),
        ("📌 Market Snapshot", ["Score", "HTF Bias", "Structure", "RSI", "ATR", "5/8 EMA"]),
        ("📊 Market & Paper Status", ["Market", "Live Price", "WebSocket", "Paper Engine", "Trades Today", "Paper P&L"]),
        ("🛡️ Phase-1 Risk Controls", ["ADX", "Session", "Entry Distance", "Setup Age", "Paper Risk"]),
        ("🧾 Phase-2 Paper Execution", ["Paper Position", "Trades Today", "Realized P&L", "Unrealized P&L"]),
        ("🗺️ Paper Entry / Exit Map", ["Entry", "Stop Loss", "Target", "Risk/Reward", "Quantity"]),
        ("👁️ Phase-3 Monitoring & Audit", ["Paper Engine", "Live Price", "Audit Events", "Paper P&L"]),
        ("📣 Signal Output", ["Direction", "Signal", "Risk / Reward", "Quantity"]),
        ("🔎 Mandatory Checks", ["Data validation", "Completed candle", "Stale-data check", "Risk checks", "Minimum RR"]),
        ("🧱 Key Levels", ["Previous Day High", "Previous Day Low", "VWAP", "Swing High", "Swing Low"]),
        ("📝 Reasons / Invalidations", ["Reason codes", "Invalidations / blocks"]),
        ("🕯️ Validated Candle Data", ["Timestamp", "Open", "High", "Low", "Close", "Volume", "VWAP", "RSI", "ATR", "ADX"]),
    ]
    for title, labels in shell_sections:
        st.markdown(f'<div class="section-head">{title}</div>', unsafe_allow_html=True)
        cols = st.columns(min(len(labels), 6))
        for i, label in enumerate(labels):
            cols[i % len(cols)].metric(label, "—")
        if title == "📝 Reasons / Invalidations":
            r1, r2 = st.columns(2)
            r1.info("WAITING FOR MARKET DATA")
            r2.info("No live invalidation data yet")
        elif title == "🕯️ Validated Candle Data":
            st.dataframe(pd.DataFrame([{label: "—" for label in labels}]), use_container_width=True, hide_index=True)
        else:
            st.caption("WAITING FOR LIVE / COMPLETED-CANDLE DATA")


def dashboard(user, workspace):
    paper_user_id = getattr(user, "id", "")
    paper_workspace_id = workspace.get("id") if workspace else "default"
    paper_state = load_paper_state(paper_user_id, paper_workspace_id)

    # Paper reset is rendered later in a dedicated permanent root, immediately
    # before the Option Chain. Keeping it there makes the control visible in the
    # same viewport as the strategy/dashboard panels instead of placing it above
    # the page title where it can be missed after a scroll.
    # Normalize any legacy/open paper position to the permanent paper quantity.
    if paper_state.get("open_position"):
        paper_state["open_position"]["quantity"] = PAPER_FIXED_QUANTITY

    for _trade in (paper_state.get("trade_history") or []):
        if isinstance(_trade, dict):
            _trade["quantity"] = PAPER_FIXED_QUANTITY

    # UI display cache: no local JSON reads on every live-price tick.
    st.session_state["trade_easy_paper_display"] = {
        "daily_realized_pnl": float(paper_state.get("daily_realized_pnl", 0.0)),
        "trades_today": int(paper_state.get("trades_today", 0)),
        "open_position": paper_state.get("open_position"),
        "unrealized_pnl": 0.0,
        "version": int(st.session_state.get("trade_easy_paper_display_version", 0)),
    }

    st.markdown("""
    <style>
    /* ========================================================
       TRADE EASY — VISUAL CLARITY LAYER
       UI only: no trading/data logic is changed here.
       ======================================================== */
    .stApp {
        background:
            radial-gradient(circle at 8% 8%, rgba(45,110,255,.16), transparent 25%),
            radial-gradient(circle at 92% 8%, rgba(170,80,255,.12), transparent 28%),
            linear-gradient(135deg,#071426,#0d1930,#080d1b);
    }
    .block-container {
        padding-top: 1.35rem;
        padding-bottom: 2.5rem;
        max-width: 1500px;
    }
    h1 { font-size: 2.05rem !important; letter-spacing: -.02em; color:#f5f8ff !important; }
    h2, h3 { letter-spacing: -.01em; color:#eef4ff !important; }
    [data-testid="stMetric"] {
        background: rgba(255,255,255,.045);
        border: 1px solid rgba(255,255,255,.10);
        border-radius: 14px;
        padding: 12px 14px;
        min-height: 92px;
    }
    [data-testid="stMetricLabel"] {
        color: #aebbd2 !important;
        font-size: .78rem !important;
        font-weight: 700 !important;
    }
    [data-testid="stMetricValue"] {
        color: #f5f8ff !important;
        font-size: 1.35rem !important;
        font-weight: 850 !important;
    }
    .state-box {
        border-radius: 18px;
        padding: 20px 24px;
        margin: 8px 0 12px;
        border: 1px solid rgba(255,255,255,.13);
        background: linear-gradient(110deg, rgba(255,255,255,.075), rgba(255,255,255,.035));
        box-shadow: 0 12px 35px rgba(0,0,0,.18);
    }
    .state-title { font-size: 38px; line-height: 1.05; font-weight: 900; color:#fff; margin: 4px 0 7px; }
    .state-sub { color:#c4d0e4; font-size:.92rem; font-weight:650; }
    .muted { color:#aebbd2; }
    .section-head {
        margin: 22px 0 10px;
        padding-bottom: 7px;
        border-bottom: 1px solid rgba(255,255,255,.10);
        color:#eef4ff;
        font-size:1.08rem;
        font-weight:850;
    }
    .signal-card {
        border-radius:16px;
        padding:16px 18px;
        border:1px solid rgba(255,255,255,.12);
        background:rgba(255,255,255,.045);
        margin-bottom:10px;
    }
    .signal-label { color:#9eacc4; font-size:.72rem; font-weight:800; text-transform:uppercase; letter-spacing:.08em; }
    .signal-value { color:#f7f9ff; font-size:1.25rem; font-weight:900; margin-top:3px; }
    .signal-small { color:#c4d0e4; font-size:.83rem; margin-top:2px; }
    .status-pass { color:#7ff0b1; font-weight:850; }
    .status-fail { color:#ff8f9d; font-weight:850; }
    .pending-box {
        border-left: 4px solid #f5c542;
        background: rgba(245,197,66,.075);
        border-radius: 10px;
        padding: 10px 14px;
        color:#f7d873;
        font-weight:700;
        margin: 8px 0 14px;
    }
    .reason-item {
        background: rgba(255,255,255,.04);
        border:1px solid rgba(255,255,255,.08);
        border-radius:9px;
        padding:7px 10px;
        margin:5px 0;
        color:#dbe4f4;
        font-size:.86rem;
    }
    [data-testid="stDataFrame"] {
        border: 1px solid rgba(255,255,255,.10);
        border-radius: 12px;
        overflow: hidden;
    }
    .json-note {
        color:#9eacc4;
        font-size:.82rem;
        margin: -4px 0 8px;
    }
    /* Reduce visual repaint shimmer during 1-second fragment updates. */
    /* NO-BLINK / LOW-REPAINT MODE
       Live updates are confined to a Streamlit fragment. Disable browser/UI
       transitions, pulse animations and focus flashes commonly visible during
       fragment rerenders. */
    [data-testid="stVerticalBlock"] { scroll-margin-top: 0; }
    .stApp, .main, [data-testid="stAppViewContainer"],
    [data-testid="stVerticalBlock"], [data-testid="stHorizontalBlock"],
    [data-testid="stMetric"], [data-testid="stAlert"],
    [data-testid="stMarkdownContainer"], [data-testid="stDataFrame"] {
        transition: none !important;
        animation: none !important;
    }
    [data-testid="stSpinner"], [data-testid="stStatusWidget"] {
        transition: none !important;
        animation: none !important;
    }
    /* Compact live ticker: one visual block, not five separate Streamlit widgets. */
    .live-ticker-shell { margin: 8px 0 14px; padding: 10px 12px; border: 1px solid rgba(120,150,210,.18); border-radius: 12px; background: rgba(9,18,38,.72); }
    .live-ticker-top { font-size: .78rem; color: #9fb0ca; margin-bottom: 8px; }
    .live-ticker-top b { color: #dce7f7; }
    .live-ticker-grid { display: grid; grid-template-columns: repeat(6, minmax(0,1fr)); gap: 8px; }
    .live-cell { min-width: 0; padding: 8px 10px; border-radius: 9px; background: rgba(255,255,255,.035); border: 1px solid rgba(255,255,255,.06); }
    .live-cell span { display:block; font-size:.62rem; color:#8d9bb3; letter-spacing:.04em; }
    .live-cell strong { display:block; margin-top:3px; font-size:1rem; color:#f2f6ff; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .live-cell.price strong { color:#74f0b1; }
    .live-cell.vix strong { color:#ffd36a; }
    .live-cell small { font-size:.62rem; color:#9fb0ca; font-weight:500; }
    .dot { display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:5px; }
    .dot.live { background:#35df91; }
    .dot.wait { background:#f2b84b; }
    .live-event { margin-top:8px; padding:7px 9px; border-radius:8px; background:rgba(255,190,70,.08); color:#ffd77c; font-size:.75rem; }
    @media (max-width: 900px) { .live-ticker-grid { grid-template-columns: repeat(3, minmax(0,1fr)); } }
    /* Keep the browser viewport stable while the fragment updates. */
    html, body { scroll-behavior: auto !important; }

    /* TOP HEADER + LOGOUT VISIBILITY FIX
       Keep Streamlit's top header from appearing as a white strip over the
       dashboard controls, and make dashboard action buttons readable. */
    /* Remove Streamlit Cloud's built-in Share / Star / Edit / More toolbar. */
    [data-testid="stToolbar"],
    [data-testid="stHeaderActionElements"],
    [data-testid="stDecoration"],
    [data-testid="stStatusWidget"] {
        display: none !important;
        visibility: hidden !important;
        pointer-events: none !important;
    }
    [data-testid="stHeader"] {
        height: 0 !important;
        min-height: 0 !important;
        background: transparent !important;
        border: 0 !important;
        box-shadow: none !important;
        z-index: 0 !important;
    }
    [data-testid="stHeader"] > div {
        display: none !important;
    }
    /* Fallback selectors for Streamlit Cloud header action controls. */
    header button, header a, header [role="button"] {
        display: none !important;
    }

    /* Dashboard buttons: dark background + white text, including Logout. */
    div[data-testid="stButton"] > button {
        background: #172033 !important;
        color: #ffffff !important;
        border: 1px solid rgba(148,163,184,.30) !important;
        box-shadow: 0 4px 12px rgba(0,0,0,.18) !important;
    }
    div[data-testid="stButton"] > button:hover {
        background: #22304a !important;
        color: #ffffff !important;
        border-color: rgba(96,165,250,.55) !important;
    }
    div[data-testid="stButton"] > button:focus,
    div[data-testid="stButton"] > button:focus-visible {
        color: #ffffff !important;
        outline: 2px solid rgba(96,165,250,.45) !important;
        outline-offset: 1px !important;
    }
    /* Give the dashboard's top row enough clearance below Streamlit header. */
    [data-testid="stAppViewContainer"] .main .block-container {
        padding-top: 3.5rem !important;
    }

    @media (max-width: 900px) {
        .state-title { font-size: 30px; }
        h1 { font-size: 1.65rem !important; }
    }
    </style>
    """, unsafe_allow_html=True)

    c1, c2 = st.columns([5, 1])
    with c1:
        st.title("Trade Easy — Index Trading Confirmation")
        st.caption("PDF specification based confirmation + risk-control dashboard")
    with c2:
        if st.button("Logout", use_container_width=True):
            try:
                supabase.auth.sign_out()
            except Exception:
                pass
            clear_oauth_params()
            st.rerun()

    st.info(
        f"Workspace: **{workspace.get('workspace_name', 'Trading Workspace')}**  "
        f"| User: **{getattr(user, 'email', '')}**"
    )

    user_subscription = st.session_state.get("trade_easy_subscription")
    if user_subscription and SUBSCRIPTION_ENFORCEMENT:
        _subscription_card(user_subscription)

    # ------------------------------------------------------------
    # ALWAYS-VISIBLE USER DASHBOARD SHELL
    # This block is intentionally rendered outside the live-data fragments so
    # a missing/late broker session can never leave the page visually blank.
    # Live/last-known values are populated by the dynamic dashboard below.
    # ------------------------------------------------------------
    # ----------------------------------------------------------------
    # SINGLE PERMANENT STRATEGY ROOT
    # ----------------------------------------------------------------
    # This root is created BEFORE Option Chain/V2 so the full strategy section
    # has a stable place in the page. The live strategy fragment later updates
    # this same root; it never creates a second copy.
    strategy_root = st.empty()

    def _render_strategy_placeholder():
        with strategy_root.container():
            st.markdown('<div class="section-head">Trade Easy Strategy Dashboard</div>', unsafe_allow_html=True)
            st.caption("Dashboard हमेशा visible रहेगा • live/completed-candle values उपलब्ध होने पर इसी section में भरेंगी")
            q1, q2, q3, q4, q5 = st.columns(5)
            q1.metric("Last Signal", "WAITING")
            q2.metric("Direction", "—")
            q3.metric("Score", "—")
            q4.metric("HTF Bias", "—")
            q5.metric("Structure", "—")

            st.markdown('<div class="section-head">Entry Confirmation</div>', unsafe_allow_html=True)
            q1, q2, q3, q4 = st.columns(4)
            q1.metric("Entry Score", "—")
            q2.metric("PA Confirmations", "—")
            q3.metric("5/8 EMA", "WAITING")
            q4.metric("Entry Alert", "—")

            st.markdown('<div class="section-head">5/8 EMA Momentum Filter</div>', unsafe_allow_html=True)
            q1, q2, q3, q4, q5 = st.columns(5)
            q1.metric("EMA 5", "—")
            q2.metric("EMA 8", "—")
            q3.metric("Spread", "—")
            q4.metric("Trend", "WAITING")
            q5.metric("Cross", "—")

            st.markdown('<div class="section-head">Swing / Level Engine</div>', unsafe_allow_html=True)
            q1, q2, q3, q4, q5 = st.columns(5)
            q1.metric("Trend", "—")
            q2.metric("Setup", "WAITING")
            q3.metric("Type", "—")
            q4.metric("Clear Path", "—")
            q5.metric("50-Point Swing", "WAITING")

            st.markdown('<div class="section-head">Market Snapshot</div>', unsafe_allow_html=True)
            q1, q2, q3, q4, q5, q6 = st.columns(6)
            q1.metric("Score", "—")
            q2.metric("HTF Bias", "—")
            q3.metric("Structure", "—")
            q4.metric("RSI", "—")
            q5.metric("ATR", "—")
            q6.metric("5/8 EMA", "WAITING")

            st.markdown('<div class="section-head">Phase-1 Risk Controls</div>', unsafe_allow_html=True)
            q1, q2, q3, q4, q5 = st.columns(5)
            q1.metric("ADX", "—")
            q2.metric("Session", "WAITING")
            q3.metric("Entry Distance", "—")
            q4.metric("Setup Age", "—")
            q5.metric("Paper Risk", "WAITING")

            st.markdown('<div class="section-head">Phase-2 Paper Execution</div>', unsafe_allow_html=True)
            q1, q2, q3, q4 = st.columns(4)
            q1.metric("Paper Position", "FLAT")
            q2.metric("Trades Today", "0/1")
            q3.metric("Realized P&L", "₹0.00")
            q4.metric("Unrealized P&L", "₹0.00")

            st.markdown('<div class="section-head">Phase-3 Monitoring & Audit</div>', unsafe_allow_html=True)
            q1, q2, q3, q4 = st.columns(4)
            q1.metric("Paper Engine", "RUNNING")
            q2.metric("Last Live Price", "—")
            q3.metric("Audit Events", "0")
            q4.metric("Paper P&L", "₹0.00")

    _render_strategy_placeholder()

    with st.sidebar:
        st.header("Configuration")
        st.caption("Live market data is managed in the background. Broker credentials and connection controls are hidden from normal users.")
        index_name = st.selectbox("Trading Index", list(INDEX_SYMBOLS.keys()), index=0)
        symbol = INDEX_SYMBOLS[index_name]
        timeframe = st.selectbox("Entry timeframe", [1, 5, 15, 30], index=1)
        risk_amount = st.number_input("Allowed risk amount", min_value=0.0, value=1000.0, step=100.0)
        min_rr = st.number_input("Minimum Risk/Reward", min_value=1.0, value=1.5, step=0.1)
        max_stale = st.number_input("Max stale minutes", min_value=1, value=10, step=1)
        news_block = st.checkbox("News block", value=False)
        st.markdown("**Phase-1 Risk Controls**")
        adx_min = st.number_input("Minimum ADX (support filter)", min_value=0.0, value=15.0, step=1.0)
        max_trigger_atr = st.number_input("Max entry distance (ATR)", min_value=0.1, value=1.0, step=0.1)
        setup_expiry_candles = st.number_input("Setup expiry (candles)", min_value=1, value=3, step=1)
        expiry_day_block = st.checkbox("Block on expiry day", value=False)
        holiday_text = st.text_input("Holiday dates (YYYY-MM-DD, comma separated)", value="")
        max_trades = 1
        st.caption("🔒 One Trade Per Day: FIXED 1")
        max_open_positions = st.number_input("Max open positions (paper)", min_value=1, value=1, step=1)
        daily_loss_limit = st.number_input("Daily loss limit (paper)", min_value=0.0, value=1000.0, step=100.0)
        st.number_input("Paper Quantity (FIXED)", min_value=65, max_value=65, value=65, step=1, disabled=True, help="Paper trading quantity is permanently fixed at 65.")

        daily_pnl = float(paper_state.get("daily_realized_pnl", 0.0))
        trades_today = int(paper_state.get("trades_today", 0))
        open_positions = 1 if paper_state.get("open_position") else 0
        st.caption(f"Paper today: {trades_today}/{int(max_trades)} trades • Open: {open_positions}/{int(max_open_positions)} • Realized P&L: ₹{daily_pnl:,.2f}")
        st.caption(f"📌 Paper quantity: FIXED {PAPER_FIXED_QUANTITY} (always)")
        if paper_state.get("open_position"):
            pp = paper_state["open_position"]
            st.info(
                f"🟢 PAPER {pp.get('signal')} OPEN • {pp.get('symbol')} • "
                f"Entry ₹{float(pp.get('entry_price', 0)):,.2f} • Qty {int(pp.get('quantity', 0))}"
            )

        st.divider()
        st.caption("Execution is paper/analysis only. Live broker controls are not exposed in the user dashboard.")


    # ================================================================
    # TOP LIVE LAYER — PRICE-ONLY LIVE CELL
    # Static labels/layout are created once. The fragment updates only the
    # numeric cells that actually need a new value. Outside market hours there
    # is no broker refresh loop; the last-known display stays quiet.
    # ================================================================
    live_ticker_root = st.container()
    with live_ticker_root:
        live_cols = st.columns([1.35, 1.0, 0.9, 0.9, 0.85, 1.0])
        with live_cols[0]:
            st.caption("LIVE PRICE")
            live_price_box = st.empty()
        with live_cols[1]:
            st.caption("PAPER P&L")
            live_pnl_box = st.empty()
        with live_cols[2]:
            st.caption("POSITION")
            live_position_box = st.empty()
        with live_cols[3]:
            st.caption("ENTRY")
            live_entry_box = st.empty()
        with live_cols[4]:
            st.caption("MARKET")
            live_market_box = st.empty()
        with live_cols[5]:
            st.caption("INDIA VIX")
            live_vix_box = st.empty()

    def _render_paper_cells(force=False):
        cache = st.session_state.setdefault("trade_easy_paper_display", {})
        version = int(cache.get("version", 0))
        if not force and version == int(st.session_state.get("trade_easy_paper_rendered_version", -1)):
            return
        pos = cache.get("open_position")
        realized = float(cache.get("daily_realized_pnl", 0.0))
        unreal = float(cache.get("unrealized_pnl", 0.0))
        live_pnl_box.markdown(f"<div style='font-size:1.05rem;font-weight:750'>₹{realized + unreal:,.2f}</div>", unsafe_allow_html=True)
        live_position_box.markdown(f"<div style='font-size:1.05rem;font-weight:750'>{pos.get('direction','FLAT') if pos else 'FLAT'}</div>", unsafe_allow_html=True)
        live_entry_box.markdown(
            f"<div style='font-size:1.05rem;font-weight:750'>₹{float(pos.get('entry_price')):,.2f}</div>"
            if pos else "<div style='font-size:1.05rem;font-weight:750'>—</div>",
            unsafe_allow_html=True,
        )
        st.session_state["trade_easy_paper_rendered_version"] = version

    def _set_market_cell(label):
        if label != st.session_state.get("trade_easy_market_label_rendered"):
            live_market_box.markdown(f"<div style='font-size:.98rem;font-weight:750'>{label}</div>", unsafe_allow_html=True)
            st.session_state["trade_easy_market_label_rendered"] = label

    def _render_live_ticker():
        market_live, session_label = india_market_status()
        token = st.session_state.get("fyers_access_token")
        appid = st.session_state.get("fyers_app_id", "").strip()
        sym = st.session_state.get("fyers_symbol", symbol)
        _render_paper_cells(force=not st.session_state.get("trade_easy_paper_rendered_once", False))
        st.session_state["trade_easy_paper_rendered_once"] = True

        # Closed/pre-market: one optional snapshot after app/session start, then NO
        # quote/VIX refreshes. The UI remains unchanged until the next live session.
        if not market_live:
            _set_market_cell("MARKET CLOSED" if session_label == "MARKET CLOSED" else session_label)
            if not st.session_state.get("trade_easy_closed_price_initialized"):
                closed_px = st.session_state.get("trade_easy_live_price_cached")
                if closed_px is None and token and appid:
                    try:
                        qpx, _ = fyers_fetch_quote(token, appid, sym)
                        if qpx is not None:
                            closed_px = float(qpx)
                            st.session_state["trade_easy_live_price_cached"] = closed_px
                    except Exception:
                        pass
                if closed_px is not None:
                    live_price_box.markdown(f"<div style='font-size:1.45rem;font-weight:800;line-height:1.1'>₹{float(closed_px):,.2f}</div>", unsafe_allow_html=True)
                elif not st.session_state.get("trade_easy_live_price_rendered"):
                    live_price_box.markdown("<div style='font-size:1.45rem;font-weight:800'>—</div>", unsafe_allow_html=True)
                st.session_state["trade_easy_closed_price_initialized"] = True
            return

        # Live session starts: unlock one live refresh loop.
        st.session_state["trade_easy_closed_price_initialized"] = False
        _set_market_cell("LIVE")

        if not token or not appid:
            return

        live = fyers_ltp_feed(token, appid, sym)
        px = parse_live_price(live.get("latest"))
        if px is None:
            px = live.get("last_tick_price")
        tick_received = live.get("last_tick_received")
        tick_age = (time.time() - float(tick_received)) if tick_received else None

        # Quote fallback is allowed ONLY during live market hours.
        if px is None or tick_age is None or tick_age > 1.0:
            last_quote_at = float(st.session_state.get("trade_easy_ticker_quote_at", 0.0))
            if time.time() - last_quote_at >= 1.0:
                qpx, qerr = fyers_fetch_quote(token, appid, sym)
                st.session_state["trade_easy_ticker_quote_at"] = time.time()
                st.session_state["trade_easy_ticker_quote_error"] = qerr
                if qpx is not None:
                    st.session_state["trade_easy_ticker_quote_price"] = float(qpx)
                    px = float(qpx)
            else:
                px = st.session_state.get("trade_easy_ticker_quote_price", px)

        if px is not None:
            px = float(px)
            old_px = st.session_state.get("trade_easy_live_price_cached")
            if old_px is None or abs(float(old_px) - px) >= 0.001:
                live_price_box.markdown(f"<div style='font-size:1.45rem;font-weight:800;line-height:1.1'>₹{px:,.2f}</div>", unsafe_allow_html=True)
                st.session_state["trade_easy_live_price_cached"] = px
                st.session_state["trade_easy_live_price_rendered"] = True

        # P&L changes only when a paper position exists. Once flat, it stays static.
        cache = st.session_state.get("trade_easy_paper_display", {})
        pos = cache.get("open_position")
        if pos and px is not None:
            px = float(px)
            sl = float(pos.get("stop_loss", 0))
            target = float(pos.get("target", 0))
            hit = None
            if pos.get("direction") == "LONG":
                if px <= sl:
                    hit = "STOP_LOSS"
                elif px >= target:
                    hit = "TARGET"
            else:
                if px >= sl:
                    hit = "STOP_LOSS"
                elif px <= target:
                    hit = "TARGET"

            if hit:
                ps = load_paper_state(paper_user_id, paper_workspace_id)
                closed = paper_close_position(ps, px, hit)
                if closed:
                    save_paper_state(paper_user_id, paper_workspace_id, ps)
                    cache["daily_realized_pnl"] = float(ps.get("daily_realized_pnl", 0.0))
                    cache["trades_today"] = int(ps.get("trades_today", 0))
                    cache["open_position"] = None
                    cache["unrealized_pnl"] = 0.0
                    cache["version"] = int(cache.get("version", 0)) + 1
                    st.session_state["trade_easy_paper_display_version"] = cache["version"]
                    _render_paper_cells(force=True)
                return

            unreal = paper_unrealized_pnl(pos, px)
            if abs(float(unreal) - float(cache.get("unrealized_pnl", 0.0))) >= 0.01:
                cache["unrealized_pnl"] = float(unreal)
                cache["version"] = int(cache.get("version", 0)) + 1
                st.session_state["trade_easy_paper_display_version"] = cache["version"]
                _render_paper_cells(force=True)

        # VIX is intentionally silent after the cash market closes.
        vix, vix_trend, _ = india_vix_snapshot(token, appid, min_interval=5.0)
        if vix is not None and abs(float(vix) - float(st.session_state.get("trade_easy_vix_display", vix))) >= 0.01:
            live_vix_box.markdown(
                f"<div style='font-size:1.05rem;font-weight:750'>{float(vix):.2f}</div><div style='font-size:.7rem;opacity:.7'>LIVE • {vix_trend}</div>",
                unsafe_allow_html=True,
            )
            st.session_state["trade_easy_vix_display"] = float(vix)

    if hasattr(st, "fragment"):
        _live_fragment = st.fragment(run_every=0.25, key="trade_easy_live_ticker")(_render_live_ticker)
        _live_fragment()
    else:
        _render_live_ticker()

    # Option Chain: live numbers update only while the cash market is LIVE.
    # After market close the last snapshot remains completely static.
    option_chain_root = st.empty()
    with option_chain_root.container():
        st.markdown('<div class="section-head">📊 Option Chain</div>', unsafe_allow_html=True)
        _oc_shell = st.columns(6)
        for _col, _label in zip(_oc_shell, ["ATM / Spot", "PCR", "Max Pain", "CALL OI", "PUT OI", "OI Change"]):
            _col.metric(_label, "—")
        st.caption("WAITING FOR LIVE OPTION-CHAIN DATA")

    def _option_chain_fingerprint(df, live_px):
        if df is None or df.empty:
            return "EMPTY"
        cols = [c for c in ["strike", "type", "ltp", "oi", "oi_change", "delta", "theta", "volume"] if c in df.columns]
        temp = df[cols].copy().sort_values([c for c in ["strike", "type"] if c in cols]).reset_index(drop=True)
        try:
            raw = pd.util.hash_pandas_object(temp, index=False).values.tobytes()
        except Exception:
            raw = repr(temp.to_dict("records")).encode()
        return hashlib.sha256(raw).hexdigest()

    @st.fragment(run_every="1.5s", key="trade_easy_option_chain")
    def _render_live_option_chain():
        market_live, session_label = india_market_status()
        token = st.session_state.get("fyers_access_token")
        appid = st.session_state.get("fyers_app_id", "").strip()
        if not token or not appid:
            return

        if not market_live:
            # One snapshot at most after a fresh app session; then remain quiet.
            if st.session_state.get("trade_easy_closed_chain_initialized"):
                return
            cached_df = st.session_state.get("trade_easy_option_chain_cache_by_symbol", {}).get(symbol)
            chain_df, chain_err = cached_df, None
            if chain_df is None or chain_df.empty:
                try:
                    chain_df, chain_err = option_chain_snapshot(token, appid, symbol, min_interval=5.0)
                except Exception as exc:
                    chain_err = str(exc)
            if chain_df is not None and not chain_df.empty:
                chain_live_price = st.session_state.get("trade_easy_live_price_cached")
                with option_chain_root:
                    render_professional_option_chain(chain_df, chain_live_price, index_name, strike_count=10)
                    st.caption("⚪ Market closed • option-chain snapshot is static")
            elif chain_err:
                with option_chain_root:
                    st.caption(f"Option Chain unavailable: {chain_err}")
            st.session_state["trade_easy_closed_chain_initialized"] = True
            return

        st.session_state["trade_easy_closed_chain_initialized"] = False
        live_now = fyers_live_feed(token, appid, symbol)
        chain_live_price = parse_live_price(live_now.get("latest")) or live_now.get("last_tick_price")
        if chain_live_price is None:
            chain_live_price = st.session_state.get("trade_easy_live_price_cached")

        chain_df, chain_err = option_chain_snapshot(token, appid, symbol, min_interval=5.0)
        option_ticks = fyers_option_live_feed(token, appid, chain_df)
        chain_df = merge_option_ticks(chain_df, option_ticks)
        if chain_df is None or chain_df.empty:
            return

        fp = _option_chain_fingerprint(chain_df, chain_live_price)
        if fp == st.session_state.get("trade_easy_option_display_fingerprint"):
            return
        st.session_state["trade_easy_option_display_fingerprint"] = fp

        with option_chain_root:
            render_professional_option_chain(chain_df, chain_live_price, index_name, strike_count=10)
            st.caption("🟢 Option Chain LIVE • values redraw only when displayed numbers change")
            if chain_err:
                st.caption(f"Last refresh warning: {chain_err} • showing last good data")

    _render_live_option_chain()

    # Permanent Paper Trading control root. This is intentionally outside every
    # live fragment and every replaceable strategy root. It therefore remains
    # visible on WAITING, market-closed, no-data and live states alike.
    paper_control_root = st.empty()
    with paper_control_root.container():
        st.markdown('<div class="section-head">🧹 Paper Trading Controls</div>', unsafe_allow_html=True)
        reset_col, reset_info = st.columns([1.20, 3.80])
        with reset_col:
            if st.button(
                "🧹 RESET PAPER TRADING",
                use_container_width=True,
                type="secondary",
                key="reset_paper_trading_permanent",
                help="Reset only paper-trading state: today's trades, P&L, open paper position and audit history. FYERS/live data and strategy settings are unchanged.",
            ):
                paper_state["date"] = pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d")
                paper_state["daily_realized_pnl"] = 0.0
                paper_state["trades_today"] = 0
                paper_state["open_position"] = None
                paper_state["last_entry_signature"] = None
                paper_state["trade_history"] = []
                paper_state["audit_log"] = []
                paper_state["paper_kill_switch"] = False
                save_paper_state(paper_user_id, paper_workspace_id, paper_state)
                st.session_state.pop("trade_easy_last_paper_event", None)
                st.session_state.pop("trade_easy_last_entry", None)
                st.session_state["trade_easy_paper_display_version"] = int(st.session_state.get("trade_easy_paper_display_version", 0)) + 1
                st.session_state["paper_reset_notice"] = True
                st.rerun()
        with reset_info:
            current_position = "OPEN" if paper_state.get("open_position") else "FLAT"
            current_trades = int(paper_state.get("trades_today", 0))
            current_pnl = float(paper_state.get("daily_realized_pnl", 0.0))
            st.caption(
                "Reset केवल Paper Trading को साफ करता है — Live/FYERS data, strategy settings और fixed Qty 65 सुरक्षित रहते हैं। "
                f"Current: {current_position} • Trades: {current_trades} • P&L: ₹{current_pnl:,.2f}"
            )
        if st.session_state.pop("paper_reset_notice", False):
            st.success("✅ Paper Trading reset हो गया — P&L ₹0, trades 0, open position साफ।")

    # ------------------------------------------------------------
    # TRADE FINDER V2 — PERMANENT RENDER ROOT
    # The V2 panel must never depend on the strategy fragment. This is critical
    # during market-close/WAIT states where the fragment intentionally returns
    # early. Live values are still updated from the same engine when available.
    # ------------------------------------------------------------
    v2_root = st.empty()
    with v2_root.container():
        _v2_cached_result = st.session_state.get("trade_easy_v2_result") or {}
        _v2_cached_tfs = st.session_state.get("trade_easy_v2_timeframes") or {15:{}, 30:{}, 60:{}}
        _v2_cached_oi = st.session_state.get("trade_easy_v2_oi_history") or {}
        render_trade_finder_v2(_v2_cached_result, _v2_cached_tfs, _v2_cached_oi)

    # Permanent dashboard shell. Cards are always visible and are populated with
    # the last completed-candle / last-known strategy snapshot whenever available.
    # This shell is updated only when the strategy snapshot itself changes; live
    # price has its own tiny ticker fragment and does not repaint these cards.
    # Separate render roots are mandatory: permanent cards must never be
    # replaced/cleared by the detailed live strategy output.
    # strategy_root was created above the Option Chain and is the single stable
    # location for all strategy cards. Never create another strategy root here.
    strategy_detail_root = st.empty()
    with strategy_detail_root.container():
        st.markdown('<div class="section-head">🧩 Detailed Logic / Confirmation Output</div>', unsafe_allow_html=True)
        st.caption("यह पूरा decision layer हमेशा दिखाई देगा। Live/completed-candle data उपलब्ध होने पर values इसी स्थान पर अपडेट होंगी।")
        d1, d2, d3, d4 = st.columns(4)
        d1.metric("Logic State", "WAITING")
        d2.metric("Entry", "—")
        d3.metric("Stop Loss", "—")
        d4.metric("Target", "—")

        st.markdown('<div class="section-head">🗺️ Paper Entry / Exit Map</div>', unsafe_allow_html=True)
        p1, p2, p3, p4, p5 = st.columns(5)
        p1.metric("Entry Price", "—")
        p2.metric("Live Price", "—")
        p3.metric("Stop Loss", "—")
        p4.metric("Target", "—")
        p5.metric("Quantity", PAPER_FIXED_QUANTITY)
        st.caption("WAITING — validated direction/setup के बाद entry, SL और target भरेंगे।")

        st.markdown('<div class="section-head">📣 Signal Output</div>', unsafe_allow_html=True)
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Direction", "—")
        s2.metric("Signal", "WAITING")
        s3.metric("Risk / Reward", "—")
        s4.metric("Quantity", PAPER_FIXED_QUANTITY)

        st.markdown('<div class="section-head">🔎 Mandatory Checks</div>', unsafe_allow_html=True)
        check_shell = pd.DataFrame([
            {"Check": "Data validation", "Status": "WAITING"},
            {"Check": "Completed candle", "Status": "WAITING"},
            {"Check": "Stale-data check", "Status": "WAITING"},
            {"Check": "Risk checks", "Status": "WAITING"},
            {"Check": "Minimum RR", "Status": "WAITING"},
            {"Check": "5/8 EMA confirmation", "Status": "WAITING"},
            {"Check": "Duplicate protection", "Status": "WAITING"},
        ])
        st.dataframe(check_shell, use_container_width=True, hide_index=True)

        st.markdown('<div class="section-head">🧱 Key Levels</div>', unsafe_allow_html=True)
        level_shell = pd.DataFrame([
            {"Level": x, "Value": "—"}
            for x in ["Previous Day High", "Previous Day Low", "VWAP", "Swing High", "Swing Low", "Support", "Resistance"]
        ])
        st.dataframe(level_shell, use_container_width=True, hide_index=True)

        st.markdown('<div class="section-head">📝 Reasons / Invalidations</div>', unsafe_allow_html=True)
        r1, r2 = st.columns(2)
        r1.info("WAITING FOR MARKET DATA — reason codes will appear here.")
        r2.info("WAITING FOR MARKET DATA — invalidations/blocks will appear here.")

        st.markdown('<div class="section-head">🕯️ Validated Candle Data</div>', unsafe_allow_html=True)
        candle_shell = pd.DataFrame([{
            "Timestamp":"—", "Open":"—", "High":"—", "Low":"—", "Close":"—",
            "Volume":"—", "VWAP":"—", "EMA 5":"—", "EMA 8":"—",
            "RSI":"—", "MACD":"—", "ATR":"—", "ADX":"—"
        }])
        st.dataframe(candle_shell, use_container_width=True, hide_index=True)

    def _render_strategy_cards(snapshot=None):
        snap = snapshot or {}
        paper = snap.get("paper", {}) or {}
        signal = snap.get("signal", "WAITING")
        direction = snap.get("direction") or "—"
        score = snap.get("score")
        bias = snap.get("bias") or "—"
        structure = snap.get("structure") or "—"
        ema_state = snap.get("ema_state", {}) or {}
        level_setup = snap.get("level_setup", {}) or {}
        path = level_setup.get("path", {}) or {}
        session_failures = snap.get("session_failures", []) or []
        strategy_confirmed = bool(snap.get("strategy_available", False))
        asof = snap.get("completed_candle", "—")
        evaluated_at = snap.get("evaluated_at", "—")
        last_label = "LAST DATA" if snapshot else "WAITING FOR DATA"
        status_text = "CONFIRMED" if strategy_confirmed else "LAST SNAPSHOT / WAITING"

        with strategy_root.container():
            st.markdown('<div class="section-head">Trade Easy Strategy Dashboard</div>', unsafe_allow_html=True)
            st.caption(
                f"Dashboard Cards / Panels हमेशा दिखाई देंगे • {last_label} • "
                f"Completed candle: {asof} • Evaluated: {evaluated_at}"
            )
            st.caption(
                f"Strategy status: **{status_text}** • Market: {snap.get('session_label', '—')} • "
                f"Last-known values stay unchanged until a new completed-candle/event update arrives."
            )

            s1, s2, s3, s4, s5 = st.columns(5)
            s1.metric("Last Signal", signal)
            s2.metric("Direction", direction)
            s3.metric("Score", f"{score}" if score is not None else "—")
            s4.metric("HTF Bias", bias)
            s5.metric("Structure", structure)

            st.markdown('<div class="section-head">Entry Confirmation</div>', unsafe_allow_html=True)
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("Entry Score", f"{score}/75" if score is not None else "—")
            s2.metric("PA Confirmations", f"{snap.get('confirmation_count', 0)}/2" if snapshot else "—")
            s3.metric("5/8 EMA", "PASS" if ema_state.get("confirmed") else ("WAIT" if snapshot else "WAITING"))
            s4.metric("Entry Alert", "YES" if snap.get("entry_alert") else ("NO" if snapshot else "—"))

            st.markdown('<div class="section-head">5/8 EMA Momentum Filter</div>', unsafe_allow_html=True)
            s1, s2, s3, s4, s5 = st.columns(5)
            s1.metric("EMA 5", f"{snap['ema_5']:,.2f}" if snap.get("ema_5") is not None else "—")
            s2.metric("EMA 8", f"{snap['ema_8']:,.2f}" if snap.get("ema_8") is not None else "—")
            s3.metric("Spread", f"{snap['ema_spread']:+,.2f}" if snap.get("ema_spread") is not None else "—")
            s4.metric("Trend", ema_state.get("trend", "—"))
            s5.metric("Cross", ema_state.get("cross", "—"))

            st.markdown('<div class="section-head">Swing / Level Engine</div>', unsafe_allow_html=True)
            s1, s2, s3, s4, s5 = st.columns(5)
            s1.metric("Trend", bias)
            s2.metric("Setup", level_setup.get("status", "WAITING") if snapshot else "WAITING")
            s3.metric("Type", level_setup.get("trade_type", "—"))
            clear_pts = path.get("clear_path")
            raw_pts = path.get("raw_distance")
            s4.metric("Clear Path", f"{clear_pts:.1f} pts" if isinstance(clear_pts, (int, float)) and np.isfinite(clear_pts) else "—")
            swing50 = bool(snap.get("clear_path_50_pass", False)) if snapshot else False
            s5.metric("50-Point Swing", "PASS" if swing50 else ("WAIT" if snapshot else "WAITING"))
            if isinstance(raw_pts, (int, float)) and np.isfinite(raw_pts):
                st.caption(f"Raw distance: {raw_pts:.1f} pts • required meaningful move: {snap.get('meaningful_move_points', '—')} pts")

            st.markdown('<div class="section-head">Market Snapshot</div>', unsafe_allow_html=True)
            s1, s2, s3, s4, s5, s6 = st.columns(6)
            s1.metric("Score", f"{score}" if score is not None else "—")
            s2.metric("HTF Bias", bias)
            s3.metric("Structure", structure)
            s4.metric("RSI", f"{snap['rsi']:.1f}" if snap.get("rsi") is not None else "—")
            s5.metric("ATR", f"{snap['atr']:.2f}" if snap.get("atr") is not None else "—")
            s6.metric("5/8 EMA", ema_state.get("trend", "—"))

            st.markdown('<div class="section-head">Phase-1 Risk Controls</div>', unsafe_allow_html=True)
            s1, s2, s3, s4, s5 = st.columns(5)
            adx_val = snap.get("adx")
            trig_atr = snap.get("trigger_distance_atr")
            setup_age = snap.get("setup_age")
            risk_ok = snap.get("risk_ok")
            s1.metric("ADX", f"{adx_val:.1f}" if adx_val is not None else "—")
            s2.metric("Session", "OPEN" if not session_failures else (snap.get("session_label", "BLOCKED")))
            s3.metric("Entry Distance", f"{trig_atr:.2f} ATR" if trig_atr is not None else "—")
            s4.metric("Setup Age", str(setup_age) if setup_age is not None else "—")
            s5.metric("Paper Risk", "PASS" if risk_ok else ("BLOCKED" if snapshot else "—"))

            st.markdown('<div class="section-head">Phase-2 Paper Execution</div>', unsafe_allow_html=True)
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("Paper Position", "OPEN" if paper.get("open_position") else "FLAT")
            s2.metric("Trades Today", str(paper.get("trades_today", 0)))
            s3.metric("Realized P&L", f"₹{float(paper.get('daily_realized_pnl', 0.0)):,.2f}")
            s4.metric("Unrealized P&L", f"₹{float(paper.get('unrealized_pnl', 0.0)):,.2f}")

            st.markdown('<div class="section-head">Phase-3 Monitoring & Audit</div>', unsafe_allow_html=True)
            st.caption("🟢 LOGIC ACTIVE — monitoring/audit remains visible continuously." if not paper.get("paper_kill_switch") else "🔴 LOGIC PAUSED — paper kill switch is active; audit remains visible.")
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("Paper Engine", "KILLED" if paper.get("paper_kill_switch") else "RUNNING")
            s2.metric("Last Live Price", f"₹{snap['live_price']:,.2f}" if snap.get("live_price") is not None else "—")
            s3.metric("Audit Events", str(paper.get("audit_events", 0)))
            total_pnl = float(paper.get("daily_realized_pnl", 0.0)) + float(paper.get("unrealized_pnl", 0.0))
            s4.metric("Paper P&L", f"₹{total_pnl:,.2f}")

            if snapshot:
                st.caption(
                    "📌 Last data source: completed candle + latest cached strategy calculation. "
                    "Live price is displayed separately and is not used to repaint these cards on every tick."
                )

    # Do not render strategy cards here: the live strategy fragment owns the
    # single strategy-card render root when a FYERS session is available.
    # Rendering the cached cards here as well caused the same dashboard blocks
    # to appear twice before the live fragment refreshed them.

    access_token = st.session_state.get("fyers_access_token")
    if not access_token:
        st.info("Live market data अभी उपलब्ध नहीं है। सभी dashboard panels ऊपर/नीचे बने रहेंगे; values live data आते ही भरेंगी।")
        return

    # Live dashboard refresh: use Streamlit fragments so only the dynamic dashboard
    # updates every second instead of rerunning/repainting the whole page.
    # This removes the visible 1-second full-page blink while keeping the FYERS
    # WebSocket/live-price and paper-monitoring updates running automatically.
    def _render_full_dashboard():
        # Invisible polling fragment. During market close it performs at most one
        # strategy evaluation; during live hours it polls for new candle/strategy
        # events without redrawing unchanged content.
        strategy_market_live, _strategy_session_label = india_market_status()
        if (not strategy_market_live
                and st.session_state.get("trade_easy_closed_strategy_initialized")):
            return
        if strategy_market_live:
            st.session_state["trade_easy_closed_strategy_initialized"] = False

        # Load paper state only at strategy/event cadence, not on every price tick.
        paper_state = load_paper_state(paper_user_id, paper_workspace_id)
        daily_pnl = float(paper_state.get("daily_realized_pnl", 0.0))
        trades_today = int(paper_state.get("trades_today", 0))
        open_positions = 1 if paper_state.get("open_position") else 0

        # Callback के बाद Streamlit browser session recreate हो सकता है।
        # उस स्थिति में Secret ID session में नहीं रहती, लेकिन FYERS access token
        # और app_id पर्याप्त हैं: market history/WebSocket को Secret ID की जरूरत नहीं।
        active_app_id = st.session_state.get("fyers_app_id", "").strip() or FYERS_CONFIG_APP_ID
        if active_app_id:
            st.session_state["fyers_app_id"] = active_app_id
    
        if not active_app_id:
            st.info("Live market connection is currently unavailable. Last available dashboard data is retained.")
            st.stop()
    
        # Completed-candle history changes only when the selected timeframe closes.
        # During market close, a single cached snapshot is enough.
        try:
            history_interval = 8.0 if strategy_market_live else 9999999.0
            raw, history_warning = fyers_history_snapshot(
                access_token, active_app_id, symbol, resolution=timeframe,
                days=5, min_interval=history_interval,
            )
            if history_warning:
                st.session_state["trade_easy_strategy_history_warning"] = history_warning
        except Exception as exc:
            st.session_state["trade_easy_strategy_error"] = str(exc)
            return
    
        # The strategy engine uses the live tick only while the market is open.
        # At market close the last cached price is reused and no quote refresh runs.
        if strategy_market_live:
            live_state = fyers_live_feed(access_token, active_app_id, symbol)
            ws_price = parse_live_price(live_state.get("latest"))
            tick_received = live_state.get("last_tick_received")
            tick_age = (time.time() - float(tick_received)) if tick_received else None
            quote_price = None
            quote_error = None
            if ws_price is None or tick_age is None or tick_age > 1.0:
                last_quote_at = float(st.session_state.get("trade_easy_last_quote_at", 0.0))
                if time.time() - last_quote_at >= 1.0:
                    quote_price, quote_error = fyers_fetch_quote(access_token, active_app_id, symbol)
                    st.session_state["trade_easy_last_quote_at"] = time.time()
                    if quote_price is not None:
                        st.session_state["trade_easy_last_quote_price"] = float(quote_price)
                else:
                    quote_price = st.session_state.get("trade_easy_last_quote_price")
            live_price = ws_price if ws_price is not None else quote_price
        else:
            live_state = {"connected": False, "tick_count": 0}
            ws_price = st.session_state.get("trade_easy_live_price_cached")
            tick_age = None
            quote_error = None
            live_price = ws_price
        if strategy_market_live:
            option_chain_df, option_chain_error = option_chain_snapshot(access_token, active_app_id, symbol, min_interval=5.0)
        else:
            option_chain_df = st.session_state.get("trade_easy_option_chain_cache_by_symbol", {}).get(symbol)
            option_chain_error = None
        option_summary = summarize_option_chain(option_chain_df)

        # ============================================================
        # TRADE FINDER ENGINE V2
        # Independent, read-only analysis layer. It never places broker orders
        # and never mutates the existing paper execution engine.
        # ============================================================
        try:
            st.session_state["trade_easy_v2_pcr"] = option_summary.get("pcr")
            option_history_v2 = option_oi_history_v2(symbol, option_chain_df)
            v2_history_cache = st.session_state.setdefault("trade_easy_v2_history_cache", {})
            v2_history_at = st.session_state.setdefault("trade_easy_v2_history_at", {})
            timeframe_results_v2 = {}
            for tf_v2 in (15, 30, 60):
                now_v2 = time.time()
                cached_v2 = v2_history_cache.get(tf_v2)
                last_v2 = float(v2_history_at.get(tf_v2, 0.0))
                # History is cached briefly so V2 cannot hammer the broker API.
                if cached_v2 is None or now_v2 - last_v2 >= (15.0 if strategy_market_live else 300.0):
                    raw_v2, warn_v2 = fyers_history_snapshot(
                        access_token, active_app_id, symbol, resolution=tf_v2, days=5,
                        min_interval=(15.0 if strategy_market_live else 300.0),
                    )
                    if raw_v2 is not None and not raw_v2.empty:
                        cached_v2 = normalize_candles(raw_v2)
                        v2_history_cache[tf_v2] = cached_v2
                        v2_history_at[tf_v2] = now_v2
                    elif warn_v2 and cached_v2 is None:
                        st.session_state.setdefault("trade_easy_v2_warnings", {})[tf_v2] = str(warn_v2)
                timeframe_results_v2[tf_v2] = _tf_evaluate_timeframe(
                    cached_v2, tf_v2, index_name, option_summary=option_summary, live_price=live_price
                )
            v2_result = trade_finder_v2(
                timeframe_results_v2, option_summary, option_history_v2,
                option_chain_df, live_price, index_name
            )
            st.session_state["trade_easy_v2_result"] = v2_result
            st.session_state["trade_easy_v2_timeframes"] = timeframe_results_v2
            st.session_state["trade_easy_v2_oi_history"] = option_history_v2
            # Update the permanent V2 root; never let the strategy fragment own
            # the lifetime of this panel.
            with v2_root:
                render_trade_finder_v2(v2_result, timeframe_results_v2, option_history_v2)
        except Exception as v2_exc:
            # V2 must never take down the main dashboard. Keep the panel visible.
            st.session_state["trade_easy_v2_error"] = str(v2_exc)
            with v2_root:
                render_trade_finder_v2(
                    st.session_state.get("trade_easy_v2_result") or {},
                    st.session_state.get("trade_easy_v2_timeframes") or {15:{}, 30:{}, 60:{}},
                    st.session_state.get("trade_easy_v2_oi_history") or {},
                )
                st.warning(f"Trade Finder V2 अभी WAIT mode में है: {v2_exc}")

        if not strategy_market_live:
            st.session_state["trade_easy_closed_strategy_initialized"] = True

        # Sidebar remains static. Updating an outside sidebar from a fragment
        # causes layout-context errors and adds unnecessary repainting.
        ws_status = "CONNECTED" if live_state.get("connected") else "RECONNECTING"
        st.session_state["trade_easy_ws_status"] = ws_status
    
        # FYERS history may include the currently forming candle.
        # The PDF safety rule evaluates completed candles only.
    
        try:
            df = normalize_candles(raw)
        except Exception as e:
            st.session_state["trade_easy_strategy_error"] = str(e)
            return
    
        # Drop an in-progress candle; only completed candles enter the engine.
        if not df.empty:
            now_utc = pd.Timestamp.now(tz="UTC")
            interval = pd.Timedelta(minutes=timeframe)
            while len(df) and df["timestamp"].iloc[-1] + interval > now_utc:
                df = df.iloc[:-1].reset_index(drop=True)
    
        data_ok, data_reasons = validate_candles(df, timeframe, max_stale)
        if df.empty or len(df) < 30:
            st.session_state["trade_easy_strategy_available"] = False
            st.session_state["trade_easy_strategy_waiting_reason"] = "Completed candle history is not available yet."
            # Never clear strategy_root or strategy_detail_root here. Existing
            # last-known content remains visible; the permanent shell already
            # contains the WAITING state when no live/completed data exists.
            return

        df = add_indicators(df)
        levels = key_levels(df)
        pa = price_action_checks(df, levels)
        score, direction, bias, structure, reasons, invalidations = score_signal(
            df, levels, pa, timeframe
        )
    
        holiday_dates = [x.strip() for x in holiday_text.split(",") if x.strip()]
        session_status = market_session_status(expiry_block=expiry_day_block, holiday_dates=holiday_dates)
        trigger_price, trigger_distance, trigger_distance_atr = entry_trigger_and_distance(df, direction)
        setup_status = setup_expiry_status(df, direction, trigger_price, setup_expiry_candles)
        current_setup_signature = f"{symbol}|{direction}|{setup_status.get('key')}|{df['timestamp'].iloc[-1]}" if direction else None
        duplicate_entry = bool(
            current_setup_signature
            and paper_state.get("last_entry_signature") == current_setup_signature
        )
        duplicate_reason = "DUPLICATE_ENTRY" if duplicate_entry else None
        adx_value = float(df["adx"].iloc[-1]) if not df.empty and pd.notna(df["adx"].iloc[-1]) else None
        paper_risk_ok, paper_risk_failures = paper_risk_checks(
            daily_pnl, daily_loss_limit, trades_today, max_trades, open_positions, max_open_positions
        )
    
        plan = calculate_trade_plan(
            df, direction, levels, risk_amount, min_rr
        )
        risk_ok, risk_failures = mandatory_risk_checks(df, plan)
        phase1_failures = []
        # Entry/session controls affect trade eligibility, while the dashboard remains visible.
        phase1_failures.extend(session_status["failures"])
        if adx_value is not None and adx_value < float(adx_min):
            phase1_failures.append("ADX_TOO_WEAK")
        if trigger_distance_atr is not None and trigger_distance_atr > float(max_trigger_atr):
            phase1_failures.append("ENTRY_DISTANCE_TOO_LARGE")
        if setup_status.get("expired"):
            phase1_failures.append("SETUP_EXPIRED")
        if duplicate_entry and duplicate_reason:
            phase1_failures.append(duplicate_reason)
        phase1_failures.extend(paper_risk_failures)
        if phase1_failures:
            risk_ok = False
            risk_failures = list(dict.fromkeys(list(risk_failures) + phase1_failures))
    
        # Entry confirmation is intentionally stricter than direction detection.
        # Direction can be known from HTF/structure alignment, but a BUY/SELL alert
        # is allowed only after the score threshold and mandatory checks pass.
        confirmation_flags = {
            "sweep_confirmed": bool(pa.get("sweep_confirmed")),
            "structure_break": bool(pa.get("structure_break")),
            "retest_confirmed": bool(pa.get("retest_confirmed")),
            "vwap_aligned": bool(
                direction == "LONG" and df["close"].iloc[-1] > df["vwap"].iloc[-1]
                or direction == "SHORT" and df["close"].iloc[-1] < df["vwap"].iloc[-1]
            ),
            "volume_confirmed": bool(
                pd.notna(df["volume_ma"].iloc[-1])
                and df["volume"].iloc[-1] > df["volume_ma"].iloc[-1]
            ),
        }
        confirmation_count = sum(
            confirmation_flags[k] for k in ("sweep_confirmed", "structure_break", "retest_confirmed")
        )
        level_setup = level_setup_engine(
            live_price if live_price is not None else float(df["close"].iloc[-1]),
            direction, bias, levels, score, confirmation_count, option_summary,
            df=df, index_name=index_name
        )
        ema_state = ema_5_8_confirmation(df, direction)

        # Decide the trade only after the level engine and 5/8 EMA filter.
        # This prevents an UnboundLocalError and, more importantly, ensures
        # that a BUY/SELL signal is validated against the current level setup.
        signal = decide(
            score=score,
            data_ok=data_ok,
            risk_ok=risk_ok,
            direction=direction,
            news_block=news_block,
        )
        if signal in ("BUY", "SELL") and level_setup.get("status") != "CONFIRMED":
            signal = "WAIT"
            reasons.append("LEVEL_ENGINE_" + str(level_setup.get("reason", "PENDING")))
        elif signal in ("BUY", "SELL") and not ema_state.get("confirmed", False):
            signal = "WAIT"
            reasons.append("EMA_5_8_CONFIRMATION_PENDING")
        elif signal == "WAIT" and level_setup.get("status") == "NO TRADE":
            reasons.append("NO_MEANINGFUL_CLEAR_PATH")

        pending_confirmations = []
        if direction and not confirmation_flags["sweep_confirmed"]:
            pending_confirmations.append("SWEEP")
        if direction and not confirmation_flags["structure_break"]:
            pending_confirmations.append("STRUCTURE_BREAK")
        if direction and not confirmation_flags["retest_confirmed"]:
            pending_confirmations.append("RETEST")
    
        if signal == "WAIT":
            if not direction:
                reasons.append("DIRECTION_PENDING")
            elif score < 75:
                reasons.append(f"ENTRY_SCORE_PENDING_{75 - score}")
            elif confirmation_count < 2:
                reasons.append("PRICE_ACTION_CONFIRMATION_PENDING")
    
        if news_block:
            invalidations = list(invalidations) + ["NEWS_BLOCK"]
    
        if not data_ok:
            invalidations = list(invalidations) + data_reasons
    
        if not risk_ok:
            invalidations = list(invalidations) + risk_failures
    
        # ---------------- Phase 2: paper execution ----------------
        # This is deliberately paper-only: no FYERS order API is called.
        paper_state, paper_event = paper_execution_engine(
            paper_state,
            symbol=symbol,
            direction=direction,
            signal=signal,
            plan=plan,
            live_price=live_price,
            entry_alert=signal in ("BUY", "SELL"),
            risk_ok=risk_ok and not duplicate_entry,
            setup_key=setup_status.get("key"),
            candle_timestamp=df["timestamp"].iloc[-1],
            max_trades=max_trades,
            max_open_positions=max_open_positions,
            daily_loss_limit=daily_loss_limit,
        )
        save_paper_state(paper_user_id, paper_workspace_id, paper_state)
        trades_today = int(paper_state.get("trades_today", 0))
        daily_pnl = float(paper_state.get("daily_realized_pnl", 0.0))
        open_positions = 1 if paper_state.get("open_position") else 0
        paper_unrealized = paper_unrealized_pnl(paper_state.get("open_position"), live_price)
        if paper_event:
            st.session_state["trade_easy_last_paper_event"] = str(paper_event)
    
        payload = {
            "symbol": symbol,
            "direction": direction,
            "signal": signal,
            "score": score,
            "timeframe": f"{timeframe}m",
            "higher_timeframe_bias": bias,
            "entry": plan["entry"] if plan else 0,
            "stop_loss": plan["stop_loss"] if plan else 0,
            "target": plan["target"] if plan else 0,
            "risk_reward": plan["risk_reward"] if plan else 0,
            "sweep_confirmed": pa["sweep_confirmed"],
            "structure_break": pa["structure_break"],
            "retest_confirmed": pa["retest_confirmed"],
            "vwap_aligned": bool(
                (direction == "LONG" and df["close"].iloc[-1] > df["vwap"].iloc[-1])
                or (direction == "SHORT" and df["close"].iloc[-1] < df["vwap"].iloc[-1])
            ),
            "volume_confirmed": bool(
                pd.notna(df["volume_ma"].iloc[-1])
                and df["volume"].iloc[-1] > df["volume_ma"].iloc[-1]
            ),
            "news_block": news_block,
            "risk_checks_passed": risk_ok,
            "adx": adx_value,
            "session_ok": not bool(session_status["failures"]),
            "expiry_day": session_status["expiry_day"],
            "trigger_price": trigger_price,
            "entry_distance_atr": trigger_distance_atr,
            "setup_age_candles": setup_status.get("age", 0),
            "setup_expired": setup_status.get("expired", False),
            "duplicate_entry_block": duplicate_entry,
            "paper_daily_pnl": daily_pnl,
            "paper_trades_today": int(trades_today),
            "paper_open_positions": int(open_positions),
            "paper_fixed_quantity": PAPER_FIXED_QUANTITY,
            "paper_unrealized_pnl": round(float(paper_unrealized), 2),
            "paper_position": paper_state.get("open_position"),
            "confirmation_count": confirmation_count,
            "pending_confirmations": pending_confirmations,
            "ema_5": float(df["ema_5"].iloc[-1]),
            "ema_8": float(df["ema_8"].iloc[-1]),
            "ema_5_8_spread": float(df["ema_5_8_spread"].iloc[-1]),
            "ema_5_slope": float(df["ema_5_slope"].iloc[-1]),
            "ema_5_8_trend": ema_state.get("trend"),
            "ema_5_8_cross": ema_state.get("cross"),
            "ema_5_8_confirmed": bool(ema_state.get("confirmed")),
            "level_setup_status": level_setup.get("status"),
            "level_trade_type": level_setup.get("trade_type"),
            "clear_path_points": level_setup.get("path", {}).get("clear_path"),
            "raw_distance_points": level_setup.get("path", {}).get("raw_distance"),
            "first_obstacle": level_setup.get("path", {}).get("first_obstacle"),
            "first_obstacle_name": level_setup.get("path", {}).get("obstacle_name"),
            "clear_path_50_pass": bool(level_setup.get("path", {}).get("eligible", False)),
            "meaningful_move_points": level_setup.get("path", {}).get("meaningful_move"),
            "index_name": index_name,
            "option_chain_available": bool(option_summary.get("available")),
            "call_oi": option_summary.get("call_oi"),
            "put_oi": option_summary.get("put_oi"),
            "call_oi_change": option_summary.get("call_oi_change"),
            "put_oi_change": option_summary.get("put_oi_change"),
            "pcr": option_summary.get("pcr"),
            "entry_alert": signal in ("BUY", "SELL"),
            "reasons": reasons,
            "invalidations": invalidations,
        }
    
        # Confirmed strategy only: until this exists, the strategy region remains blank.
        strategy_available = bool(
            signal in ("BUY", "SELL")
            and level_setup.get("status") == "CONFIRMED"
            and ema_state.get("confirmed", False)
            and data_ok
            and risk_ok
        )
        strategy_signature = (
            f"{symbol}|{df['timestamp'].iloc[-1]}|{signal}|{direction}|"
            f"{level_setup.get('status')}|{ema_state.get('trend')}|{ema_state.get('cross')}|"
            f"{paper_event or ''}|"
            f"{paper_state.get('open_position', {}).get('id') if paper_state.get('open_position') else 'FLAT'}"
        )

        # Cache the latest completed-candle strategy snapshot. The dashboard shell
        # can therefore show useful LAST DATA even when there is no confirmed
        # BUY/SELL strategy. This cache is updated only when this strategy cycle
        # produces a new completed-candle/event signature.
        try:
            latest_completed_ts = pd.Timestamp(df["timestamp"].iloc[-1]).tz_convert("Asia/Kolkata").strftime("%Y-%m-%d %H:%M:%S IST")
        except Exception:
            latest_completed_ts = str(df["timestamp"].iloc[-1])
        try:
            evaluated_at = pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d %H:%M:%S IST")
        except Exception:
            evaluated_at = datetime.now(timezone.utc).isoformat()
        strategy_snapshot = {
            "strategy_available": strategy_available,
            "signal": signal,
            "direction": direction,
            "score": int(score),
            "bias": bias,
            "structure": structure,
            "confirmation_count": int(confirmation_count),
            "entry_alert": bool(signal in ("BUY", "SELL")),
            "ema_5": float(df["ema_5"].iloc[-1]),
            "ema_8": float(df["ema_8"].iloc[-1]),
            "ema_spread": float(df["ema_5_8_spread"].iloc[-1]),
            "ema_state": ema_state,
            "level_setup": level_setup,
            "rsi": float(df["rsi"].iloc[-1]) if pd.notna(df["rsi"].iloc[-1]) else None,
            "atr": float(df["atr"].iloc[-1]) if pd.notna(df["atr"].iloc[-1]) else None,
            "adx": adx_value,
            "trigger_distance_atr": trigger_distance_atr,
            "setup_age": setup_status.get("age", 0),
            "risk_ok": bool(risk_ok),
            "session_failures": list(session_status.get("failures", [])),
            "session_label": "LIVE" if strategy_market_live and not session_status.get("failures") else ("MARKET CLOSED" if not strategy_market_live else "BLOCKED"),
            "live_price": float(live_price) if live_price is not None else None,
            "completed_candle": latest_completed_ts,
            "evaluated_at": evaluated_at,
            "paper": {
                "open_position": paper_state.get("open_position"),
                "trades_today": int(paper_state.get("trades_today", 0)),
                "daily_realized_pnl": float(paper_state.get("daily_realized_pnl", 0.0)),
                "unrealized_pnl": float(paper_unrealized),
                "paper_kill_switch": bool(paper_state.get("paper_kill_switch")),
                "audit_events": len(paper_state.get("audit_log") or []),
            },
        }
        st.session_state["trade_easy_last_strategy_snapshot"] = strategy_snapshot

        # Keep the small live P&L cache synchronized without repainting the big dashboard.
        paper_cache = st.session_state.setdefault("trade_easy_paper_display", {})
        old_pos = paper_cache.get("open_position")
        old_pos_id = old_pos.get("id") if isinstance(old_pos, dict) else None
        new_pos = paper_state.get("open_position")
        new_pos_id = new_pos.get("id") if isinstance(new_pos, dict) else None
        state_changed = (
            old_pos_id != new_pos_id
            or float(paper_cache.get("daily_realized_pnl", 0.0)) != float(paper_state.get("daily_realized_pnl", 0.0))
            or int(paper_cache.get("trades_today", 0)) != int(paper_state.get("trades_today", 0))
        )
        version = int(paper_cache.get("version", 0)) + (1 if state_changed else 0)
        paper_cache.update({
            "daily_realized_pnl": float(paper_state.get("daily_realized_pnl", 0.0)),
            "trades_today": int(paper_state.get("trades_today", 0)),
            "open_position": new_pos,
            "unrealized_pnl": float(paper_unrealized),
            "version": version,
        })
        if state_changed:
            st.session_state["trade_easy_paper_display_version"] = version

        if strategy_signature == st.session_state.get("trade_easy_strategy_signature"):
            # No new completed-candle strategy/event: keep BOTH permanent render
            # roots untouched. Live price is handled by the tiny ticker fragment.
            return

        # New completed-candle/event snapshot: update the cards once. They remain
        # visible even when the current signal is WAIT/blocked; the cards show the
        # last evaluated data rather than disappearing.
        st.session_state["trade_easy_strategy_signature"] = strategy_signature
        st.session_state["trade_easy_strategy_visible"] = bool(strategy_available)
        _render_strategy_cards(strategy_snapshot)
        state_icon = {"BUY": "🟢", "SELL": "🔴", "WAIT": "🟡", "BLOCKED": "⛔"}[signal]

        # IMPORTANT: Detailed logic panels are PERMANENT.
        # They are rendered even when a strategy is not yet confirmed.
        # Each panel reports its own logic state (WAITING / BUILDING / READY /
        # CONFIRMED / BLOCKED) instead of disappearing until the final signal.
        with strategy_detail_root.container():
            if live_price is not None:
                st.metric("Live Price", f"{live_price:,.2f}")
                if tick_age is not None and tick_age <= 3.0:
                    st.caption(f"🟢 LIVE • WebSocket • {tick_age:.1f}s old • {int(live_state.get('tick_count', 0))} ticks")
                else:
                    st.caption("🟡 Waiting for a fresh live tick")
    
            state_message = {
                "BUY": "LONG direction confirmed — entry conditions passed.",
                "SELL": "SHORT direction confirmed — entry conditions passed.",
                "WAIT": f"{direction or 'NO'} direction detected — entry confirmation is still pending.",
                "BLOCKED": "A mandatory data, risk, or safety condition has blocked the trade candidate.",
            }[signal]
            st.markdown(
                f'<div class="state-box">'
                f'<div class="muted">FINAL CONFIRMATION STATE</div>'
                f'<div class="state-title">{state_icon} {signal}</div>'
                f'<div class="state-sub">{state_message}</div>'
                f'<div class="state-sub" style="margin-top:8px;">Score: {score}/100 &nbsp; • &nbsp; HTF Bias: {bias} &nbsp; • &nbsp; Structure: {structure}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
    
            if signal == "BLOCKED":
                st.error("Trade blocked because a mandatory data/risk/order condition failed.")
            elif signal == "WAIT":
                if level_setup.get("status") == "NO TRADE":
                    st.warning(f"WAIT: {direction or 'NO'} setup does not have the required clear path. No chase entry.")
                elif direction and score < 75:
                    st.warning(f"WAIT: {direction} direction detected, but entry score is {score}/75. More confirmation is required.")
                elif direction and confirmation_count < 2:
                    st.warning(f"WAIT: {direction} direction detected. Price-action confirmation is still pending ({confirmation_count}/2).")
                else:
                    st.warning("WAIT: confirmation is below the trade-candidate threshold or requires further confirmation.")
            elif signal in ("BUY", "SELL"):
                st.success(f"{signal} candidate: score ≥ 75 and mandatory risk checks passed.")
    
            st.markdown('<div class="section-head">Entry Confirmation</div>', unsafe_allow_html=True)
            st.caption("🟢 LOGIC COMPLETE" if signal in ("BUY", "SELL") and risk_ok and ema_state.get("confirmed") and level_setup.get("status") == "CONFIRMED" else "🟡 LOGIC PENDING — conditions will appear here as they are satisfied.")
            ec1, ec2, ec3, ec4 = st.columns(4)
            ec1.metric("Entry Score", f"{score}/75")
            ec2.metric("PA Confirmations", f"{confirmation_count}/2")
            ec3.metric("5/8 EMA", "PASS" if ema_state.get("confirmed") else "WAIT")
            ec4.metric("Entry Alert", "YES" if signal in ("BUY", "SELL") else "NO")

            st.markdown('<div class="section-head">5/8 EMA Momentum Filter</div>', unsafe_allow_html=True)
            st.caption("🟢 LOGIC COMPLETE — EMA 5/8 confirmation passed." if ema_state.get("confirmed") else "🟡 LOGIC PENDING — waiting for direction + EMA 5/8 confirmation.")
            em1, em2, em3, em4, em5 = st.columns(5)
            em1.metric("EMA 5", f"{float(df['ema_5'].iloc[-1]):,.2f}")
            em2.metric("EMA 8", f"{float(df['ema_8'].iloc[-1]):,.2f}")
            em3.metric("Spread", f"{float(df['ema_5_8_spread'].iloc[-1]):+,.2f}")
            em4.metric("Trend", ema_state.get("trend", "—"))
            em5.metric("Cross", ema_state.get("cross", "NONE"))
            if ema_state.get("confirmed"):
                st.success("🟢 5/8 EMA confirms the current direction.")
            elif direction:
                st.warning(f"🟡 5/8 EMA pending for {direction}: {ema_state.get('reason', 'PENDING')}")
            else:
                st.caption("5/8 EMA waiting for a valid LONG/SHORT direction.")
            st.caption(f"EMA5 slope: {float(df['ema_5_slope'].iloc[-1]):+,.3f} • Price: {ema_state.get('price_position', '—')}")
            pending_all = list(pending_confirmations)
            if direction and not ema_state.get("confirmed", False):
                pending_all.append("EMA_5_8")
            if pending_all and signal == "WAIT":
                st.markdown(
                    '<div class="pending-box">Pending confirmations: ' +
                    ' &nbsp; • &nbsp; '.join(pending_confirmations) +
                    '</div>',
                    unsafe_allow_html=True,
                )
    
            st.markdown('<div class="section-head">Swing / Level Engine</div>', unsafe_allow_html=True)
            st.caption("🟢 LOGIC COMPLETE — level setup confirmed." if level_setup.get("status") == "CONFIRMED" else ("🔴 LOGIC BLOCKED — no qualifying path/setup." if level_setup.get("status") == "NO TRADE" else "🟡 LOGIC BUILDING — waiting for level/path confirmations."))
            lc1, lc2, lc3, lc4, lc5 = st.columns(5)
            lc1.metric("Trend", bias or "—")
            lc2.metric("Setup", level_setup.get("status", "—"))
            lc3.metric("Type", level_setup.get("trade_type", "—"))
            clear_pts = level_setup.get("path", {}).get("clear_path")
            meaningful_pts = level_setup.get("path", {}).get("meaningful_move")
            lc4.metric("Clear Path", f"{clear_pts:.1f} pts" if clear_pts is not None and np.isfinite(clear_pts) else "—")
            raw_pts = level_setup.get("path", {}).get("raw_distance")
            lc5.metric("Raw Distance", f"{raw_pts:.1f} pts" if raw_pts is not None and np.isfinite(raw_pts) else "—")
            obstacle = level_setup.get("path", {}).get("obstacle_name", "NONE")
            obstacle_price = level_setup.get("path", {}).get("first_obstacle")
            obstacle_text = f"{obstacle} @ {obstacle_price:.2f}" if obstacle_price is not None and np.isfinite(obstacle_price) else obstacle
            clear_path_text = f"{float(clear_pts):.1f}" if clear_pts is not None and np.isfinite(clear_pts) else "—"
            meaningful_text = f"{float(meaningful_pts):.1f}" if meaningful_pts is not None and np.isfinite(meaningful_pts) else "—"
            if level_setup.get("status") == "CONFIRMED":
                st.success(f"🟢 ENTRY CONFIRMED • {direction or 'NO DIRECTION'} • {level_setup.get('trade_type')} • Clear Path {clear_path_text} pts • Required {meaningful_text} pts • First obstacle: {obstacle_text}")
            elif level_setup.get("status") == "NO TRADE":
                st.error(f"🔴 NO TRADE • {level_setup.get('reason')} • Clear Path {clear_path_text} pts • Required {meaningful_text} pts • First obstacle: {obstacle_text}")
            else:
                st.warning(f"🟡 {level_setup.get('status')} • {direction or 'NO DIRECTION'} • {level_setup.get('trade_type')} • Clear Path {clear_path_text} pts • Required {meaningful_text} pts • {level_setup.get('reason')}")
            if option_summary.get("available"):
                oc1, oc2, oc3, oc4, oc5 = st.columns(5)
                oc1.metric("CALL OI", f"{option_summary['call_oi']:,.0f}")
                oc2.metric("PUT OI", f"{option_summary['put_oi']:,.0f}")
                oc3.metric("CALL OI Chg", f"{option_summary['call_oi_change']:,.0f}")
                oc4.metric("PUT OI Chg", f"{option_summary['put_oi_change']:,.0f}")
                oc5.metric("PCR", f"{option_summary['pcr']:.2f}" if np.isfinite(option_summary['pcr']) else "—")
            else:
                st.caption(f"OI/OI Change: {option_chain_error or 'Option-chain data not available'} — no invented OI values.")
            st.markdown('<div class="section-head">Paper Entry / Exit Map</div>', unsafe_allow_html=True)
            if plan:
                pc1, pc2, pc3, pc4, pc5 = st.columns(5)
                pc1.metric("Entry Price", f"₹{plan['entry']:,.2f}")
                pc2.metric("Live Price", f"₹{live_price:,.2f}" if live_price is not None else "—")
                pc3.metric("Stop Loss", f"₹{plan['stop_loss']:,.2f}")
                pc4.metric("Target", f"₹{plan['target']:,.2f}")
                pc5.metric("Quantity", PAPER_FIXED_QUANTITY)
                st.success("🟢 ENTRY/EXIT LOGIC READY — trade plan calculated from the current validated setup.")
            else:
                pc1, pc2, pc3, pc4, pc5 = st.columns(5)
                pc1.metric("Entry Price", "—")
                pc2.metric("Live Price", f"₹{live_price:,.2f}" if live_price is not None else "—")
                pc3.metric("Stop Loss", "—")
                pc4.metric("Target", "—")
                pc5.metric("Quantity", PAPER_FIXED_QUANTITY)
                st.warning("🟡 ENTRY/EXIT LOGIC PENDING — a valid direction/setup is required before prices are calculated.")

            st.markdown('<div class="section-head">Market Snapshot</div>', unsafe_allow_html=True)
            m1, m2, m3, m4, m5, m6 = st.columns(6)
            m1.metric("Score", score)
            m2.metric("HTF Bias", bias)
            m3.metric("Structure", structure)
            m4.metric("RSI", f"{df['rsi'].iloc[-1]:.1f}" if pd.notna(df["rsi"].iloc[-1]) else "—")
            m5.metric("ATR", f"{df['atr'].iloc[-1]:.2f}" if pd.notna(df["atr"].iloc[-1]) else "—")
            m6.metric("5/8 EMA", ema_state.get("trend", "—"))
            st.markdown('<div class="section-head">Phase-1 Risk Controls</div>', unsafe_allow_html=True)
            st.caption("🟢 LOGIC COMPLETE — mandatory risk/session checks passed." if risk_ok and not phase1_failures else "🔴 LOGIC BLOCKED — one or more mandatory risk/session checks failed or are pending.")
            rc1, rc2, rc3, rc4, rc5 = st.columns(5)
            rc1.metric("ADX", f"{adx_value:.1f}" if adx_value is not None else "—", "PASS" if adx_value is not None and adx_value >= float(adx_min) else "WEAK")
            rc2.metric("Session", "OPEN" if not session_status["failures"] else "BLOCKED")
            rc3.metric("Entry Distance", f"{trigger_distance_atr:.2f} ATR" if trigger_distance_atr is not None else "—")
            rc4.metric("Setup Age", f"{setup_status.get('age', 0)}/{setup_expiry_candles}")
            rc5.metric("Paper Risk", "PASS" if paper_risk_ok else "BLOCKED")
            if phase1_failures:
                st.markdown('<div class="pending-box">Phase-1 blocks: ' + ' &nbsp; • &nbsp; '.join(phase1_failures) + '</div>', unsafe_allow_html=True)
    
            st.markdown('<div class="section-head">Phase-2 Paper Execution</div>', unsafe_allow_html=True)
            st.caption("🟢 LOGIC ACTIVE — paper execution engine is ready to act only after the final entry alert." if not paper_state.get("paper_kill_switch") else "🔴 LOGIC BLOCKED — paper kill switch is active.")
            pe1, pe2, pe3, pe4 = st.columns(4)
            pe1.metric("Paper Position", "OPEN" if paper_state.get("open_position") else "FLAT")
            pe2.metric("Trades Today", f"{trades_today}/{int(max_trades)}")
            pe3.metric("Realized P&L", f"₹{daily_pnl:,.2f}")
            pe4.metric("Unrealized P&L", f"₹{paper_unrealized:,.2f}")

            if paper_state.get("open_position"):
                pp = paper_state["open_position"]
                pcols = st.columns(6)
                pcols[0].metric("Direction", pp.get("direction", "—"))
                pcols[1].metric("Entry", f"₹{float(pp.get('entry_price', 0)):,.2f}")
                pcols[2].metric("Current", f"₹{float(live_price):,.2f}" if live_price is not None else "—")
                pcols[3].metric("Stop Loss", f"₹{float(pp.get('stop_loss', 0)):,.2f}")
                pcols[4].metric("Target", f"₹{float(pp.get('target', 0)):,.2f}")
                pcols[5].metric("Qty", int(pp.get("quantity", 0)))
                if st.button("⏹️ Manual Paper Exit", type="secondary", use_container_width=True):
                    exit_price = float(live_price) if live_price is not None else float(pp.get("entry_price", 0))
                    closed = paper_close_position(paper_state, exit_price, "MANUAL_EXIT")
                    save_paper_state(paper_user_id, paper_workspace_id, paper_state)
                    if closed:
                        st.success(f"Paper position closed @ ₹{exit_price:,.2f} | P&L ₹{closed['realized_pnl']:,.2f}")
                    st.rerun()
    
            history = paper_state.get("trade_history") or []
            if history:
                st.markdown("**Recent Paper Trades**")
                hist_rows = []
                for t in reversed(history[-10:]):
                    hist_rows.append({
                        "Time": t.get("exit_time", t.get("entry_time", "")),
                        "Direction": t.get("direction", ""),
                        "Entry": round(float(t.get("entry_price", 0)), 2),
                        "Exit": round(float(t.get("exit_price", 0)), 2),
                        "Qty": int(t.get("quantity", 0)),
                        "P&L": round(float(t.get("realized_pnl", 0)), 2),
                        "Exit Reason": t.get("exit_reason", ""),
                    })
                st.dataframe(pd.DataFrame(hist_rows), use_container_width=True, hide_index=True)
            else:
                st.caption("अभी कोई completed paper trade नहीं है।")
    
            st.markdown('<div class="section-head">Phase-3 Monitoring & Audit</div>', unsafe_allow_html=True)
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Paper Engine", "KILLED" if paper_state.get("paper_kill_switch") else "RUNNING")
            m2.metric("Live Price", f"₹{float(live_price):,.2f}" if live_price is not None else "—")
            m3.metric("Audit Events", len(paper_state.get("audit_log") or []))
            m4.metric("Paper P&L", f"₹{float(paper_state.get('daily_realized_pnl', 0.0)) + float(paper_unrealized):,.2f}")
            if live_price is not None:
                source_label = "WebSocket" if ws_price is not None else "Quotes fallback"
                freshness = f"tick age {tick_age:.1f}s" if tick_age is not None else "snapshot"
                st.caption(f"🟢 Live price source: {source_label} • {freshness} • {symbol}")
            else:
                st.info("Live price अभी उपलब्ध नहीं है; last available value बनी रहेगी।")
            kc1, kc2 = st.columns([1, 3])
            with kc1:
                if paper_state.get("paper_kill_switch"):
                    if st.button("▶️ Resume Paper Engine", use_container_width=True):
                        paper_state["paper_kill_switch"] = False
                        paper_audit_event(paper_state, "PAPER_KILL_SWITCH_OFF")
                        save_paper_state(paper_user_id, paper_workspace_id, paper_state)
                        st.rerun()
                else:
                    if st.button("🛑 Paper Kill Switch", type="secondary", use_container_width=True):
                        paper_state["paper_kill_switch"] = True
                        paper_audit_event(paper_state, "PAPER_KILL_SWITCH_ON")
                        save_paper_state(paper_user_id, paper_workspace_id, paper_state)
                        st.rerun()
            with kc2:
                st.caption("यह Phase-3 kill switch केवल PAPER position/engine को रोकता है; live market connection प्रभावित नहीं होता।")
            audit_rows = []
            for ev in reversed((paper_state.get("audit_log") or [])[-15:]):
                audit_rows.append({"Time": ev.get("timestamp", ""), "Event": ev.get("event_type", ""), "Reason": ev.get("reason", ""), "Trade ID": (ev.get("trade") or {}).get("id", "")})
            if audit_rows:
                st.dataframe(pd.DataFrame(audit_rows), use_container_width=True, hide_index=True)
    
            st.markdown('<div class="section-head">Signal Output</div>', unsafe_allow_html=True)
            signal_json = {
                **payload,
                "quantity": plan["quantity"] if plan else 0,
            }
            sc1, sc2, sc3, sc4 = st.columns(4)
            sc1.metric("Direction", direction or "—")
            sc2.metric("Signal", signal)
            sc3.metric("Risk / Reward", f"{plan['risk_reward']:.2f}" if plan else "—")
            sc4.metric("Quantity", plan["quantity"] if plan else 0)
            st.markdown('<div class="json-note">Raw JSON is kept available for download/audit without dominating the dashboard.</div>', unsafe_allow_html=True)
            with st.expander("View raw Signal Output JSON", expanded=False):
                st.json(signal_json)
    
            if plan:
                p1, p2, p3, p4, p5 = st.columns(5)
                p1.metric("Entry", f"{plan['entry']:.2f}")
                p2.metric("Stop Loss", f"{plan['stop_loss']:.2f}")
                p3.metric("Target", f"{plan['target']:.2f}")
                p4.metric("Risk/Reward", f"{plan['risk_reward']:.2f}")
                p5.metric("Quantity", plan["quantity"])
    
            st.markdown('<div class="section-head">Mandatory Checks</div>', unsafe_allow_html=True)
            checks = {
                "Data validation": data_ok,
                "Completed candle": "INCOMPLETE_CANDLE" not in data_reasons,
                "Duplicate check": "DUPLICATE_CANDLE" not in data_reasons,
                "Stale-data check": "STALE_DATA" not in data_reasons,
                "News filter": not news_block,
                "Risk checks": risk_ok,
                "Minimum RR": bool(plan and plan["risk_reward"] >= min_rr),
                "Session / expiry": not bool(session_status["failures"]),
                "ADX support": bool(adx_value is not None and adx_value >= float(adx_min)),
                "5/8 EMA confirmation": bool(ema_state.get("confirmed", False)),
                "Entry distance": bool(trigger_distance_atr is None or trigger_distance_atr <= float(max_trigger_atr)),
                "Setup not expired": not setup_status.get("expired", False),
                "Duplicate protection": not duplicate_entry,
                "Daily loss limit": "DAILY_LOSS_LIMIT" not in paper_risk_failures,
                "Max trades": "MAX_TRADES_REACHED" not in paper_risk_failures,
                "Open positions": "OPEN_POSITION_LIMIT" not in paper_risk_failures,
            }
            check_df = pd.DataFrame(
                [{"Check": k, "Status": "PASS" if v else "FAIL"} for k, v in checks.items()]
            )
            st.dataframe(check_df, use_container_width=True, hide_index=True)
    
            st.markdown('<div class="section-head">Key Levels</div>', unsafe_allow_html=True)
            level_df = pd.DataFrame(
                [{"Level": k.replace("_", " ").title(), "Value": v} for k, v in levels.items()]
            )
            st.dataframe(level_df, use_container_width=True, hide_index=True)
    
            st.markdown('<div class="section-head">Reasons / Invalidations</div>', unsafe_allow_html=True)
            r1, r2 = st.columns(2)
            with r1:
                st.markdown("**Reason codes**")
                if reasons:
                    for x in reasons:
                        st.markdown(f'<div class="reason-item">✅ {x}</div>', unsafe_allow_html=True)
                else:
                    st.markdown('<div class="reason-item">No positive reason code.</div>', unsafe_allow_html=True)
            with r2:
                st.markdown("**Invalidations / blocks**")
                if invalidations:
                    for x in invalidations:
                        st.markdown(f'<div class="reason-item">⛔ {x}</div>', unsafe_allow_html=True)
                else:
                    st.markdown('<div class="reason-item status-pass">✓ None</div>', unsafe_allow_html=True)
    
            st.markdown('<div class="section-head">Validated Candle Data</div>', unsafe_allow_html=True)
            st.dataframe(
                df.tail(50)[
                    ["timestamp", "open", "high", "low", "close", "volume", "vwap",
                     "ma_fast", "ma_slow", "ema_5", "ema_8", "ema_5_8_spread",
                     "rsi", "macd", "macd_signal", "atr", "adx"]
                ],
                use_container_width=True,
                hide_index=True,
                height=420,
            )
    
            st.download_button(
                "Download Signal JSON",
                data=json.dumps(signal_json, indent=2, default=str),
                file_name="trade_easy_signal.json",
                mime="application/json",
                use_container_width=True,
            )
    
            if st.button("Save signal audit", use_container_width=True):
                audit_signal(
                    getattr(user, "id", ""),
                    workspace.get("id"),
                    signal_json,
                )
                st.success("Signal audit save request completed.")
    

    # Invisible strategy polling fragment: it writes to strategy_root only when
    # a new confirmed strategy or paper-trade event is detected.
    if hasattr(st, "fragment"):
        _render_full_dashboard = st.fragment(
            run_every="1s", key="trade_easy_strategy_dashboard"
        )(_render_full_dashboard)
    _render_full_dashboard()

# ============================================================
# PASSWORD RECOVERY: DEFAULT SUPABASE EMAIL (NO CUSTOM SMTP)
# ============================================================
# IMPORTANT: Supabase's default reset link uses the browser URL fragment
# (#access_token=...&refresh_token=...). The fragment never reaches the
# Streamlit Python backend. Therefore the most reliable no-SMTP solution is
# to complete the recovery entirely in the browser with Supabase JS.
#
# The component is blank during normal login. When a recovery fragment is
# present, it reads the fragment in the top-level browser URL, creates a
# recovery session with Supabase JS, lets the user set a new password, then
# redirects back to the clean Trade Easy login URL.

def _browser_password_recovery():
    """Handle Supabase's default password-reset URL entirely in the browser.

    Supabase's default/implicit recovery flow returns access_token and
    refresh_token in the URL fragment (#...). The browser receives that
    fragment; Streamlit's Python backend does not. Using st.html with
    unsafe_allow_javascript=True keeps the token in the browser and avoids
    the iframe limitation of components.html.

    No custom SMTP is required.
    """
    try:
        st.html(
            f"""
            <div id="te-recovery-overlay" style="display:none;position:fixed;inset:0;z-index:2147483647;background:#08111f;color:#fff;font-family:Arial,sans-serif;padding:24px;box-sizing:border-box;overflow:auto;">
              <div style="max-width:520px;margin:70px auto;background:#111c2e;border:1px solid rgba(255,255,255,.15);border-radius:20px;padding:30px;box-shadow:0 20px 70px rgba(0,0,0,.45);">
                <h2 style="margin:0 0 8px;font-size:28px;">🔐 Set New Password</h2>
                <p id="te-recovery-msg" style="color:#b8c5d9;line-height:1.5;">Password reset link verify हो रही है...</p>
                <div id="te-recovery-form" style="display:none;">
                  <label style="display:block;margin:16px 0 7px;">New Password</label>
                  <input id="te-new-password" type="password" autocomplete="new-password" style="width:100%;padding:13px;border-radius:10px;border:1px solid #44546b;background:#0b1422;color:#fff;box-sizing:border-box;font-size:16px;">
                  <label style="display:block;margin:16px 0 7px;">Confirm New Password</label>
                  <input id="te-confirm-password" type="password" autocomplete="new-password" style="width:100%;padding:13px;border-radius:10px;border:1px solid #44546b;background:#0b1422;color:#fff;box-sizing:border-box;font-size:16px;">
                  <button id="te-update-password" style="margin-top:22px;width:100%;padding:14px;border:0;border-radius:10px;background:#2563eb;color:#fff;font-size:16px;font-weight:700;cursor:pointer;">Update Password</button>
                </div>
              </div>
            </div>
            <script>
            (() => {{
              const overlay = document.getElementById('te-recovery-overlay');
              const msg = document.getElementById('te-recovery-msg');
              const form = document.getElementById('te-recovery-form');
              const btn = document.getElementById('te-update-password');
              const pw1 = document.getElementById('te-new-password');
              const pw2 = document.getElementById('te-confirm-password');
              if (!overlay || !msg || !form || !btn) return;

              const SUPABASE_URL = {SUPABASE_URL!r};
              const SUPABASE_KEY = {SUPABASE_PUBLISHABLE_KEY!r};

              function parseRecoveryFragment() {{
                const hash = window.location.hash || '';
                if (!hash || !hash.includes('access_token=')) return null;
                const params = new URLSearchParams(hash.replace(/^#/, ''));
                const type = (params.get('type') || '').toLowerCase();
                const accessToken = params.get('access_token');
                const refreshToken = params.get('refresh_token');
                if (type !== 'recovery' || !accessToken) return null;
                return {{ accessToken, refreshToken, type }};
              }}

              function cleanAndReload() {{
                try {{
                  const u = new URL(window.location.href);
                  u.hash = '';
                  u.searchParams.delete('reset_password');
                  u.searchParams.delete('recovery_access_token');
                  u.searchParams.delete('recovery_refresh_token');
                  u.searchParams.delete('recovery_type');
                  u.searchParams.delete('password_reset_success');
                  window.location.replace(u.toString());
                }} catch (_) {{
                  window.location.reload();
                }}
              }}

              async function updatePassword(accessToken, password) {{
                const response = await fetch(SUPABASE_URL.replace(/\\/$/, '') + '/auth/v1/user', {{
                  method: 'PUT',
                  headers: {{
                    'apikey': SUPABASE_KEY,
                    'Authorization': 'Bearer ' + accessToken,
                    'Content-Type': 'application/json'
                  }},
                  body: JSON.stringify({{ password }})
                }});
                let body = null;
                try {{ body = await response.json(); }} catch (_) {{}}
                if (!response.ok) {{
                  const detail = body && (body.msg || body.message || body.error_description || body.error) ? (body.msg || body.message || body.error_description || body.error) : ('HTTP ' + response.status);
                  throw new Error(detail);
                }}
                return body;
              }}

              async function runRecovery() {{
                const recovery = parseRecoveryFragment();
                if (!recovery) return;

                overlay.style.display = 'block';
                msg.textContent = 'Recovery link verified. नया password सेट करें।';
                form.style.display = 'block';
                try {{ if (document.body) document.body.style.overflow = 'hidden'; }} catch (_) {{}}

                btn.onclick = async () => {{
                  const a = pw1.value || '';
                  const b = pw2.value || '';
                  if (a.length < 8) {{ msg.textContent = 'Password कम-से-कम 8 characters का होना चाहिए।'; return; }}
                  if (a !== b) {{ msg.textContent = 'दोनों passwords match नहीं कर रहे हैं।'; return; }}

                  btn.disabled = true;
                  btn.textContent = 'Updating...';
                  try {{
                    await updatePassword(recovery.accessToken, a);
                    msg.textContent = '✅ Password successfully updated. Login page खुल रहा है...';
                    setTimeout(cleanAndReload, 900);
                  }} catch (e) {{
                    msg.textContent = 'Password update failed: ' + (e && e.message ? e.message : 'Unknown error');
                    btn.disabled = false;
                    btn.textContent = 'Update Password';
                  }}
                }};
              }}

              runRecovery().catch(e => {{
                overlay.style.display = 'block';
                msg.textContent = 'Password reset link verify नहीं हो सका: ' + (e && e.message ? e.message : 'Unknown error');
              }});
            }})();
            </script>
            """,
            unsafe_allow_javascript=True,
            width="stretch",
        )
    except Exception as exc:
        # Do not break the normal login/dashboard if the optional recovery UI
        # cannot render on an older Streamlit runtime.
        st.session_state["trade_easy_recovery_ui_error"] = str(exc)


# ============================================================
# APP ROUTER
# ============================================================

def main():
    # Restore the locally protected FYERS access token before rendering the dashboard.
    # This survives F5/Ctrl+R and Streamlit session recreation.
    _restore_fyers_session()

    # Handle the URL fragment produced by Supabase's DEFAULT password-reset
    # email. This runs before Python reads st.query_params because fragments
    # are browser-only and are not sent to the Streamlit server.
    _browser_password_recovery()

    # Production credentials are loaded from Streamlit Secrets/environment.
    if FYERS_CONFIG_APP_ID:
        st.session_state.setdefault("fyers_app_id", FYERS_CONFIG_APP_ID)
    if FYERS_CONFIG_SECRET:
        st.session_state.setdefault("fyers_secret", FYERS_CONFIG_SECRET)
    user = get_current_user()

    oauth_code = st.query_params.get("code")
    recovery_token_hash = st.query_params.get("token_hash")
    recovery_type = st.query_params.get("type")
    recovery_access_token = st.query_params.get("recovery_access_token")
    recovery_refresh_token = st.query_params.get("recovery_refresh_token")
    recovery_fragment_type = st.query_params.get("recovery_type")
    fyers_auth_code = st.query_params.get("auth_code")
    oauth_error = st.query_params.get("error")
    oauth_error_description = st.query_params.get("error_description")

    # FYERS redirects back with ?s=ok&code=200&auth_code=... .
    # The Connect step stores only SHA256(app_id:secret) locally so the
    # callback still works even if Streamlit recreates its browser session.
    if fyers_auth_code:
        cached_auth = _load_fyers_auth_material()
        app_id = st.session_state.get("fyers_app_id", "").strip() or FYERS_CONFIG_APP_ID
        secret = st.session_state.get("fyers_secret", "").strip() or FYERS_CONFIG_SECRET

        if not app_id or not secret:
            st.session_state["fyers_login_status"] = "error"
            st.session_state["fyers_login_error"] = (
                "FYERS callback मिला, लेकिन authentication material उपलब्ध नहीं है। "
                "Sidebar में App ID + Secret ID डालकर Connect FYERS फिर दबाएँ।"
            )
            clear_oauth_params()
        else:
            try:
                token_response = fyers_exchange_auth_code(
                    app_id,
                    secret,
                    fyers_auth_code,
                )
                if isinstance(token_response, dict) and token_response.get("access_token"):
                    st.session_state["fyers_access_token"] = token_response["access_token"]
                    st.session_state["fyers_login_status"] = "connected"
                    # Keep the access token only in the current Streamlit session.
                    # Production credentials remain in Streamlit Secrets.
                    _save_fyers_session(token_response["access_token"], app_id)
                    st.session_state["fyers_login_url"] = ""
                    st.session_state["fyers_login_error"] = ""
                    st.session_state["fyers_token_response"] = token_response
                    clear_oauth_params()
                    st.rerun()
                else:
                    st.session_state["fyers_login_status"] = "error"
                    st.session_state["fyers_login_error"] = (
                        f"FYERS token exchange failed: {token_response}"
                    )
                    _clear_fyers_session()
                    clear_oauth_params()
            except Exception as exc:
                st.session_state["fyers_login_status"] = "error"
                st.session_state["fyers_login_error"] = (
                    f"FYERS token exchange exception: {type(exc).__name__}: {exc}"
                )
                _clear_fyers_session()
                clear_oauth_params()

    if oauth_error:
        clear_oauth_params()
        st.error(f"Google login failed: {oauth_error_description or oauth_error}")
        st.stop()

    # ------------------------------------------------------------
    # PASSWORD RECOVERY CALLBACK
    # ------------------------------------------------------------
    # IMPORTANT: Handle the password-reset callback BEFORE the normal
    # Google OAuth callback. Supabase sends the reset code back to the
    # deployed app; if the generic oauth_code block consumes it first,
    # the user is returned to the normal Login page instead of seeing
    # the Set New Password screen.
    reset_requested = (
        st.query_params.get("reset_password") == "1"
        or str(recovery_type or "").lower() == "recovery"
        or bool(st.session_state.get("password_recovery"))
    )

    # ------------------------------------------------------------
    # PASSWORD RECOVERY: default Supabase fragment flow (NO SMTP)
    # ------------------------------------------------------------
    # The default Supabase email sends a recovery session in the URL fragment.
    # The browser bridge above converts that fragment into temporary query
    # parameters; now establish the authenticated Supabase session server-side.
    if (
        reset_requested
        and recovery_access_token
        and recovery_refresh_token
        and str(recovery_fragment_type or "").lower() == "recovery"
        and user is None
    ):
        try:
            response = supabase.auth.set_session(
                recovery_access_token,
                recovery_refresh_token,
            )
            recovered_user = getattr(response, "user", None)
            if recovered_user is not None:
                user = recovered_user
                st.session_state["password_recovery"] = True
                clear_oauth_params()
            else:
                user = get_current_user()
                if user is not None:
                    st.session_state["password_recovery"] = True
                    clear_oauth_params()

            if user is None:
                st.error("Password reset session नहीं बन सकी। नया reset link request करें।")
                st.stop()
        except Exception as e:
            st.error(f"Password reset session verification failed: {type(e).__name__}: {e}")
            st.stop()

    # ------------------------------------------------------------
    # PASSWORD RECOVERY: token-hash flow (recommended for Streamlit)
    # ------------------------------------------------------------
    # Supabase's normal email confirmation can return a session in the URL
    # fragment (#access_token=...), which a Python/Streamlit server cannot
    # read. Our recovery email template can instead send token_hash + type
    # to this page. verify_otp() exchanges that token for a real session.
    if reset_requested and recovery_token_hash and user is None:
        try:
            response = supabase.auth.verify_otp({
                "token_hash": recovery_token_hash,
                "type": "recovery",
            })
            recovered_user = getattr(response, "user", None)
            if recovered_user is not None:
                user = recovered_user
                st.session_state["password_recovery"] = True
                clear_oauth_params()
            else:
                user = get_current_user()
                if user is not None:
                    st.session_state["password_recovery"] = True
                    clear_oauth_params()

            if user is None:
                st.error("Password reset link invalid या expired है। नया reset link request करें।")
                st.stop()
        except Exception as e:
            st.error(f"Password reset verification failed: {type(e).__name__}: {e}")
            st.stop()

    # ------------------------------------------------------------
    # PASSWORD RECOVERY: PKCE code fallback
    # ------------------------------------------------------------
    if reset_requested and oauth_code and user is None:
        try:
            response = supabase.auth.exchange_code_for_session({"auth_code": oauth_code})
            recovered_user = getattr(response, "user", None)
            if recovered_user is not None:
                user = recovered_user
                st.session_state["password_recovery"] = True
                clear_oauth_params()
            else:
                user = get_current_user()
                if user is not None:
                    st.session_state["password_recovery"] = True
                    clear_oauth_params()

            if user is None:
                st.error("Password reset session नहीं बन सकी। Reset link दोबारा भेजें।")
                st.stop()
        except Exception as e:
            st.error(f"Password reset callback error: {type(e).__name__}: {e}")
            st.stop()

    # ------------------------------------------------------------
    # NORMAL GOOGLE OAUTH CALLBACK
    # ------------------------------------------------------------
    if oauth_code and user is None and not reset_requested:
        try:
            response = supabase.auth.exchange_code_for_session(
                {"auth_code": oauth_code}
            )

            if getattr(response, "user", None) is not None:
                clear_oauth_params()
                st.rerun()

            user = get_current_user()
            if user is not None:
                clear_oauth_params()
                st.rerun()

            clear_oauth_params()
            st.error("Google login completed, but session was not found.")
            st.stop()

        except Exception as e:
            user_after_error = get_current_user()
            if user_after_error is not None:
                clear_oauth_params()
                st.rerun()

            clear_oauth_params()
            st.error(f"Google login callback error: {e}")
            st.stop()

    if st.session_state.get("password_recovery"):
        if user is None:
            clear_oauth_params()
            st.session_state.pop("password_recovery", None)
            login_page()
            return

        st.markdown("## 🔐 Set New Password")
        st.info("आपका password reset link सही है। नया password सेट करें।")
        new_password = st.text_input(
            "New Password",
            type="password",
            key="recovery_new_password",
        )
        confirm_password = st.text_input(
            "Confirm New Password",
            type="password",
            key="recovery_confirm_password",
        )
        if st.button("Update Password", type="primary", use_container_width=True, key="recovery_update_btn"):
            if len(new_password) < 8:
                st.error("Password कम-से-कम 8 characters का होना चाहिए।")
            elif new_password != confirm_password:
                st.error("दोनों passwords match नहीं कर रहे हैं।")
            else:
                try:
                    response = supabase.auth.update_user({"password": new_password})
                    if getattr(response, "user", None) is not None:
                        st.success("✅ Password successfully updated. अब आप Trade Easy में login कर सकते हैं।")
                        try:
                            supabase.auth.sign_out()
                        except Exception:
                            pass
                        st.session_state.pop("password_recovery", None)
                        clear_oauth_params()
                        st.rerun()
                    else:
                        st.error("Password update failed.")
                except Exception as e:
                    st.error(f"Password update error: {e}")
        st.stop()

    if user is None:
        login_page()
        return

    workspace, workspace_error = get_or_create_workspace(user)
    if workspace is None:
        st.error("Workspace load नहीं हो पाया।")
        st.code(workspace_error or "Unknown workspace error")
        st.info(
            "Supabase में public.workspaces पर authenticated user के SELECT/INSERT "
            "permissions और RLS policies जाँचें।"
        )
        st.stop()

    st.session_state["workspace_id"] = workspace["id"]
    st.session_state["workspace_name"] = workspace["workspace_name"]
    st.session_state["user_id"] = getattr(user, "id", "")
    st.session_state["user_email"] = getattr(user, "email", "")

    profile, profile_error = ensure_user_profile(user)
    if profile is None:
        st.error("User profile system load नहीं हो पाया।")
        st.code(profile_error or "Unknown profile error")
        st.info("Supabase में `Trade_Easy_SUBSCRIPTION_SETUP.sql` एक बार run करें।")
        st.stop()

    # Admins bypass subscription gating so they can always manage users/plans.
    if is_admin_profile(profile):
        admin_dashboard(user, profile, workspace)
        return

    allowed, subscription = subscription_access_state(getattr(user, "id", ""))
    st.session_state["trade_easy_subscription"] = subscription
    if not allowed:
        subscription_block_page(subscription, user)
        return

    dashboard(user, workspace)


if __name__ == "__main__":
    main()
