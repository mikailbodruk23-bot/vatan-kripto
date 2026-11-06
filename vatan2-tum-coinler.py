#!/usr/bin/env python3
"""VATAN 2 – Binance USDT-M aktif perpetual sözleşmelerini tarar.

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

from vatan2_core import adx, choppiness, market_regime

BASE = "https://fapi.binance.com"
BARS = ("1Dutc", "4H", "1H", "15m")
WEIGHTS = {"1Dutc": 20, "4H": 20, "1H": 15, "15m": 15}
OUT = Path("son-tum-analiz.json")
WORKERS = max(1, min(int(os.environ.get("VATAN_WORKERS", "7")), 10))
MIN_INTERVAL = 0.12  # ~8.3 REST istegi/sn; Binance IP weight ve dalgali hatalara pay
MIN_TURNOVER = 2_000_000  # Yalnizca islem ONAYI icin; taramaya sinir getirmez
MAX_SPREAD_PCT = 0.35
_request_lock = threading.Lock()
_next_request = 0.0


def request_json(path, params=None, retries=3):
    """Binance USDT-M public REST; rate-limit aware, no API key."""
    global _next_request
    url = BASE + path + (('?' + urllib.parse.urlencode(params)) if params else '')
    err = None
    for attempt in range(retries):
        with _request_lock:
            now = time.monotonic()
            wait = max(0, _next_request - now)
            _next_request = max(now, _next_request) + MIN_INTERVAL
        if wait: time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'VATAN2-Binance-UM-Public/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=14) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            err = exc
            if exc.code in (418, 429):
                # Do not continue hammering the exchange after a ban/rate limit.
                raise RuntimeError('Binance rate limit / IP ban: HTTP '+str(exc.code)) from exc
        except (urllib.error.URLError, ValueError, TimeoutError, OSError) as exc:
            err = exc
        if attempt < retries-1: time.sleep(0.8*(attempt+1))
    raise RuntimeError(str(err)[:160])


def discover():
    info = request_json('/fapi/v1/exchangeInfo')
    tickers = request_json('/fapi/v1/ticker/24hr')
    books = request_json('/fapi/v1/ticker/bookTicker')
    if not isinstance(info, dict) or not isinstance(tickers, list) or not isinstance(books,list):
        raise ValueError('Binance REST veri semasi beklenmedik')
    ticks = {t.get('symbol'): t for t in tickers if isinstance(t,dict)}
    bmap = {t.get('symbol'): t for t in books if isinstance(t,dict)}
    markets = []
    for inst in info.get('symbols',[]):
        symbol = inst.get('symbol','')
        if not (inst.get('contractType')=='PERPETUAL' and inst.get('status')=='TRADING'
                and inst.get('quoteAsset')=='USDT' and inst.get('marginAsset')=='USDT'
                and symbol.endswith('USDT')):
            continue
        t=ticks.get(symbol,{})
        b=bmap.get(symbol,{})
        px=safe_number(t.get('lastPrice'))
        turnover=safe_number(t.get('quoteVolume'))
        bid=safe_number(b.get('bidPrice'))
        ask=safe_number(b.get('askPrice'))
        spread=(100*(ask-bid)/((ask+bid)/2)) if bid>0 and ask>=bid else None
        markets.append({'coin':symbol,'anlik_fiyat':px,
                        'hacim24h_usdt_tahmini':round(turnover,2),
                        'spread_yuzde':round(spread,5) if spread is not None else None})
    return sorted(markets,key=lambda x:x['coin'])


def safe_number(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else 0.0
    except (ValueError, TypeError):
        return 0.0


def candles(symbol, bar):
    interval = {'1Dutc':'1d', '4H':'4h', '1H':'1h','15m':'15m'}[bar]
    rows=request_json('/fapi/v1/klines', {'symbol':symbol,'interval':interval,'limit':110})
    if not isinstance(rows,list): raise ValueError('Binance mum listesi eksik')
    now_ms=int(time.time()*1000)
    # Binance klines: [open_time,open,high,low,close,base_volume,close_time,quote_volume,...]
    # Never use current unfinished candle, append the OKX adapter's confirmed flag.
    closed=[]
    for candle in rows:
        if not isinstance(candle,list) or len(candle)<9: continue
        if int(candle[6])>=now_ms-1500: continue
        closed.append(candle[:8]+['1'])
    closed.sort(key=lambda c:int(c[0]))
    if len(closed)<60: raise ValueError(f'{bar}: {len(closed)} kapanmis mum')
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


def price_action(history):
    """Closed-candle price structure only. No indicator line on the user chart.

    UTC daily levels refer to the LAST COMPLETED UTC day (yesterday).
    Sweeps are potential reactions, never guaranteed liquidation/stop data.
    """
    day = history['1Dutc'][-1]
    yesterday = {'acilis': float(day[1]), 'en_yuksek': float(day[2]),
                 'en_dusuk': float(day[3]), 'kapanis': float(day[4]),
                 'gun_ts': int(day[0])}
    hour = history['1H']
    hr = hour[-21:]
    closes = [float(c[4]) for c in hr]
    high = max(float(c[2]) for c in hr)
    low = min(float(c[3]) for c in hr)
    path = sum(abs(b-a) for a,b in zip(closes,closes[1:]))
    efficiency = abs(closes[-1]-closes[0])/path if path else 0
    regime = 'TREND' if efficiency >= 0.43 else 'YATAY' if efficiency <= 0.22 else 'GECIS'
    range_position = (closes[-1]-low)/(high-low) if high>low else .5
    range_position = round(max(0, min(1, range_position)), 3)
    mid_range = regime == 'YATAY' and 0.35 <= range_position <= 0.65

    sweep = 'YOK'
    note = 'Son kapanmış 15 dk mumlarında dünün tepe/dibinde belirgin süpürme yok'
    # Most recent closed candle wins. A wick followed by a close back inside
    # yesterday's range is only a *possible* stop sweep, not proven liquidation.
    for c in reversed(history['15m'][-3:]):
        hi,lo,close = float(c[2]),float(c[3]),float(c[4])
        if hi > yesterday['en_yuksek'] and close < yesterday['en_yuksek']:
            sweep = 'TEPE_IGNE_SHORT';note='Dünün tepesinin üstünde iğne, sonra altında kapanış: olası SHORT tuzağı teyidi';break
        if lo < yesterday['en_dusuk'] and close > yesterday['en_dusuk']:
            sweep = 'DIP_IGNE_LONG';note='Dünün dibinin altında iğne, sonra üstünde kapanış: olası LONG dönüş teyidi';break
    return {
        'dun_seviyeleri': yesterday, 'piyasa_rejimi': regime,
        'range_konumu': range_position, 'range_orta_riski': mid_range,
        'likidite_supurmesi': sweep, 'sweep_notu': note,
        'son_15m_kapanislar': [round(float(c[4]), 10) for c in history['15m'][-28:]],
        'son_15m_mumlar': [[int(c[0])]+[round(float(c[j]),10) for j in (1,2,3,4)]
                           for c in history['15m'][-24:]],
        'son_15m_kapanis_ts': int(history['15m'][-1][0]),
        'fiyat_yapisi_yontemi':'KAPANMIS_MUM_1H_15M'
    }


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
        action = price_action(history)
        # Completed 1h candle ADX/CHOP are confirmation gates, not extra score.
        adx_1h = adx(history['1H'])
        chop_1h = choppiness(history['1H'])
        hours = history['1H'][-21:]
        close_hours = [float(c[4]) for c in hours]
        travelled = sum(abs(b-a) for a,b in zip(close_hours, close_hours[1:]))
        efficiency = abs(close_hours[-1]-close_hours[0])/travelled if travelled else 0
        regime = market_regime(adx_1h, chop_1h, efficiency)
        action['piyasa_rejimi'] = regime
        action['adx1h'] = adx_1h
        action['chop1h'] = chop_1h
        action['trend_verimliligi1h'] = round(efficiency, 4)
        action['rejim_yontemi'] = 'ADX14_CHOP14_EFFICIENCY20_CLOSED_1H'
        action['range_orta_riski'] = regime == 'YATAY' and 0.35 <= action['range_konumu'] <= 0.65
        row.update(action)
        row.update({
            "yonler": trends, "fiyat_kapanis": closes[-1],
            "rsi15m": round(strength, 2), "macd15m": round(momentum, 6),
            "hacim_orani": round(ratio, 3), "puan": score,
            "aday_yon": side, "btc_filtre": btc_ok, "trend_onayi": aligned,
            "likidite_onayi": liquid, "karar": side + " ADAY" if ready else "BEKLE",
            "durum": "ANALIZ_EDILDI", "mum_15m_ts": int(history["15m"][-1][0]),
        })
        # Compute a provisional structure-aware plan even if macro/funding
        # later adjusts the score. The final report shows valid levels ONLY
        # for 85+ as either approved plan or clearly marked non-tradable draft.
        if vol > 0 and closes[-1] > 0:
            risk = vol * 1.5
            entry = closes[-1]
            stop = entry - risk if side == 'LONG' else entry + risk
            target = entry + 1.8 * risk if side == 'LONG' else entry - 1.8 * risk
            if stop > 0 and target > 0:
                row.update({'giris_referans': entry,
                            'stop': round(stop, 10), 'hedef': round(target, 10),
                            'risk_odul': 1.8})
    except (Exception,) as exc:
        row.update({"karar": "BEKLE", "puan": None, "durum": "VERI_EKSIK",
                    "hata": str(exc)[:150]})
    return row


def main():
    started = time.monotonic()
    print("VATAN 2 – TUM BINANCE USDT-M TARAMASI", flush=True)
    markets = discover()
    if not markets:
        raise RuntimeError("Binance USD-M perpetual sozlesme bulunamadi")
    print("Bulunan aktif USDT-M perpetual:", len(markets), flush=True)
    btc_market = next((m for m in markets if m["coin"] == "BTCUSDT"), None)
    if not btc_market:
        raise RuntimeError("BTC yon filtresi icin BTCUSDT bulunamadi")
    btc_trends = {p: direction(candles("BTCUSDT", p)) for p in ("1Dutc", "4H")}
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
        "source": "BINANCE_UM",
        "market": "USDT-M-PERPETUAL",
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
