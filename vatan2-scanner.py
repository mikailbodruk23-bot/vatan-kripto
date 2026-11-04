
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

# VATAN 2 - Binance Futures Tarama
BASE = "https://fapi.binance.com"

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT"
]


def get_json(path):
    url = BASE + path

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "VATAN2-Scanner/1.0",
            "Accept": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(
            req, timeout=15
        ) as response:
            return json.load(response)

    except urllib.error.HTTPError as e:
        detail = e.read(500).decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            f"HTTP {e.code}: {detail}"
        ) from e

    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Baglanti hatasi: {e.reason}"
        ) from e


def scan(symbol):
    path = (
        f"/fapi/v1/klines?symbol={symbol}"
        "&interval=15m&limit=60"
    )

    data = get_json(path)

    if not isinstance(data, list):
        raise ValueError("Gecersiz veri")

    if len(data) < 22:
        raise ValueError("Yetersiz mum verisi")

    # Son kapanmis mumlari kullan
    candles = data[:-1]

    closes = [
        float(candle[4])
        for candle in candles
    ]

    price = closes[-1]

    # Basit ortalama - EMA degildir
    avg9 = sum(closes[-9:]) / 9
    avg21 = sum(closes[-21:]) / 21

    if avg9 > avg21 and price > avg9:
        signal = "LONG ADAYI"

    elif avg9 < avg21 and price < avg9:
        signal = "SHORT ADAYI"

    else:
        signal = "BEKLE"

    return {
        "symbol": symbol,
        "price": price,
        "signal": signal,
        "avg9": round(avg9, 4),
        "avg21": round(avg21, 4)
    }


def main():
    print("VATAN 2 TARAMA TESTI")
    print(
        datetime.now(
            timezone.utc
        ).isoformat()
    )

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
            print(
                f"{symbol}: HATA - {error}"
            )

    if errors > 0:
        print(
            f"TARAMA BASARISIZ: {errors} hata"
        )
        sys.exit(1)

    print("TARAMA BASARILI")


if __name__ == "__main__":
    main()
