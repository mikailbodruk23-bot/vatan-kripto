#!/usr/bin/env python3
"""VATAN 2 public-market risk monitor. Read-only; NEVER places trades.

Feeds: OKX public funding/OI/tickers; BLS release ICS; Federal Reserve FOMC calendar;
US Treasury daily yield XML; optional FRED daily broad dollar index (NOT ICE DXY).
Produces vatan2-risk.json (including rolling public-market snapshots); leaves existing VATAN 2 analyses untouched.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
OKX = "https://www.okx.com"
BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"
FED_CAL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
TREASURY = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
JSON_FILE = Path("son-tum-analiz.json")
OUT_FILE = Path("vatan2-risk.json")
PREVIOUS = {}
# The macro job may run roughly hourly. Do not label irregular samples as 15m moves.
# Snapshots are stored IN vatan2-risk.json, so the existing workflow still publishes one file.
MAX_COMPARISON_MINUTES = 180
HISTORY_RETENTION_MINUTES = 360
HISTORY_MAX_SNAPSHOTS = 12


def utc_now():
    return datetime.now(UTC)


def stamp(v=None):
    return (v or utc_now()).astimezone(UTC).isoformat()


def request_bytes(url, timeout=18):
    req = Request(url, headers={"User-Agent": "VATAN2-Risk-Monitor/1.0 (+public-data)", "Accept": "*/*"})
    with urlopen(req, timeout=timeout) as r:
        data = r.read(2_500_000 + 1)
        if len(data) > 2_500_000:
            raise ValueError("Source exceeded 2.5MB response cap")
        return data


def get_json(url):
    return json.loads(request_bytes(url).decode("utf-8"))


def okx(path, **params):
    url = OKX + path + (("?" + urlencode(params)) if params else "")
    payload = get_json(url)
    if not isinstance(payload, dict) or payload.get("code") != "0" or not isinstance(payload.get("data"), list):
        raise ValueError("OKX invalid response: " + str(payload)[:180])
    return payload["data"]


def number(value):
    try:
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError, OverflowError):
        return None


def event_record(name, start, end, source):
    return {"name": name, "start": stamp(start), "end": stamp(end), "source": source}


def parse_ics_datetime(value, key):
    tz = NY
    if "TZID=" in key:
        value_tz = key.split("TZID=", 1)[1].split(";", 1)[0]
        try:
            tz = ZoneInfo(value_tz)
        except (KeyError, ValueError):
            tz = NY
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    if "T" in value:
        return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=tz)
    return datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=tz)


def bls_events(text):
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    result = []
    current = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current:
                title = current.get("SUMMARY", "").lower()
                kinds = [
                    ("Consumer Price Index", "CPI"),
                    ("Employment Situation", "İstihdam/NFP"),
                    ("Producer Price Index", "PPI"),
                ]
                kind = next((label for tag, label in kinds if tag.lower() in title), None)
                if kind and current.get("DTSTART"):
                    try:
                        begin = parse_ics_datetime(*current["DTSTART"])
                        # Date-only entries are covered for the full date; timed data for +/- 90m.
                        if len(current["DTSTART"][0]) == 8:
                            start = begin
                            end = begin + timedelta(days=1)
                        else:
                            start, end = begin - timedelta(minutes=90), begin + timedelta(minutes=90)
                        result.append(event_record(kind, start, end, BLS_ICS))
                    except (ValueError, KeyError):
                        pass
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            if key.startswith("SUMMARY"):
                current["SUMMARY"] = value.replace("\\,", ",")
            elif key.startswith("DTSTART"):
                current["DTSTART"] = (value, key)
    return result


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.bits = []
    def handle_data(self, data):
        val = " ".join(data.split())
        if val:
            self.bits.append(val)


MONTHS = {name: index for index, name in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}


def fomc_events(html_text):
    p = VisibleText()
    p.feed(html_text)
    bits = p.bits
    events = []
    for year in (utc_now().year, utc_now().year + 1):
        start_idx = next((i for i, s in enumerate(bits) if s == f"{year} FOMC Meetings"), None)
        if start_idx is None:
            continue
        section = bits[start_idx + 1:]
        end_idx = next((i for i, s in enumerate(section) if re.fullmatch(r"\d{4} FOMC Meetings", s)), len(section))
        section = section[:end_idx]
        for i, token in enumerate(section[:-1]):
            month = MONTHS.get(token)
            if not month:
                continue
            found = re.match(r"^(\d{1,2})-(\d{1,2})\*?$", section[i + 1])
            if not found:
                continue
            start_day, end_day = int(found.group(1)), int(found.group(2))
            try:
                beginning = datetime(year, month, start_day, tzinfo=NY)
                ending = datetime(year, month, end_day, 23, 59, 59, tzinfo=NY)
            except ValueError:
                continue
            events.append(event_record("FOMC toplantısı", beginning, ending, FED_CAL))
    return events


def treasury_rates(xml_bytes):
    root = ET.fromstring(xml_bytes)
    rows = []
    for entry in root.iter():
        if entry.tag.split("}")[-1].lower() != "entry":
            continue
        fields = {el.tag.split("}")[-1].upper(): (el.text or "").strip() for el in entry.iter()}
        d = fields.get("NEW_DATE", "")
        y2, y10 = number(fields.get("BC_2YEAR")), number(fields.get("BC_10YEAR"))
        if d and y2 is not None and y10 is not None:
            rows.append({"date": d[:10], "yield_2y_pct": y2, "yield_10y_pct": y10})
    if not rows:
        raise ValueError("Treasury feed has no 2yr/10yr observations")
    rows.sort(key=lambda row: row["date"], reverse=True)
    latest = rows[0]
    if len(rows) > 1:
        previous = rows[1]
        latest["previous_date"] = previous["date"]
        latest["yield_2y_change_bp"] = round((latest["yield_2y_pct"] - previous["yield_2y_pct"]) * 100, 2)
        latest["yield_10y_change_bp"] = round((latest["yield_10y_pct"] - previous["yield_10y_pct"]) * 100, 2)
    date = datetime.fromisoformat(latest["date"]).date()
    latest["fresh_daily"] = (utc_now().date() - date).days <= 5
    latest["source"] = TREASURY
    latest["frequency"] = "daily (NOT realtime)"
    return latest


def broad_dollar_proxy():
    key = os.getenv("FRED_API_KEY", "").strip()
    if not key:
        return {"status": "NOT_CONFIGURED", "name": "Fed broad trade-weighted dollar index (NOT DXY)", "source": "FRED DTWEXBGS", "note": "Optional FRED_API_KEY required"}
    uri = "https://api.stlouisfed.org/fred/series/observations?" + urlencode({
        "series_id": "DTWEXBGS", "api_key": key, "file_type": "json",
        "observation_start": (utc_now() - timedelta(days=30)).date().isoformat(),
        "sort_order": "desc", "limit": "30"})
    payload = get_json(uri)
    obs = [o for o in payload.get("observations", []) if number(o.get("value")) is not None]
    if not obs:
        raise ValueError("FRED did not return usable broad USD observations")
    latest = obs[0]
    age = (utc_now().date() - datetime.fromisoformat(latest["date"]).date()).days
    return {"status": "OK" if age <= 6 else "STALE", "name": "Fed broad trade-weighted dollar index (NOT DXY)",
            "value": number(latest["value"]), "date": latest["date"],
            "change_pct": round((number(obs[0]["value"]) / number(obs[1]["value"]) - 1)*100, 4) if len(obs) > 1 and number(obs[1]["value"]) else None,
            "source": "https://fred.stlouisfed.org/series/DTWEXBGS", "frequency": "daily"}


def symbol_from_coin(row):
    value = str(row.get("coin") or row.get("symbol") or row.get("instId") or "").upper().strip()
    if value.endswith("-USDT-SWAP"):
        return value
    if value.endswith("USDT") and "-" not in value:
        return value[:-4] + "-USDT-SWAP"
    return ""


def select_candidates(data, limit=60):
    rows = data.get("results", [])
    if not isinstance(rows, list):
        raise ValueError("The scan JSON results must be a list")
    eligible = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        coin = symbol_from_coin(row)
        score = number(row.get("puan", row.get("score", 0))) or 0
        if coin:
            eligible.append((int(row.get('likidite_onayi') is True), int(row.get('trend_onayi') is True), score, coin))
    eligible.sort(reverse=True)
    symbols = [coin for _, _, _, coin in eligible[:limit]]
    # BTC, ETH, SOL always included for broad-market diagnostics.
    return list(dict.fromkeys(symbols + ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]))


def floatdiff(current, previous):
    if current is None or previous is None or previous == 0:
        return None
    return round((current / previous - 1) * 100, 3)


def parse_aware_timestamp(value):
    """Return UTC-aware datetime or None for missing/invalid timestamps."""
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def valid_positive(value):
    result = number(value)
    return result if result is not None and result > 0 else None


def clean_snapshot_values(raw):
    """Normalize a compact snapshot: [OI_USD, last_price, OI_base_ccy]."""
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        vals = list(raw[:3])
        while len(vals) < 3:
            vals.append(None)
        oi, price, oi_ccy = vals
    elif isinstance(raw, dict):
        oi, price, oi_ccy = raw.get("oi_usd"), raw.get("last_price"), raw.get("oi_ccy")
    else:
        return None
    oi, price, oi_ccy = valid_positive(oi), valid_positive(price), valid_positive(oi_ccy)
    if oi is None or price is None:
        return None
    return [oi, price, oi_ccy]


def load_market_history(previous, now):
    """Read recent snapshots, or migrate one old-style report if needed."""
    history = []
    raw_entries = previous.get("market_history", []) if isinstance(previous, dict) else []
    if not isinstance(raw_entries, list):
        raw_entries = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        dt = parse_aware_timestamp(entry.get("at"))
        if dt is None or not (0 < (now - dt).total_seconds() <= HISTORY_RETENTION_MINUTES * 60):
            continue
        raw_coins = entry.get("values", {})
        if not isinstance(raw_coins, dict):
            continue
        values = {k: clean_snapshot_values(v) for k, v in raw_coins.items() if isinstance(k, str) and k.endswith("-USDT-SWAP")}
        values = {k: v for k, v in values.items() if v is not None}
        if values:
            history.append({"at": stamp(dt), "values": values})
    # First run after upgrade can compare coins already stored in the previous report.
    if not history and isinstance(previous, dict):
        dt = parse_aware_timestamp(previous.get("updated_at"))
        if dt is not None and 0 < (now - dt).total_seconds() <= HISTORY_RETENTION_MINUTES * 60:
            rows = previous.get("coins", [])
            if isinstance(rows, list):
                values = {}
                for coin in rows:
                    if isinstance(coin, dict):
                        symbol = coin.get("coin")
                        if isinstance(symbol, str) and symbol.endswith("-USDT-SWAP"):
                            v = clean_snapshot_values(coin)
                            if v is not None:
                                values[symbol] = v
                if values:
                    history.append({"at": stamp(dt), "values": values})
    # Avoid reusing duplicate measurements after reruns.
    by_time = {entry["at"]: entry for entry in history}
    return sorted(by_time.values(), key=lambda x: x["at"])[-HISTORY_MAX_SNAPSHOTS:]


def reference_sample(history, symbol, now, target=None, tolerance=None):
    """Use actual observation timestamps, not assumed 5m/15m/60m intervals."""
    candidates = []
    for entry in history:
        dt = parse_aware_timestamp(entry.get("at"))
        age = (now - dt).total_seconds() / 60 if dt else None
        if age is None or not 1 <= age <= MAX_COMPARISON_MINUTES:
            continue
        if target is not None and abs(age - target) > tolerance:
            continue
        v = entry.get("values", {}).get(symbol)
        v = clean_snapshot_values(v)
        if v is not None:
            candidates.append((age, entry["at"], v))
    if not candidates:
        return None
    return (min(candidates, key=lambda x: abs(x[0] - target)) if target is not None
            else min(candidates, key=lambda x: x[0]))


def current_market_snapshot(oi_map, px_map, oi_ccy_map, now):
    values = {}
    for symbol in oi_map.keys() & px_map.keys():
        if isinstance(symbol, str) and symbol.endswith("-USDT-SWAP"):
            v = clean_snapshot_values([oi_map[symbol], px_map[symbol], oi_ccy_map.get(symbol)])
            if v is not None:
                values[symbol] = v
    return {"at": stamp(now), "values": values} if values else None


def assess(funding, oi_usd, last_px, previous):
    alerts = []
    if funding is not None and abs(funding) >= 0.001:
        alerts.append("Aşırı pozitif/negatif fonlama: tek taraflı pozisyon yoğunluğu olabilir")
    oi_change = floatdiff(oi_usd, number(previous.get("oi_usd")))
    px_change = floatdiff(last_px, number(previous.get("last_price")))
    if oi_change is not None and oi_change >= 7:
        alerts.append("Açık faiz önceki ölçüme göre hızla yükseldi")
    if oi_change is not None and px_change is not None and oi_change >= 5 and abs(px_change) >= 1.5:
        alerts.append("Açık faiz ve fiyat aynı dönemde sert değişti: sıkışma olasılığını kontrol et")
    if funding is None or oi_usd is None:
        state = "VERI_EKSIK"
    elif alerts:
        state = "RISK_UYARISI"
    else:
        state = "IZLE"
    return state, alerts, oi_change, px_change


def build_report(now=None):
    global PREVIOUS
    now = now or utc_now()
    report = {"version": 1, "updated_at": stamp(now), "status": "MONITOR_ONLY",
              "note": "This monitor cannot approve leveraged trades or predict liquidations.",
              "auto_trade": False, "orders": False, "dxy": {"status": "NOT_CONNECTED", "note": "ICE DXY licensed realtime feed not connected; broad USD proxy is not DXY."},
              "sources": {}, "macro": {}, "coins": [], "errors": []}

    if OUT_FILE.exists():
        try:
            PREVIOUS = json.loads(OUT_FILE.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            PREVIOUS = {}
    # Read previous observations from the already-committed risk report. A one-hour
    # scheduled workflow is longer than the old 50-minute gate, so that gate was
    # causing permanently-null changes. Keep the ACTUAL interval with each result.
    history = load_market_history(PREVIOUS, now)
    report["market_comparison"] = {
        "max_comparison_minutes": MAX_COMPARISON_MINUTES,
        "history_samples": len(history),
        "note": "Percent changes require a prior observation; OI_USD also moves with price."
    }

    try:
        scan = json.loads(JSON_FILE.read_text("utf-8"))
        if not isinstance(scan, dict) or not scan.get("results"):
            raise ValueError("No results in current scanner JSON")
        last_scan = datetime.fromisoformat(str(scan.get("updated_at", "")).replace("Z", "+00:00"))
        if last_scan.tzinfo is None or (now-last_scan).total_seconds() > 40 * 60 or (now-last_scan).total_seconds() < -120:
            raise ValueError("Existing 525-coin scan stale or has invalid timestamp")
        coins = select_candidates(scan)
        report["analysis_source"] = {"file": str(JSON_FILE), "updated_at": stamp(last_scan), "considered_count": len(scan["results"]), "risk_checked_count": len(coins)}
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        report["errors"].append(f"Underlying scan unavailable/stale: {exc}")
        coins = []
        report["analysis_source"] = {"status": "UNAVAILABLE"}

    try:
        source = okx("/api/v5/public/open-interest", instType="SWAP")
        oi_map = {x.get("instId"): number(x.get("oiUsd")) for x in source}
        oi_ccy_map = {x.get("instId"): number(x.get("oiCcy")) for x in source}
        report["sources"]["okx_open_interest"] = {"status": "OK", "retrieved_at": stamp(now), "symbols": len(oi_map)}
    except Exception as exc:
        oi_map = {}
        oi_ccy_map = {}
        report["errors"].append("OKX OI unavailable: " + str(exc)[:130])
        report["sources"]["okx_open_interest"] = {"status": "UNAVAILABLE"}

    try:
        market = okx("/api/v5/market/tickers", instType="SWAP")
        px_map = {x.get("instId"): number(x.get("last")) for x in market}
        report["sources"]["okx_tickers"] = {"status": "OK", "retrieved_at": stamp(now)}
    except Exception as exc:
        px_map = {}
        report["errors"].append("OKX tickers unavailable: " + str(exc)[:130])
        report["sources"]["okx_tickers"] = {"status": "UNAVAILABLE"}

    report["sources"]["okx_funding"] = {"status": "OK", "retrieved_at": stamp(now), "ok_count": 0, "requested": len(coins)}
    for coin in coins:
        funding = None
        next_funding = None
        try:
            row = okx("/api/v5/public/funding-rate", instId=coin)[0]
            funding = number(row.get("fundingRate"))
            next_funding = row.get("fundingTime")
            if funding is not None:
                report["sources"]["okx_funding"]["ok_count"] += 1
        except Exception as exc:
            report["errors"].append(f"Funding {coin}: {str(exc)[:100]}")
        oi = oi_map.get(coin)
        if oi is not None and oi <= 0:
            oi = None
        price = px_map.get(coin)
        ref = reference_sample(history, coin, now)
        old_values = {"oi_usd": ref[2][0], "last_price": ref[2][1]} if ref else {}
        status, alerts, oi_change, px_change = assess(funding, oi, price, old_values)
        oi_ccy = valid_positive(oi_ccy_map.get(coin))
        ccy_change = floatdiff(oi_ccy, ref[2][2]) if ref else None
        # USD open interest moves with price too; base-asset OI is an additional
        # diagnostic, NOT a stand-alone trade signal.
        changes = {}
        for label, target, tolerance in (("5m", 5, 3), ("15m", 15, 6), ("60m", 60, 12)):
            interval_ref = reference_sample(history, coin, now, target, tolerance)
            changes[f"oi_change_{label}_pct"] = (floatdiff(oi, interval_ref[2][0]) if interval_ref else None)
            changes[f"price_change_{label}_pct"] = (floatdiff(price, interval_ref[2][1]) if interval_ref else None)
        report["coins"].append({"coin": coin, "status": status, "funding_rate": funding,
                                "funding_pct": round(funding * 100, 5) if funding is not None else None,
                                "next_funding_ms": next_funding, "oi_usd": oi, "oi_change_pct": oi_change,
                                "oi_ccy": oi_ccy, "oi_ccy_change_pct": ccy_change,
                                "last_price": price, "last_price_change_pct": px_change,
                                "change_reference_at": ref[1] if ref else None,
                                "change_period_minutes": round(ref[0], 1) if ref else None,
                                "comparison_status": "OK" if ref else "REFERANS_YOK",
                                **changes, "warnings": alerts})
        time.sleep(0.12)
    if coins and report["sources"]["okx_funding"]["ok_count"] == 0:
        report["sources"]["okx_funding"]["status"] = "UNAVAILABLE"
    if not coins:
        report["sources"]["okx_funding"]["status"] = "NOT_RUN"

    # Preserve snapshots for ALL USDT swaps, not only the top ~60 currently checked.
    # This provides a baseline when the top-list membership changes next run.
    snap = current_market_snapshot(oi_map, px_map, oi_ccy_map, now)
    if snap is not None:
        history.append(snap)
    report["market_history"] = history[-HISTORY_MAX_SNAPSHOTS:]
    report["market_comparison"]["current_symbols"] = len(snap["values"]) if snap else 0
    report["market_comparison"]["comparable_coins"] = sum(c["comparison_status"] == "OK" for c in report["coins"])

    all_events = []
    try:
        all_events += bls_events(request_bytes(BLS_ICS).decode("utf-8-sig"))
        report["sources"]["bls_calendar"] = {"status": "OK", "retrieved_at": stamp(now), "url": BLS_ICS}
    except Exception as exc:
        report["errors"].append("BLS calendar unavailable: " + str(exc)[:110])
        report["sources"]["bls_calendar"] = {"status": "UNAVAILABLE"}
    try:
        fomc = fomc_events(request_bytes(FED_CAL).decode("utf-8"))
        if not fomc:
            raise ValueError("could not parse future FOMC meetings")
        all_events += fomc
        report["sources"]["fed_calendar"] = {"status": "OK", "retrieved_at": stamp(now), "url": FED_CAL}
    except Exception as exc:
        report["errors"].append("FOMC calendar unavailable: " + str(exc)[:110])
        report["sources"]["fed_calendar"] = {"status": "UNAVAILABLE"}

    upcoming = []
    active = []
    for event in all_events:
        start, end = datetime.fromisoformat(event["start"]), datetime.fromisoformat(event["end"])
        if end < now - timedelta(hours=3):
            continue
        if start <= now <= end:
            active.append(event)
        elif now < start <= now + timedelta(days=45):
            upcoming.append(event)
    upcoming.sort(key=lambda x: x["start"])
    if not all(report["sources"].get(name, {}).get("status") == "OK" for name in ("fed_calendar", "bls_calendar")):
        macro_gate = "TAKVIM_EKSIK"
    elif active:
        macro_gate = "ETKINLIK_RISKI"
    else:
        macro_gate = "OLAY_YOK"  # not the same as trade approval
    report["macro"]["event_gate"] = macro_gate
    report["macro"]["active_events"] = active[:10]
    report["macro"]["upcoming_events"] = upcoming[:15]

    try:
        at_ny = now.astimezone(NY)
        monthly = at_ny.strftime("%Y%m")
        url = TREASURY + "?" + urlencode({"data": "daily_treasury_yield_curve", "field_tdr_date_value_month": monthly})
        data = treasury_rates(request_bytes(url))
        report["macro"]["treasury"] = data
        report["sources"]["us_treasury"] = {"status": "OK" if data["fresh_daily"] else "STALE", "retrieved_at": stamp(now)}
    except Exception as exc:
        report["macro"]["treasury"] = {"status": "UNAVAILABLE"}
        report["sources"]["us_treasury"] = {"status": "UNAVAILABLE"}
        report["errors"].append("US Treasury unavailable: " + str(exc)[:120])

    try:
        report["macro"]["broad_usd_proxy"] = broad_dollar_proxy()
    except Exception as exc:
        report["errors"].append("FRED broad dollar unavailable: " + str(exc)[:120])
        report["macro"]["broad_usd_proxy"] = {"status": "UNAVAILABLE"}

    # A monitoring addon can ONLY narrow eligibility. It cannot issue new LONG/SHORT/ONAY signals.
    if macro_gate != "OLAY_YOK":
        report["global_risk"] = "BEKLE"
    elif report["analysis_source"].get("status") == "UNAVAILABLE" or not coins or report["sources"]["okx_funding"].get("status") != "OK":
        report["global_risk"] = "VERI_EKSIK"
    elif any(report["sources"].get(k, {}).get("status") != "OK" for k in ("okx_open_interest", "okx_tickers", "us_treasury")):
        report["global_risk"] = "VERI_EKSIK"
    else:
        report["global_risk"] = "KONTROL_ET"  # not an approval
    return report


def self_test():
    sample = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Consumer Price Index for Test\r\nDTSTART;TZID=America/New_York:20261110T083000\r\nEND:VEVENT\r\nEND:VCALENDAR"
    assert len(bls_events(sample)) == 1
    assert abs((datetime.fromisoformat(bls_events(sample)[0]["start"]) - datetime(2026, 11, 10, 12, 0, tzinfo=UTC)).total_seconds()) < 2
    assert symbol_from_coin({"coin": "BTC-USDT-SWAP"}) == "BTC-USDT-SWAP"
    assert select_candidates({"results": [{"coin": "CAP-USDT-SWAP", "puan": 95}]})[0] == "CAP-USDT-SWAP"
    assert assess(0.002, 150, 80, {"oi_usd": 100, "last_price": 82})[0] == "RISK_UYARISI"
    assert assess(None, 100, 80, {})[0] == "VERI_EKSIK"
    html = "<h3>2026 FOMC Meetings</h3><div>December</div><div>8-9*</div><h3>2027 FOMC Meetings</h3><div>January</div><div>26-27</div>"
    assert len(fomc_events(html)) >= 1
    t0 = datetime(2026, 11, 5, 17, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=61)
    first = {"updated_at": stamp(t0), "coins": [{"coin": "BTC-USDT-SWAP", "oi_usd": 100, "last_price": 80}]}
    h = load_market_history(first, t1)
    assert len(h) == 1 and reference_sample(h, "BTC-USDT-SWAP", t1)[0] == 61
    assert floatdiff(110, reference_sample(h, "BTC-USDT-SWAP", t1)[2][0]) == 10.0
    assert reference_sample(h, "BTC-USDT-SWAP", t1 + timedelta(hours=4)) is None
    assert reference_sample(h, "ETH-USDT-SWAP", t1) is None
    s = current_market_snapshot({"ETH-USDT-SWAP": 200}, {"ETH-USDT-SWAP": 20}, {}, t1)
    assert s and s["values"]["ETH-USDT-SWAP"][:2] == [200, 20]
    assert len(load_market_history({"market_history": [s]}, t1 + timedelta(minutes=61))) == 1
    print("SELF_TEST_OK: parsers, OI history, comparisons, stale guards, symbol normalization")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    report = build_report()
    OUT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print("RISK_REPORT", json.dumps({"updated_at": report["updated_at"], "risk": report["global_risk"],
                                      "coins": len(report["coins"]), "errors": len(report["errors"]),
                                      "calendar_gate": report["macro"]["event_gate"]}, ensure_ascii=False))
    # No hard failure on partial providers: report is explicitly missing/unknown. Avoid false green.


if __name__ == "__main__":
    main()
