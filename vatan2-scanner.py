
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

BASE = "https://fapi.binance.com"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

def get_json(path):
    req = urllib.request.Request(
        BASE + path,
        headers={"User-Agent": "VATAN2-Scanner/1.0"}
    try:
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)
except urllib.error.HTTPError as e:
    detail = e.read(500).decode("utf-8", errors="replace")
    raise RuntimeError(
        f"Binance HTTP {e.code}: {detail}"
    ) from e

def scan(symbol):
    data = get_json(
        f"/fapi/v1/klines?symbol={symbol}&interval=15m&limit=60"
    )

    # Son kapanmış mumları kullan
    candles = data[:-1]
    closes = [float(c[4]) for c in candles]

    ema9 = sum(closes[-9:]) / 9
    ema21 = sum(closes[-21:]) / 21
    price = closes[-1]

    if ema9 > ema21 and price > ema9:
        signal = "LONG ADAYI"
    elif ema9 < ema21 and price < ema9:
        signal = "SHORT ADAYI"
    else:
        signal = "BEKLE"

    return {
        "symbol": symbol,
        "price": price,
        "signal": signal,
        "ema9_proxy": round(ema9, 4),
        "ema21_proxy": round(ema21, 4)
    }

print("VATAN 2 tarama testi")
print(datetime.now(timezone.utc).isoformat())

for symbol in SYMBOLS:
    try:
        print(json.dumps(scan(symbol), ensure_ascii=False))
    except Exception as error:
        print(f"{symbol}: HATA - {error}")
      
