#!/usr/bin/env python3
"""VATAN 2 - LONG/SHORT teknik + vadeli + makro puan birlestirici.

Var olan son-tum-analiz.json ve vatan2-risk.json dosyalarini OKUR,
bunlara dokunmaz; son-makro60.json olusturur. Otomatik emir yok.
60/100 yon adayi esigidir; 85/100 gercek kazanma olasiligi degildir.
Puanlar kazanma olasiligi/yuzdesi degildir.
Yeni risk motorunun gercek sureli OI ve fiyat degisimlerini kontrol eder;
veri eksikse veya anomaliyse yeni LONG/SHORT islem onayi vermez.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path

from vatan2_core import expected_net_rr

SOURCE = Path('son-tum-analiz.json')
RISK = Path('vatan2-risk.json')
OUT = Path('son-makro60.json')
WEIGHTS = {'1Dutc': 20, '4H': 20, '1H': 15, '15m': 15}
CANDIDATE_THRESHOLD = 60
APPROVAL_THRESHOLD = 85
MAX_NET_EFFECT = 15
MAX_AGE_SECONDS = 35 * 60


def num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError, OverflowError):
        return None


def clamp(v, low=0, high=100):
    return max(low, min(high, v))


def parse_time(t):
    try:
        d = datetime.fromisoformat(str(t).replace('Z', '+00:00'))
        return d.astimezone(timezone.utc) if d.tzinfo else None
    except (ValueError, TypeError):
        return None


def freshness(text, now, seconds=MAX_AGE_SECONDS):
    t = parse_time(text)
    return t is not None and -120 <= (now - t).total_seconds() <= seconds


def same_instant(a, b, tolerance_seconds=2):
    """Two ISO timestamps may use different but equivalent UTC representations."""
    t1, t2 = parse_time(a), parse_time(b)
    return t1 is not None and t2 is not None and abs((t1 - t2).total_seconds()) <= tolerance_seconds


def clear_old_trade_levels(row):
    """Remove obsolete or unsafe numeric levels when no valid 85+ plan exists."""
    for key in ('giris_referans', 'stop', 'hedef', 'risk_odul', 'hedef1', 'hedef2', 'pozisyon_plani'):
        row.pop(key, None)


def technical_scores(row):
    if row.get('durum') != 'ANALIZ_EDILDI' or not isinstance(row.get('yonler'), dict):
        return None
    trends = row['yonler']
    rsi, macd, vol = num(row.get('rsi15m')), num(row.get('macd15m')), num(row.get('hacim_orani'))
    if any(v is None for v in (rsi, macd, vol)):
        return None
    output = {}
    for side in ('LONG', 'SHORT'):
        s = sum(w for p, w in WEIGHTS.items() if trends.get(p) == side)
        if (side == 'LONG' and 50 <= rsi <= 70) or (side == 'SHORT' and 30 <= rsi <= 50):
            s += 10
        if (side == 'LONG' and macd > 0) or (side == 'SHORT' and macd < 0):
            s += 10
        s += 10 if vol >= 1.3 else 5 if vol >= 1 else 0
        output[side] = int(clamp(s))
    return output


def macro_impact(report):
    """Macro data only votes on direction if a *fresh change* is known.
    Event times increase risk in both directions; missing sources = no approval.
    Nominal Treasury yields do NOT imply bullish/bearish direction.
    """
    shifts = {'LONG': 0, 'SHORT': 0}
    notes = []
    macro = report.get('macro') or {}
    proxy = macro.get('broad_usd_proxy') or {}
    chg = num(proxy.get('change_pct'))
    if proxy.get('status') == 'OK' and chg is not None:
        if chg >= 0.35:
            shifts['LONG'] -= 5
            shifts['SHORT'] += 5
            notes.append('Geniş dolar endeksi yükseliyor (DXY değil): SHORT lehine +5')
        elif chg <= -0.35:
            shifts['LONG'] += 5
            shifts['SHORT'] -= 5
            notes.append('Geniş dolar endeksi düşüyor (DXY değil): LONG lehine +5')
        else:
            notes.append('Dolar endeksinde belirgin günlük değişim yok')
    else:
        notes.append('Doğrulanmış dolar eğilimi yok: yön puanı değiştirilmedi')
    # Score only verified day-over-day daily Treasury changes; never infer trend from yield levels.
    treasury = macro.get('treasury') or {}
    b2 = num(treasury.get('yield_2y_change_bp'))
    b10 = num(treasury.get('yield_10y_change_bp'))
    if treasury.get('fresh_daily') is True and b2 is not None and b10 is not None:
        if b2 >= 8 and b10 >= 8:
            shifts['LONG'] -= 4
            shifts['SHORT'] += 4
            notes.append('ABD 2Y ve 10Y getiriler aynı gün +8bp üzeri: LONG -4 / SHORT +4')
        elif b2 <= -8 and b10 <= -8:
            shifts['LONG'] += 4
            shifts['SHORT'] -= 4
            notes.append('ABD 2Y ve 10Y getiriler aynı gün -8bp altı: LONG +4 / SHORT -4')
        else:
            notes.append('Tahvil getirileri nötr veya ayrışmış: yön etkisi 0')
    else:
        notes.append('ABD tahvil değişim verisi eksik/eski: yön etkisi 0')
    return shifts, notes


def derivatives_impact(rec):
    """Conservative, *time-aware* derivatives adjustment.

    oi_usd is a notional value and can rise merely because price rises.
    Prefer measured oi_ccy change, otherwise estimate quantity change after
    removing the price effect. Never equate OI growth to guaranteed direction.

    Returns (score shifts, explanatory notes, safe-for-approval, data status).
    """
    shifts = {'LONG': 0, 'SHORT': 0}
    reasons = []
    if not isinstance(rec, dict):
        return shifts, ['Vadeli veri kaydı yok'], False, 'VERI_EKSIK'

    rate = num(rec.get('funding_rate'))
    oi_usd = num(rec.get('oi_usd'))
    last_price = num(rec.get('last_price'))
    oi_pct = num(rec.get('oi_change_pct'))
    px_pct = num(rec.get('last_price_change_pct'))
    ccy_pct = num(rec.get('oi_ccy_change_pct'))
    interval = num(rec.get('change_period_minutes'))
    monitor_state = rec.get('status')

    if rate is None or oi_usd is None or oi_usd <= 0 or last_price is None or last_price <= 0:
        return shifts, ['Funding, açık faiz veya son fiyat eksik: onay engellendi'], False, 'VERI_EKSIK'

    # Funding's direction-only effect remains deliberately small and never
    # creates an approval on its own.
    if rate >= 0.001:
        shifts['LONG'] -= 6
        reasons.append('Yüksek pozitif funding: kalabalık LONG riski; LONG -6')
    elif rate <= -0.001:
        shifts['SHORT'] -= 6
        reasons.append('Yüksek negatif funding: kalabalık SHORT riski; SHORT -6')

    if rec.get('comparison_status') != 'OK' or interval is None or oi_pct is None or px_pct is None:
        reasons.append('Önceki doğrulanmış OI/fiyat ölçümü yok: yön teyidi yapılamadı')
        return shifts, reasons, False, 'REFERANS_YOK'
    if not 5 <= interval <= 90:
        reasons.append(f'Ölçümler arası {interval:.1f} dk: kısa vadeli teyit için uygun değil')
        return shifts, reasons, False, 'ARALIK_UYGUN_DEGIL'

    # Abrupt OI shocks, extreme price moves, or inconsistent amount/notional
    # combinations require observation rather than automated scoring.
    if (abs(oi_pct) >= 30 or abs(px_pct) >= 18 or
            (ccy_pct is not None and abs(ccy_pct) >= 30)):
        reasons.append(f'Olağan dışı hareket ({interval:.1f} dk): OI {oi_pct:+.2f}%, fiyat {px_pct:+.2f}%; onay bloke')
        return shifts, reasons, False, 'ANOMALI'

    if ccy_pct is not None:
        implied_notional_pct = ((1 + ccy_pct / 100) * (1 + px_pct / 100) - 1) * 100
        if abs(implied_notional_pct - oi_pct) > 15:
            reasons.append('USD açık faiz ile baz varlık miktarı/fiyat değişimi tutarsız: onay bloke')
            return shifts, reasons, False, 'TUTARSIZ_VERI'
        quantity_change = ccy_pct
        reasons.append(f'Baz varlık OI değişimi {ccy_pct:+.2f}% ({interval:.1f} dk)')
        quantity_confirmed = True
    elif px_pct > -99:
        quantity_change = ((1 + oi_pct / 100) / (1 + px_pct / 100) - 1) * 100
        reasons.append(f'OI miktarı yaklaşık {quantity_change:+.2f}% (fiyat etkisi çıkarıldı; teyitsiz)')
        quantity_confirmed = False
    else:
        reasons.append('Fiyat değişimi geçersiz: OI miktarı türetilemedi')
        return shifts, reasons, False, 'TUTARSIZ_VERI'

    if quantity_change >= 3 and px_pct >= 0.6:
        shifts['LONG'] += 4 if quantity_confirmed else 2
        shifts['SHORT'] -= 3 if quantity_confirmed else 1
        reasons.append('OI miktarı ve fiyat yükseliyor: temkinli LONG yön katkısı')
    elif quantity_change >= 3 and px_pct <= -0.6:
        shifts['SHORT'] += 4 if quantity_confirmed else 2
        shifts['LONG'] -= 3 if quantity_confirmed else 1
        reasons.append('OI miktarı yükselirken fiyat düşüyor: temkinli SHORT yön katkısı')
    elif quantity_change <= -8:
        shifts['LONG'] -= 2
        shifts['SHORT'] -= 2
        reasons.append('Pozisyonlar hızla kapanıyor olabilir: iki yöne -2')
    else:
        reasons.append(f'Fiyat/OI eşleşmesinden güçlü yön teyidi yok ({interval:.1f} dk)')

    if monitor_state != 'IZLE' or rec.get('warnings'):
        reasons.append('Risk motoru uyarısı var: işlem adayı onaylanmadı')
        return shifts, reasons, False, 'RISK_UYARISI'
    if not quantity_confirmed:
        reasons.append('OI baz miktarının geçmiş değişimi eksik: onay için ikinci ölçüm gerekli')
        return shifts, reasons, False, 'MIKTAR_TEYITSIZ'
    return shifts, reasons, True, 'DOGRULANDI'


# VATAN 2 v3: only the final decision labels / safety gates change.
# The user's 60/85 scores and the existing scoring formulas remain intact.
MAJORS = {'BTCUSDT', 'ETHUSDT', 'SOLUSDT'}
SPREAD_HEALTHY = {'major': 0.02, 'alt': 0.08}  # percent
SPREAD_RISK = {'major': 0.04, 'alt': 0.12}     # percent, configurable starting limits
FUNDING_LONG_RISK_8H_PCT = 0.05
FUNDING_SHORT_RISK_8H_PCT = -0.05
HARD_DERIVATIVE_STATES = {'RISK_UYARISI', 'ANOMALI', 'TUTARSIZ_VERI', 'ARALIK_UYGUN_DEGIL'}
MISSING_DERIVATIVE_STATES = {'VERI_EKSIK', 'REFERANS_YOK', 'KAPSAM_DISI', 'MIKTAR_TEYITSIZ'}


def spread_limits(coin):
    category = 'major' if coin in MAJORS else 'alt'
    return SPREAD_HEALTHY[category], SPREAD_RISK[category]


def trade_plan_valid(row, side):
    entry, stop, target, rr = (num(row.get(k)) for k in ('giris_referans', 'stop', 'hedef', 'risk_odul'))
    if None in (entry, stop, target, rr) or min(entry, stop, target) <= 0 or rr < 1.5:
        return False
    return (stop < entry < target) if side == 'LONG' else (target < entry < stop)


def derive_decision(row, side, score, gap, global_checks, derivative_status, deriv, derivative_ok):
    """Return (single badge, reasons list, blockers list, confidence gate).

    High-scoring blocked setups must never look like approved trades.
    Inadequate data is not a market setup; mark it explicitly as missing.
    """
    notes = []
    blockers = []
    score = num(score)
    if score is None or row.get('durum') != 'ANALIZ_EDILDI':
        return 'VERİ EKSİK', ['Kapanmış mum/teknik veri alınamadı'], ['TEKNIK_VERI'], False
    # Stale or incomplete feeds are unsafe regardless of the numeric score.
    if not global_checks['scan_fresh'] or not global_checks['risk_fresh'] or not global_checks['same_scan']:
        return 'VERİ EKSİK', ['Puanlama verisi güncel veya senkronize değil'], ['VERI_GUNCELLIGI'], False
    if not global_checks['market_feeds_ok'] or not global_checks['treasury_ok'] or not global_checks['calendar_sources_ok'] or global_checks['global_risk'] == 'VERI_EKSIK':
        return 'VERİ EKSİK', ['Genel piyasa veya resmî ekonomi kaynağı doğrulanamadı'], ['GENEL_VERI_EKSIK'], False
    if score < CANDIDATE_THRESHOLD or gap < 8:
        return 'BEKLE', ['Yön kurulumu henüz yeterince net değil'], ['YON_ZAYIF'], False

    # A missing critical market input cannot be replaced by a high technical score.
    missing = []
    if not global_checks['scan_fresh'] or not global_checks['risk_fresh'] or not global_checks['same_scan']:
        missing.append('Tarama veya risk raporu güncel/uyumlu değil')
    if not global_checks['market_feeds_ok']:
        missing.append('Binance vadeli piyasa kaynağı eksik')
    if not global_checks['treasury_ok']:
        missing.append('Makro veri doğrulaması eksik')
    if not global_checks['calendar_sources_ok']:
        missing.append('Resmî ekonomi takvimi verisi eksik')
    if global_checks['global_risk'] == 'VERI_EKSIK':
        missing.append('Genel risk motoru veri eksiği')
    if derivative_status in MISSING_DERIVATIVE_STATES or not isinstance(deriv, dict):
        missing.append('Açık faiz/funding karşılaştırması eksik')
    rate_8h = num((deriv or {}).get('funding_8h_pct'))
    hours = num((deriv or {}).get('funding_interval_hours'))
    if rate_8h is None or hours is None:
        missing.append('Funding aralığı ve 8 saatlik eşdeğer doğrulanamadı')
    spread = num(row.get('spread_yuzde'))
    if spread is None or spread < 0:
        missing.append('Alış-satış spread verisi eksik')
    if num(row.get('mum_15m_ts')) is None:
        missing.append('15 dakikalık kapanmış mum zamanı eksik')
    if row.get('piyasa_rejimi') not in ('TREND','YATAY','GECIS') or not isinstance(row.get('dun_seviyeleri'), dict):
        missing.append('Kapanmış mumlarla fiyat yapısı / dünün seviyeleri eksik')

    if num(row.get('adx1h')) is None or num(row.get('chop1h')) is None:
        missing.append('ADX/CHOP rejim doğrulaması eksik')
    if missing:
        return 'VERİ EKSİK', missing[:5], ['VERI_EKSIK'], False

    healthy, spread_risk = spread_limits(row.get('coin'))
    if spread >= spread_risk:
        blockers.append('SPREAD')
        notes.append(f'Spread %{spread:.4f}: risk sınırı %{spread_risk:.2f}')
    elif spread >= healthy:
        notes.append(f'Spread %{spread:.4f}: normal bandın üzerinde, izleniyor')
    next_funding_ms = num((deriv or {}).get('next_funding_ms'))
    if next_funding_ms is None:
        blockers.append('FUNDING_SAATI_VERI')
        notes.append('Funding saati doğrulanamadı')
    else:
        seconds_until = (next_funding_ms - global_checks['now_ms']) / 1000.0
        if -90 <= seconds_until <= 180:
            blockers.append('FUNDING_SAATI')
            notes.append('Funding ödemesine 3 dakika veya daha az kaldı / yeni tamamlandı')
    if side == 'LONG' and rate_8h >= FUNDING_LONG_RISK_8H_PCT:
        blockers.append('FUNDING')
        notes.append(f'LONG funding +%{rate_8h:.4f} (8s): maliyet/sıkışma riski')
    if side == 'SHORT' and rate_8h <= FUNDING_SHORT_RISK_8H_PCT:
        blockers.append('FUNDING')
        notes.append(f'SHORT funding %{rate_8h:.4f} (8s): maliyet/sıkışma riski')
    # Clean-price confirmation: never treat a sweep as proven stop liquidation.
    sweep = row.get('likidite_supurmesi')
    if (sweep == 'TEPE_IGNE_SHORT' and side == 'LONG') or (sweep == 'DIP_IGNE_LONG' and side == 'SHORT'):
        blockers.append('TERS_IGNE')
        notes.append('Dünün tepe/dip iğnesi bu işlem yönüne karşı; kapanış teyidi bekleniyor')
    adx_1h, chop_1h = num(row.get('adx1h')), num(row.get('chop1h'))
    if row.get('piyasa_rejimi') != 'TREND' or adx_1h < 25 or chop_1h >= 61.8:
        blockers.append('REJIM')
        notes.append('Trend rejimi güçlü değil: ADX/CHOP teyidi bekleniyor')
    if row.get('range_orta_riski') is True:
        blockers.append('RANGE_ORTASI')
        notes.append('Fiyat yatay bandın ortasında: yeni işlem için uygun konum değil')
    elif row.get('piyasa_rejimi') == 'YATAY':
        notes.append('Yatay piyasa: sadece bandın sınırındaki teyitleri izle')
    if derivative_status in HARD_DERIVATIVE_STATES or (deriv or {}).get('warnings'):
        blockers.append('VADELI_RISK')
        notes.append('Açık faiz/fiyat ya da risk motoru olağan dışı durum bildirdi')
    if global_checks['global_risk'] not in ('KONTROL_ET',):
        blockers.append('MAKRO')
        notes.append('Genel ekonomik risk motoru işlem için uygun değil')
    if not global_checks['no_event']:
        blockers.append('MAKRO_OLAY')
        notes.append('Kritik ekonomi olayı / açıklama riski')
    if row.get('trend_onayi') is not True or not all((row.get('yonler') or {}).get(p) == side for p in WEIGHTS):
        blockers.append('TREND')
        notes.append('Dört zaman dilimi aynı yönde değil')
    if row.get('btc_filtre') is not True:
        blockers.append('BTC')
        notes.append('BTC günlük ve dört saatlik yön onayı yok')
    if row.get('likidite_onayi') is not True:
        blockers.append('LIKIDITE')
        notes.append('Hacim veya derinlik temel likidite filtresini geçemedi')
    volume_ratio = num(row.get('hacim_orani'))
    if volume_ratio is None or volume_ratio < 1.3:
        blockers.append('HACIM')
        notes.append('15 dakikalık hacim tetikleyicisi 1,3x altında')

    if score >= APPROVAL_THRESHOLD:
        if gap < 12:
            blockers.append('YON_AYRISMASI')
            notes.append('LONG ve SHORT puanları arasında yeterli fark yok')
        if trade_plan_valid(row, side):
            net_rr = expected_net_rr(num(row.get('giris_referans')), num(row.get('stop')),
                                     num(row.get('hedef')), side)
            if net_rr is None or net_rr < 1.5:
                blockers.append('NET_RISK_ODUL')
                notes.append('Komisyon ve kayma tahmini sonrası risk/ödül 1,5 altında')
        if not trade_plan_valid(row, side):
            # Do not invent trade levels; if all else is ready, block as missing.
            if not blockers:
                return 'VERİ EKSİK', ['Geçerli giriş/stop/hedef veya en az 1,5 risk/ödül hesabı yok'], ['ISLEM_PLANI'], False
            notes.append('Geçerli işlem seviyesi henüz yok')
        if blockers or not derivative_ok:
            if not derivative_ok and not blockers:
                blockers.append('VADELI_ONAY')
                notes.append('Vadeli piyasa teyidi henüz tamamlanmadı')
            return 'RİSKLİ BEKLE', notes[:5] or ['Onay filtreleri tamamlanmadı'], blockers, False
        return side, ['Tüm analiz ve güvenlik koşulları tamamlandı'], [], True

    # 60–74: 15m and 1.3x volume are LAST triggers, not mandatory before ADAY.
    # 1D+4H+1H and BTC+liquidity remain mandatory to prevent weak candidates.
    trends = row.get('yonler') or {}
    long_horizon = all(trends.get(p) == side for p in ('1Dutc', '4H', '1H'))
    hard = [b for b in blockers if b not in ('TREND', 'HACIM')]
    if hard or not long_horizon:
        return 'BEKLE', notes[:4] or ['Yön ve güvenlik kontrolleri bekleniyor'], blockers, False
    pending = ['PUAN_85']
    if trends.get('15m') != side:
        pending.append('15DK_TETIK')
    if volume_ratio is None or volume_ratio < 1.3:
        pending.append('HACIM_TETIK')
    return 'ADAY ' + side, [f'{side} ana trend uygun; giriş teyidi bekleniyor'] + notes[:2], pending, False


def make_report(scan, risk, now=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(scan.get('results'), list):
        raise ValueError('Tarama listesi yok')
    scan_fresh = freshness(scan.get('updated_at'), now)
    if scan.get('source') != 'BINANCE_UM' or scan.get('market') != 'USDT-M-PERPETUAL' or risk.get('source') != 'BINANCE_UM':
        raise ValueError('Binance ile eski OKX raporu birlestirilemez')
    risk_fresh = freshness(risk.get('updated_at'), now)
    same_scan = same_instant(scan.get('updated_at'), (risk.get('analysis_source') or {}).get('updated_at'))
    sources = risk.get('sources') or {}
    macro = risk.get('macro') or {}
    calendar_sources_ok = all((sources.get(k) or {}).get('status') == 'OK' for k in ('bls_calendar', 'fed_calendar'))
    calendar_ok = calendar_sources_ok and macro.get('event_gate') == 'OLAY_YOK'
    no_event = calendar_ok and not (macro.get('active_events') or [])
    treasury_ok = (sources.get('us_treasury') or {}).get('status') == 'OK' and (macro.get('treasury') or {}).get('fresh_daily') is True
    market_feeds_ok = all((sources.get(k) or {}).get('status') == 'OK' for k in ('binance_open_interest', 'binance_tickers', 'binance_funding'))
    risk_gate_ok = risk.get('global_risk') == 'KONTROL_ET'
    global_ok = scan_fresh and risk_fresh and same_scan and no_event and treasury_ok and market_feeds_ok and risk_gate_ok
    checks = dict(scan_fresh=scan_fresh, risk_fresh=risk_fresh, same_scan=same_scan,
                  now_ms=int(now.timestamp()*1000), market_feeds_ok=market_feeds_ok, treasury_ok=treasury_ok,
                  calendar_sources_ok=calendar_sources_ok, no_event=no_event,
                  global_risk=risk.get('global_risk'))
    overall_notes = []
    if not scan_fresh: overall_notes.append('Coin taraması eski')
    if not risk_fresh: overall_notes.append('Ekonomik rapor eski')
    if not same_scan: overall_notes.append('Ekonomik rapor başka taramaya ait')
    if not calendar_sources_ok: overall_notes.append('Resmî takvim kaynağı eksik')
    elif not no_event: overall_notes.append('Kritik ekonomi olayı var')
    if not treasury_ok: overall_notes.append('Tahvil verisi doğrulanmamış/eski')
    if not market_feeds_ok: overall_notes.append('Binance vadeli piyasa kaynağı eksik')
    if not risk_gate_ok: overall_notes.append('Risk motorunun genel güvenlik kapısı uygun değil')
    mshift, mnotes = macro_impact(risk) if risk_fresh and same_scan else ({'LONG':0,'SHORT':0},['Makro rapor eski/uyumsuz; puana katılmadı'])
    by_coin = {c.get('coin'):c for c in risk.get('coins',[]) if isinstance(c,dict)}
    rows = []
    for r in scan['results']:
        if not isinstance(r, dict):
            continue
        row = dict(r)
        tech = technical_scores(r)
        if tech is None:
            clear_old_trade_levels(row)
            row.update(long_puani=None, short_puani=None, yon_esigi=60, yon_adayi='YOK',
                       makro_etki={'LONG':0,'SHORT':0}, makro_notlari=['Teknik veri eksik'],
                       makro_durumu='VERI_EKSIK', karar='VERİ EKSİK', puan=None,
                       karar_gerekcesi='Kapanmış mum/teknik veri alınamadı',
                       eksik_sartlar=['TEKNIK_VERI'], onay_sartlari_kalan=99,
                       risk_nedenleri=[], makro_onay=False, risk_freni=True,
                       plan_durumu='GIZLI', plan_yonu=None, plan_kaynak_zamani=None)
            rows.append(row)
            continue
        deriv = by_coin.get(r.get('coin')) if risk_fresh and same_scan else None
        if deriv is not None:
            dshift, dnotes, derivative_ok, derivative_status = derivatives_impact(deriv)
        else:
            dshift, dnotes, derivative_ok, derivative_status = {'LONG':0,'SHORT':0}, ['Bu coin için güncel funding/OI kontrolü yok'], False, 'KAPSAM_DISI'
        net = {side: int(clamp(dshift[side]+mshift[side], -MAX_NET_EFFECT, MAX_NET_EFFECT)) for side in ('LONG','SHORT')}
        combined = {side: int(clamp(tech[side]+net[side])) for side in ('LONG','SHORT')}
        side = 'LONG' if combined['LONG'] >= combined['SHORT'] else 'SHORT'
        gap = combined[side] - combined['SHORT' if side=='LONG' else 'LONG']
        decision, notes, blockers, approved = derive_decision(row, side, combined[side], gap, checks, derivative_status, deriv, derivative_ok)
        # Visibility is score-based; execution approval remains independent.
        # The scanner produces closed-candle levels. On 85+ without approval they
        # are shown ONLY as a clearly labelled draft (never as a live signal).
        # Do not invent numbers or retain stale/invalid stop and target levels.
        has_valid_plan = combined[side] >= APPROVAL_THRESHOLD and trade_plan_valid(row, side)
        if not has_valid_plan:
            clear_old_trade_levels(row)
        else:
            entry, stop = num(row.get('giris_referans')), num(row.get('stop'))
            risk_distance = abs(entry - stop)
            step = risk_distance if side == 'LONG' else -risk_distance
            row['hedef1'] = round(entry + step, 10)  # 1R level, indicative
            row['hedef2'] = row.get('hedef')
            row['pozisyon_plani'] = ('Hedef 1 (1R): pozisyonun %50’sini kapatmak değerlendirilebilir. '
                                      'Stopu yalnızca net maliyetler gözetilerek yönet; zarara ekleme yapma.')
        row['plan_durumu'] = ('ONAYLI' if approved and has_valid_plan else
                              'TASLAK' if has_valid_plan else 'GIZLI')
        row['plan_yonu'] = side if has_valid_plan else None
        row['plan_kaynak_zamani'] = scan.get('updated_at') if has_valid_plan else None
        if approved:
            status = 'GUNCEL'
        elif decision == 'VERİ EKSİK':
            status = 'VERI_EKSIK'
        elif decision == 'RİSKLİ BEKLE':
            status = 'RISK_UYARISI'
        else:
            status = 'FILTRE_BEKLE'
        row.update(teknik_long=tech['LONG'], teknik_short=tech['SHORT'],
                   long_puani=combined['LONG'], short_puani=combined['SHORT'],
                   makro_etki=net, makro_notlari=mnotes+dnotes,
                   vadeli_veri_durumu=derivative_status,
                   oi_degisim_yuzde=deriv.get('oi_change_pct') if deriv else None,
                   oi_miktar_degisim_yuzde=deriv.get('oi_ccy_change_pct') if deriv else None,
                   fiyat_degisim_yuzde=deriv.get('last_price_change_pct') if deriv else None,
                   olcum_araligi_dakika=deriv.get('change_period_minutes') if deriv else None,
                   funding_orani=deriv.get('funding_rate') if deriv else None,
                   funding_8s_yuzde=deriv.get('funding_8h_pct') if deriv else None,
                   funding_araligi_saat=deriv.get('funding_interval_hours') if deriv else None,
                   makro_durumu=status, yon_esigi=CANDIDATE_THRESHOLD,
                   yon_adayi=side if combined[side]>=60 and gap>=8 else 'YOK',
                   aday_yon=side, puan=max(combined.values()),
                   karar=decision, karar_gerekcesi=' · '.join(notes[:2]),
                   risk_nedenleri=notes, eksik_sartlar=blockers,
                   fiyat_aksiyonu_notu=row.get('sweep_notu', 'Fiyat yapısı kontrol ediliyor'),
                   onay_sartlari_kalan=len(blockers) if blockers else (0 if approved else 1),
                   makro_onay=approved, risk_freni=not approved)
        rows.append(row)
    order = {'LONG':0, 'SHORT':0, 'ADAY LONG':1, 'ADAY SHORT':1, 'RİSKLİ BEKLE':2, 'VERİ EKSİK':3, 'BEKLE':4}
    rows.sort(key=lambda r: (order.get(r.get('karar'), 5), r.get('onay_sartlari_kalan',99), -(r.get('puan') or 0), str(r.get('coin'))))
    count = sum(r.get('makro_onay') is True for r in rows)
    state_counts = {state:sum(r.get('karar')==state for r in rows) for state in order}
    return {'version':3, 'karar_sistemi':'VATAN2_7_DURUM', 'updated_at': now.isoformat(), 'source':'BINANCE_UM',
            'source_scan_at':scan.get('updated_at'), 'source_risk_at':risk.get('updated_at'),
            'market':'USDT-M-PERPETUAL', 'eligible_count':scan.get('eligible_count',len(rows)),
            'analyzed_count':scan.get('analyzed_count'), 'error_count':scan.get('error_count'),
            'approved_count':count, 'status_counts':state_counts,
            'candidate_threshold':CANDIDATE_THRESHOLD, 'approval_threshold':APPROVAL_THRESHOLD,
            'economic_gate':'ACIK' if global_ok else 'BEKLE',
            'economic_warnings':overall_notes, 'economic_notes':mnotes,
            'risk_covered_count':len(by_coin), 'scores_are_probabilities':False,
            'funding_thresholds_8h_pct':{'LONG':0.05,'SHORT':-0.05},
            'spread_thresholds_pct':{'majors':0.04,'altcoins':0.12},
            'results':rows}


def self_test():
    from copy import deepcopy
    now=datetime(2026,11,5,10,0,tzinfo=timezone.utc)
    stamp=now.isoformat()
    base={'coin':'AAAUSDT','durum':'ANALIZ_EDILDI',
          'yonler':{'1Dutc':'SHORT','4H':'SHORT','1H':'SHORT','15m':'SHORT'},
          'rsi15m':25,'macd15m':-1,'hacim_orani':1.5,
          'likidite_onayi':True,'btc_filtre':True,'trend_onayi':True,
          'hacim24h_usdt_tahmini':9_000_000,'spread_yuzde':0.03,
          'mum_15m_ts':1_762_337_700_000,
          'piyasa_rejimi':'TREND','adx1h':33.0,'chop1h':44.0,'range_orta_riski':False,
          'likidite_supurmesi':'YOK',
          'dun_seviyeleri':{'acilis':102,'en_yuksek':105,'en_dusuk':96},
          'puan':90,'karar':'SHORT ADAY','giris_referans':100,'stop':101,'hedef':98.2,'risk_odul':1.8}
    scan={'source':'BINANCE_UM','market':'USDT-M-PERPETUAL','updated_at':stamp,'results':[base],'eligible_count':1,'analyzed_count':1,'error_count':0}
    rec={'coin':'AAAUSDT','status':'IZLE','funding_rate':-0.0001,
         'funding_interval_hours':8,'funding_8h_pct':-0.01,'next_funding_ms':int((now.timestamp()+3600)*1000),
         'oi_usd':1000,'last_price':100,'comparison_status':'OK',
         'change_period_minutes':28,'oi_change_pct':5,'last_price_change_pct':-1,
         'oi_ccy_change_pct':6,'warnings':[]}
    risk={'source':'BINANCE_UM','updated_at':stamp,'global_risk':'KONTROL_ET',
          'analysis_source':{'updated_at':stamp.replace('+00:00','Z')},
          'sources':{k:{'status':'OK'} for k in ('bls_calendar','fed_calendar','us_treasury',
                        'binance_open_interest','binance_tickers','binance_funding')},
          'macro':{'event_gate':'OLAY_YOK','active_events':[],
                   'treasury':{'fresh_daily':True},
                   'broad_usd_proxy':{'status':'OK','change_pct':0.6}},
          'coins':[rec]}
    a=make_report(scan,risk,now)['results'][0]
    assert a['short_puani'] > a['teknik_short'] and a['long_puani'] < a['teknik_long'],a
    assert a['karar']=='SHORT' and a['makro_onay'],a
    assert a['hedef1']==99 and a['hedef2']==98.2 and 'pozisyon_plani' in a,a
    # Raw technical score is preserved when changing only safeguards.
    rec['funding_8h_pct']=-0.07
    b=make_report(scan,risk,now)['results'][0]
    assert b['karar']=='RİSKLİ BEKLE' and 'FUNDING' in b['eksik_sartlar'] and b['short_puani']==a['short_puani'],b
    assert a['plan_durumu']=='ONAYLI' and b['plan_durumu']=='TASLAK',b
    assert all(k in b for k in ('giris_referans','stop','hedef1','hedef2')),b
    assert b['makro_onay'] is False and b['risk_freni'] is True,b
    rec['funding_8h_pct']=-0.01
    base['spread_yuzde']=0.15
    c=make_report(scan,risk,now)['results'][0]
    assert c['karar']=='RİSKLİ BEKLE' and 'SPREAD' in c['eksik_sartlar'],c
    base['spread_yuzde']=0.03
    rec['comparison_status']='REFERANS_YOK'
    d=make_report(scan,risk,now)['results'][0]
    assert d['karar']=='VERİ EKSİK' and d['plan_durumu']=='TASLAK' and 'stop' in d,d
    rec['comparison_status']='OK'
    rec['oi_change_pct']=74.5
    e=make_report(scan,risk,now)['results'][0]
    assert e['karar']=='RİSKLİ BEKLE' and e['vadeli_veri_durumu']=='ANOMALI',e
    rec['oi_change_pct']=5
    base['btc_filtre']=False
    f=make_report(scan,risk,now)['results'][0]
    assert f['karar']=='RİSKLİ BEKLE' and 'BTC' in f['eksik_sartlar'],f
    base['btc_filtre']=True
    base['piyasa_rejimi']='YATAY';base['range_orta_riski']=True
    assert make_report(scan,risk,now)['results'][0]['karar']=='RİSKLİ BEKLE'
    base['piyasa_rejimi']='TREND';base['range_orta_riski']=False
    base['likidite_supurmesi']='DIP_IGNE_LONG'
    assert 'TERS_IGNE' in make_report(scan,risk,now)['results'][0]['eksik_sartlar']
    base['likidite_supurmesi']='YOK'
    risk['macro']['event_gate']='ETKINLIK_RISKI'
    assert make_report(scan,risk,now)['results'][0]['karar']=='RİSKLİ BEKLE'
    risk['macro']['event_gate']='OLAY_YOK'
    risk['coins']=[]
    assert make_report(scan,risk,now)['results'][0]['karar']=='VERİ EKSİK'
    risk['coins']=[rec]
    base['rsi15m']=60  # SHORT RSI +10 disappears, macro score stays high
    base['hacim_orani']=0.8  # Last trigger missing; 60–74 remains candidate
    g=make_report(scan,risk,now)['results'][0]
    assert g['karar'] in ('ADAY SHORT','BEKLE','RİSKLİ BEKLE','SHORT'),g
    assert g['plan_durumu'] == ('TASLAK' if g['puan'] >= APPROVAL_THRESHOLD else 'GIZLI'),g
    if g['puan'] < APPROVAL_THRESHOLD:
        assert all(k not in g for k in ('giris_referans','stop','hedef','hedef1','hedef2')),g
    # An unambiguously sub-85 setup never leaks old scanner levels.
    low = deepcopy(base)
    low['yonler'] = {'1Dutc':'SHORT','4H':'BEKLE','1H':'BEKLE','15m':'BEKLE'}
    low_scan = dict(scan, results=[low])
    under = make_report(low_scan, risk, now)['results'][0]
    assert under['puan'] < APPROVAL_THRESHOLD and under['plan_durumu']=='GIZLI',under
    assert all(k not in under for k in ('giris_referans','stop','hedef','hedef1','hedef2')),under
    base['adx1h']=13
    assert make_report(scan,risk,now)['results'][0]['karar'] in ('RİSKLİ BEKLE','BEKLE')
    base['adx1h']=33
    rec['next_funding_ms']=int((now.timestamp()+120)*1000)
    assert make_report(scan,risk,now)['results'][0]['karar'] == 'RİSKLİ BEKLE'
    rec['next_funding_ms']=int((now.timestamp()+3600)*1000)
    scan['updated_at']='2026-11-04T10:00:00+00:00'
    assert make_report(scan,risk,now)['results'][0]['karar']=='VERİ EKSİK'
    print('SELF_TEST_OK: 7 durum, 60/85, funding/spread engeli, BTC, makro, veri, OI ve stale')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--self-test',action='store_true')
    args=parser.parse_args()
    if args.self_test:
        self_test();return
    scan=json.loads(SOURCE.read_text('utf-8'))
    risk=json.loads(RISK.read_text('utf-8'))
    output=make_report(scan,risk)
    OUT.write_text(json.dumps(output,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print('MAKRO_60_OK',json.dumps({k:v for k,v in output.items() if k not in ('results',)},ensure_ascii=False))


if __name__=='__main__':
    main()
