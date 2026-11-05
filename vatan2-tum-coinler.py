#!/usr/bin/env python3
"""VATAN 2 – OKX'teki tüm aktif USDT-SWAP sözleşmelerini tarar.

Python 3.11 standard library; kimlik bilgisi ve otomatik emir gerektirmez.
Yalnızca kapanmış mumları kullanır. Her coin değerlendirilir; eksik veri
olan coinler de sonuç JSON'unda HATA/VERI_EKSIK olarak görünür.
"""
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://www.okx.com"
BARS = ("1Dutc", "4H", "1H", "15m")
WEIGHTS = {"1Dutc": 20, "4H": 20, "1H": 15, "15m": 15}
OUT = Path("son-tum-analiz.json")
WORKERS = max(1, min(int(os.environ.get("VATAN_WORKERS", "7")), 10))
MIN_INTERVAL = 0.085  # ~11.7 istek/sn; OKX mum limiti 40/2sn altinda
MIN_TURNOVER = 2_000_000  # Yalnizca islem ONAYI icin; taramaya sinir getirmez
MAX_SPREAD_PCT = 0.35
_request_lock = threading.Lock()
_next_request = 0.0


def request_json(path, params, retries=3):
    global _next_request
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    last_error = None
    for attempt in range(retries):
        with _request_lock:
            now = time.monotonic()
            wait = max(0, _next_request - now)
            _next_request = max(now, _next_request) + MIN_INTERVAL
        if wait:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "VATAN2-OKX-AllScanner/1.0",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=12) as response:
                result = json.load(response)
            if result.get("code") != "0":
                raise ValueError("OKX " + str(result.get("code")) + ": " + str(result.get("msg"))[:90])
            return result.get("data", [])
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(str(last_error)[:160])


def discover():
    instruments = request_json("/api/v5/public/instruments", {"instType": "SWAP"})
    tickers = request_json("/api/v5/market/tickers", {"instType": "SWAP"})
    ticks = {x.get("instId"): x for x in tickers}
    result = []
    for instrument in instruments:
        symbol = instrument.get("instId", "")
        if not (instrument.get("state") == "live" and
                instrument.get("settleCcy") == "USDT" and
                symbol.endswith("-USDT-SWAP") and
                instrument.get("ctType", "linear") == "linear"):
            continue
        t = ticks.get(symbol, {})
        price = max(0.0, safe_number(t.get("last")))
        # OKX volCcy24h: coin miktari; fiyatla carpilarak yaklasik USDT hacmine cevrilir.
        turnover = safe_number(t.get("volCcy24h")) * price
        bid = safe_number(t.get("bidPx"))
        ask = safe_number(t.get("askPx"))
        spread = 100.0 * (ask - bid) / ((ask + bid) / 2) if bid > 0 and ask >= bid else None
        result.append({"coin": symbol, "anlik_fiyat": price,
                       "hacim24h_usdt_tahmini": round(turnover, 2),
                       "spread_yuzde": round(spread, 4) if spread is not None else None})
    # TAM KAPSAM: likidite az olsa da coinler elenmez; sadece islem onayi filtrelenir.
    return sorted(result, key=lambda x: x["coin"])


