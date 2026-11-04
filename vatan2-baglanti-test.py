
import json
import urllib.request
import urllib.error

TESTLER = {
    "OKX": (
        "https://www.okx.com/api/v5/market/ticker"
        "?instId=BTC-USDT-SWAP"
    ),
    "BYBIT": (
        "https://api.bybit.com/v5/market/tickers"
        "?category=linear&symbol=BTCUSDT"
    )
}

for isim, url in TESTLER.items():
    print(f"\n{isim} BAGLANTI TESTI")

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "VATAN2-Test/1.0",
                "Accept": "application/json"
            }
        )

        with urllib.request.urlopen(
            req, timeout=15
        ) as response:
            veri = json.load(response)

        if isim == "OKX":
            if veri.get("code") != "0":
                raise ValueError(str(veri)[:300])
            fiyat = veri["data"][0]["last"]

        else:
            if veri.get("retCode") != 0:
                raise ValueError(str(veri)[:300])
            fiyat = veri["result"]["list"][0]["lastPrice"]

        print("BASARILI")
        print("BTC Fiyati:", fiyat)

    except urllib.error.HTTPError as e:
        print("HTTP HATA:", e.code)
        print(
            e.read(300).decode(
                "utf-8", errors="replace"
            )
        )

    except Exception as e:
        print("BAGLANTI HATASI:", str(e))

print("\nTEST TAMAMLANDI")
print("\nOKX 15 DAKIKALIK MUM TESTI")

for symbol in [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP"
]:
    try:
        url = (
            "https://www.okx.com/api/v5/market/candles"
            f"?instId={symbol}&bar=15m&limit=60"
        )

        req = urllib.request.Request(
            url,
            headers={"User-Agent": "VATAN2-Test/1.0"}
        )

        with urllib.request.urlopen(
            req, timeout=15
        ) as response:
            result = json.load(response)

        if result.get("code") != "0":
            raise ValueError(str(result)[:200])

        candles = [
            c for c in result["data"]
            if c[8] == "1"
        ]

        if len(candles) < 21:
            raise ValueError("Yetersiz mum verisi")

        candles.sort(key=lambda c: int(c[0]))

        print(
            symbol,
            "BASARILI",
            "Mum:", len(candles),
            "Kapanis:", candles[-1][4]
        )

    except Exception as error:
        print(symbol, "HATA:", error)
        
