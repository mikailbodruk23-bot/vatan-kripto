import json
import sys
import urllib.request
from datetime import datetime, timezone

# VATAN 2 - Coklu zaman dilimi testi

COINS = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP"
]

PERIODS = [
    "1Dutc",
    "4H",
    "1H",
    "15m"
]

BASE = "https://www.okx.com"


def get_candles(symbol, period):
    url = (
        BASE + "/api/v5/market/candles"
        f"?instId={symbol}&bar={period}&limit=100"
    )

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "VATAN2-Analiz/1.0"}
    )

    with urllib.request.urlopen(
        req, timeout=15
    ) as response:
        data = json.load(response)

    if data.get("code") != "0":
        raise RuntimeError(str(data)[:250])

    candles = [
        c for c in data.get("data", [])
        if len(c) >= 9 and c[8] == "1"
    ]

    candles.sort(key=lambda c: int(c[0]))

    if len(candles) < 35:
        raise ValueError("Yetersiz mum verisi")

    return candles


def ema(values, period):
    result = sum(values[:period]) / period
    k = 2 / (period + 1)

    for price in values[period:]:
        result = price * k + result * (1 - k)

    return result


def analyze(symbol, period):
    candles = get_candles(symbol, period)

    closes = [float(c[4]) for c in candles]

    price = closes[-1]
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)

    if price > ema9 > ema21:
        direction = "LONG"

    elif price < ema9 < ema21:
        direction = "SHORT"

    else:
        direction = "BEKLE"

    return {
        "zaman": period,
        "fiyat": price,
        "ema9": round(ema9, 4),
        "ema21": round(ema21, 4),
        "yon": direction,
        "mum_sayisi": len(candles)
    }


def main():
    print("VATAN 2 COKLU ZAMAN ANALIZI")
    print(datetime.now(timezone.utc).isoformat())

    errors = 0

    for coin in COINS:
        print("\nCOIN:", coin)
        directions = []

        for period in PERIODS:
            try:
                result = analyze(coin, period)
                print(json.dumps(result))
                directions.append(result["yon"])

            except Exception as error:
                errors += 1
                print(period, "HATA:", error)

        if len(directions) != len(PERIODS):
            print("SONUC: VERI EKSIK")

        elif (
            len(set(directions)) == 1
            and directions[0] != "BEKLE"
        ):
            print("SONUC: TREND UYUMLU")

        else:
            print("SONUC: BEKLE - TREND CELISKISI")

    if errors:
        sys.exit(1)

    print("\nANALIZ TESTI BASARILI")


if __name__ == "__main__":
    main()
  
