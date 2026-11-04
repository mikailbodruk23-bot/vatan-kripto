
import json
import sys
import urllib.request
from datetime import datetime, timezone

# VATAN 2 - 75 puan analizi (test)
COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
PERIODS = ["1Dutc", "4H", "1H", "15m"]
WEIGHTS = {"1Dutc": 20, "4H": 20, "1H": 15, "15m": 15}


def candles(symbol, period):
    url = (
        "https://www.okx.com/api/v5/market/candles"
        f"?instId={symbol}&bar={period}&limit=100"
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": "VATAN2-Analiz/2.0"}
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        result = json.load(response)

    if result.get("code") != "0":
        raise RuntimeError(str(result)[:250])

    rows = [
        row for row in result.get("data", [])
        if len(row) >= 9 and row[8] == "1"
    ]
    rows.sort(key=lambda row: int(row[0]))

    if len(rows) < 60:
        raise ValueError("Yetersiz kapanmis mum")

    return rows


def ema(values, period):
    answer = sum(values[:period]) / period
    k = 2 / (period + 1)

    for number in values[period:]:
        answer = number * k + answer * (1 - k)

    return answer


def direction(rows):
    prices = [float(row[4]) for row in rows]
    price = prices[-1]
    e9 = ema(prices, 9)
    e21 = ema(prices, 21)

    if price > e9 > e21:
        return "LONG"
    if price < e9 < e21:
        return "SHORT"
    return "BEKLE"


def rsi(values, period=14):
    changes = [b - a for a, b in zip(values, values[1:])]
    first = changes[:period]

    gain = sum(max(x, 0) for x in first) / period
    loss = sum(max(-x, 0) for x in first) / period

    for change in changes[period:]:
        gain = (
            gain * (period - 1) + max(change, 0)
        ) / period
        loss = (
            loss * (period - 1) + max(-change, 0)
        ) / period

    if loss == 0:
        return 100.0 if gain > 0 else 50.0

    return 100 - 100 / (1 + gain / loss)


def macd(values):
    line = [
        ema(values[:end], 12) - ema(values[:end], 26)
        for end in range(26, len(values) + 1)
    ]
    return line[-1], ema(line, 9)


def atr(rows, period=14):
    recent = rows[-(period + 1):]
    ranges = []

    for before, current in zip(recent, recent[1:]):
        high = float(current[2])
        low = float(current[3])
        prev = float(before[4])

        ranges.append(
            max(
                high - low,
                abs(high - prev),
                abs(low - prev)
            )
        )

    return sum(ranges) / period


def analyze(symbol, btc_trends):
    history = {
        p: candles(symbol, p)
        for p in PERIODS
    }

    trends = {
        p: direction(history[p])
        for p in PERIODS
    }

    daily = trends["1Dutc"]
    main = trends["4H"]

    
    side = next(
        (
            trends[p]
            for p in ["1Dutc", "4H", "1H", "15m"]
            if trends[p] != "BEKLE"
        ),
        "BEKLE"
    )


    closes = [
        float(row[4])
        for row in history["15m"]
    ]

    price = closes[-1]
    strength = rsi(closes)
    macd_value, macd_signal = macd(closes)

    volumes = [
        float(row[7])
        for row in history["15m"]
    ]

    prior_average = sum(volumes[-21:-1]) / 20

    volume_ratio = (
        volumes[-1] / prior_average
        if prior_average > 0
        else 0
    )

    score = 0

    if side != "BEKLE":
        score = sum(
            weight
            for period, weight in WEIGHTS.items()
            if trends[period] == side
        )

        if (
            side == "LONG" and 50 <= strength <= 70
        ) or (
            side == "SHORT" and 30 <= strength <= 50
        ):
            score += 10

        if (
            side == "LONG" and macd_value > macd_signal
        ) or (
            side == "SHORT" and macd_value < macd_signal
        ):
            score += 10

        if volume_ratio >= 1.3:
            score += 10
        elif volume_ratio >= 1.0:
            score += 5

    btc_ok = side != "BEKLE" and all(
        btc_trends[p] == side
        for p in ["1Dutc", "4H"]
    )

    full_trend = side != "BEKLE" and all(
        trends[p] == side
        for p in PERIODS
    )

    liquid = volumes[-1] >= 100000
    volatility = atr(history["15m"])

    ready = (
        score >= 75
        and btc_ok
        and full_trend
        and liquid
        and volatility > 0
    )

    result = {
        "coin": symbol,
        "yonler": trends,
        "rsi15m": round(strength, 2),
        "macd15m": round(
            macd_value - macd_signal, 6
        ),
        "hacim_orani": round(volume_ratio, 2),
        "puan": score,
        "btc_filtre": btc_ok,
        "karar": (
            side + " ADAY"
            if ready
            else "BEKLE"
        )
    }

    if ready:
        risk = 1.5 * volatility

        stop = (
            price - risk
            if side == "LONG"
            else price + risk
        )

        target = (
            price + 1.8 * risk
            if side == "LONG"
            else price - 1.8 * risk
        )

        result.update({
            "giris_referans": price,
            "stop": round(stop, 6),
            "hedef": round(target, 6),
            "risk_odul": 1.8
        })

    return result


def main():
    print("VATAN 2 - 75 PUAN TESTI")
    print(datetime.now(timezone.utc).isoformat())

    errors = 0

    try:
        btc_trends = {
            p: direction(
                candles("BTC-USDT-SWAP", p)
            )
            for p in ["1Dutc", "4H"]
        }

        print("BTC FILTRESI:", btc_trends)

    except Exception as error:
        print("BTC VERI HATASI:", error)
        sys.exit(1)

    for symbol in COINS:
        try:
            result = analyze(symbol, btc_trends)

            print(
                json.dumps(
                    result,
                    ensure_ascii=False
                )
            )

        except Exception as error:
            errors += 1
            print(symbol, "HATA:", error)

    if errors:
        sys.exit(1)

    print("ANALIZ TESTI TAMAMLANDI")


if __name__ == "__main__":
    main()
