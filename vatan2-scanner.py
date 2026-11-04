
import json
import sys
import urllib.request
from datetime import datetime, timezone

# VATAN 2 - OKX Futures Scanner
SYMBOLS = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP"
]

def get_candles(symbol):
    url = (
        "https://www.okx.com/api/v5/market/candles"
        f"?instId={symbol}&bar=15m&limit=60"
    )

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "VATAN2-Scanner/2.0"}
    )

    with urllib.request.urlopen(
        req, timeout=15
    ) as response:
        result = json.load(response)

    if result.get("code") != "0":
        raise ValueError(str(result)[:300])

    candles = [
        row for row in result.get("data", [])
        if len(row) >= 9 and row[8] == "1"
    ]

    if len(candles) < 30:
        raise ValueError("Yetersiz mum verisi")

    candles.sort(key=lambda row: int(row[0]))
    return candles


def ema(values, period):
    value = sum(values[:period]) / period
    multiplier = 2 / (period + 1)

    for price in values[period:]:
        value = (
            price * multiplier
            + value * (1 - multiplier)
        )

    return value


def scan(symbol):
    candles = get_candles(symbol)
    closes = [float(c[4]) for c in candles]

    price = closes[-1]
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)

    if price > ema9 > ema21:
        signal = "LONG ADAYI"

    elif price < ema9 < ema21:
        signal = "SHORT ADAYI"

    else:
        signal = "BEKLE"

    return {
        "coin": symbol,
        "fiyat": price,
        "sinyal": signal,
        "ema9": round(ema9, 4),
        "ema21": round(ema21, 4),
        "mum": "15m kapanmis"
    }


def main():
    print("VATAN 2 - OKX TARAMA")
    print(datetime.now(timezone.utc).isoformat())

    errors = 0

    for symbol in SYMBOLS:
        try:
            result = scan(symbol)
            print(
                json.dumps(
                    result,
                    ensure_ascii=False
                )
            )

        except Exception as error:
            errors += 1
            print(f"{symbol}: HATA - {error}")

    if errors:
        print(f"TARAMA HATASI: {errors}")
        sys.exit(1)

    print("OKX TARAMA BASARILI")


if __name__ == "__main__":
    main()