def safe_number(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else 0.0
    except (ValueError, TypeError):
        return 0.0


def candles(symbol, bar):
    rows = request_json("/api/v5/market/candles", {"instId": symbol, "bar": bar, "limit": "100"})
    closed = [c for c in rows if len(c) >= 9 and c[8] == "1"]
    closed.sort(key=lambda c: int(c[0]))
    if len(closed) < 60:
        raise ValueError(f"{bar}: sadece {len(closed)} kapanmis mum")
    return closed


def ema(values, period):
    val = sum(values[:period]) / period
    k = 2 / (period + 1)
    for x in values[period:]:
        val += (x - val) * k
    return val


def direction(rows):
    values = [float(r[4]) for r in rows]
    p, e9, e21 = values[-1], ema(values, 9), ema(values, 21)
    if p > e9 > e21:
        return "LONG"
    if p < e9 < e21:
        return "SHORT"
    return "BEKLE"


def rsi(values, period=14):
    changes = [b - a for a, b in zip(values, values[1:])]
    gains = sum(max(0, d) for d in changes[:period]) / period
    losses = sum(max(0, -d) for d in changes[:period]) / period
    for d in changes[period:]:
        gains = (gains * (period - 1) + max(0, d)) / period
        losses = (losses * (period - 1) + max(0, -d)) / period
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    return 100 - 100 / (1 + gains / losses)


def macd_diff(values):
    series = [ema(values[:n], 12) - ema(values[:n], 26)
              for n in range(26, len(values) + 1)]
    return series[-1] - ema(series, 9)


def atr(rows, period=14):
    last = rows[-(period + 1):]
    trs = []
    for prev, now in zip(last, last[1:]):
        h, l, prev_close = float(now[2]), float(now[3]), float(prev[4])
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
    return sum(trs) / period


def analyze(market, btc_trends):
    symbol = market["coin"]
    row = dict(market)
    try:
        history = {p: candles(symbol, p) for p in BARS}
        trends = {p: direction(history[p]) for p in BARS}
        closes = [float(c[4]) for c in history["15m"]]
        vols = [float(c[7]) for c in history["15m"]]  # Gercek USDT cinsinden hacim
        avg = sum(vols[-21:-1]) / 20
        ratio = vols[-1] / avg if avg > 0 else 0.0
        strength, momentum, vol = rsi(closes), macd_diff(closes), atr(history["15m"])
        # Iki yonun puani da olculur; trend celiskisi varsa yine puan gosterilir,
        # ancak islem icin tum zaman dilimlerinde yon birligi gerekir.
        scores = {}
        for side in ("LONG", "SHORT"):
            s = sum(w for p, w in WEIGHTS.items() if trends[p] == side)
            if (side == "LONG" and 50 <= strength <= 70) or (side == "SHORT" and 30 <= strength <= 50):
                s += 10
            if (side == "LONG" and momentum > 0) or (side == "SHORT" and momentum < 0):
                s += 10
            s += 10 if ratio >= 1.3 else 5 if ratio >= 1 else 0
            scores[side] = s
        side = max(scores, key=scores.get)
        score = scores[side]
        aligned = all(trends[p] == side for p in BARS)
        btc_ok = all(btc_trends.get(p) == side for p in ("1Dutc", "4H"))
        spread = market["spread_yuzde"]
        liquid = (market["hacim24h_usdt_tahmini"] >= MIN_TURNOVER
                  and vols[-1] >= 100_000
                  and spread is not None and 0 <= spread <= MAX_SPREAD_PCT)
        ready = bool(score >= 75 and aligned and btc_ok and liquid and vol > 0)
        row.update({
            "yonler": trends, "fiyat_kapanis": closes[-1],
            "rsi15m": round(strength, 2), "macd15m": round(momentum, 6),
            "hacim_orani": round(ratio, 3), "puan": score,
            "aday_yon": side, "btc_filtre": btc_ok, "trend_onayi": aligned,
            "likidite_onayi": liquid, "karar": side + " ADAY" if ready else "BEKLE",
            "durum": "ANALIZ_EDILDI", "mum_15m_ts": int(history["15m"][-1][0]),
        })
        if ready:
            risk = vol * 1.5
            entry = closes[-1]
            row.update({"giris_referans": entry,
                        "stop": round(entry - risk if side == "LONG" else entry + risk, 10),
                        "hedef": round(entry + 1.8 * risk if side == "LONG" else entry - 1.8 * risk, 10),
                        "risk_odul": 1.8})
    except (Exception,) as exc:
        row.update({"karar": "BEKLE", "puan": None, "durum": "VERI_EKSIK",
                    "hata": str(exc)[:150]})
    return row


def main():
    started = time.monotonic()
    print("VATAN 2 – TUM AKTIF USDT-SWAP TARAMASI", flush=True)
    markets = discover()
    if not markets:
        raise RuntimeError("OKX'te uygun USDT SWAP sozlesmesi bulunamadi")
    print("Bulunan aktif USDT SWAP:", len(markets), flush=True)
    btc_market = next((m for m in markets if m["coin"] == "BTC-USDT-SWAP"), None)
    if not btc_market:
        raise RuntimeError("BTC yon filtresi icin BTC-USDT-SWAP bulunamadi")
    btc_trends = {p: direction(candles("BTC-USDT-SWAP", p)) for p in ("1Dutc", "4H")}
    print("BTC trend filtresi:", btc_trends, flush=True)
    rows = []
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(analyze, m, btc_trends) for m in markets]
        for i, f in enumerate(as_completed(futures), start=1):
            rows.append(f.result())
            if i % 25 == 0 or i == len(futures):
                print(f"Taranan {i}/{len(futures)} – sure {int(time.monotonic()-started)}sn", flush=True)
    rows.sort(key=lambda x: (x["durum"] != "ANALIZ_EDILDI", -int(x["puan"] or 0), x["coin"]))
    done = sum(r["durum"] == "ANALIZ_EDILDI" for r in rows)
    approved = sum(r["karar"] in ("LONG ADAY", "SHORT ADAY") for r in rows)
    if done == 0:
        raise RuntimeError("Hicbir sozlesme analiz edilemedi; eski sonucu koruyoruz")
    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "OKX",
        "market": "USDT-SWAP",
        "eligible_count": len(markets),
        "analyzed_count": done,
        "error_count": len(markets) - done,
        "approved_count": approved,
        "btc_trends": btc_trends,
        "scan_seconds": round(time.monotonic()-started, 1),
        "results": rows,
    }
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUT)
    print("TAMAMLANDI", json.dumps({k: v for k, v in output.items() if k != "results"}, ensure_ascii=False), flush=True)
    print("JSON:", OUT, OUT.stat().st_size, "bytes", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("TARAMA BASARISIZ:", exc, file=sys.stderr)
        sys.exit(1)
