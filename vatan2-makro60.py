#!/usr/bin/env python3
"""VATAN 2 - LONG/SHORT teknik + vadeli + makro puan birlestirici.

Var olan son-tum-analiz.json ve vatan2-risk.json dosyalarini OKUR,
bunlara dokunmaz; son-makro60.json olusturur. Otomatik emir yok.
60/100 yon adayi esigidir; 75/100 teknik risk onayi degildir.
Puanlar kazanma olasiligi/yuzdesi degildir.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path

SOURCE = Path('son-tum-analiz.json')
RISK = Path('vatan2-risk.json')
OUT = Path('son-makro60.json')
WEIGHTS = {'1Dutc': 20, '4H': 20, '1H': 15, '15m': 15}
CANDIDATE_THRESHOLD = 60
APPROVAL_THRESHOLD = 75
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
    shifts = {'LONG': 0, 'SHORT': 0}
    reasons = []
    rate = num(rec.get('funding_rate'))
    oi = num(rec.get('oi_change_pct'))
    price = num(rec.get('last_price_change_pct'))
    if rate is None or num(rec.get('oi_usd')) is None:
        return shifts, ['Funding / açık faiz eksik: yön ek puanı verilmedi'], False
    # High funding indicates crowded positioning, NOT automatic reverse signal.
    # fundingRate fractional unit: 0.001 = 0.10% per settlement (not APR).
    if rate >= 0.001:
        shifts['LONG'] -= 6
        reasons.append('Aşırı pozitif funding: LONG kalabalık, LONG -6')
    elif rate <= -0.001:
        shifts['SHORT'] -= 6
        reasons.append('Aşırı negatif funding: SHORT kalabalık, SHORT -6')
    # Moderate funding does not get directional votes without corroboration.
    if oi is not None and price is not None:
        if oi >= 3 and price >= 0.6:
            shifts['LONG'] += 4
            shifts['SHORT'] -= 3
            reasons.append('OI ve fiyat birlikte yükseldi: LONG +4 / SHORT -3')
        elif oi >= 3 and price <= -0.6:
            shifts['SHORT'] += 4
            shifts['LONG'] -= 3
            reasons.append('OI yükseldi, fiyat düştü: SHORT +4 / LONG -3')
        elif oi <= -8:
            shifts['LONG'] -= 2
            shifts['SHORT'] -= 2
            reasons.append('OI hızlı daraldı: iki yön -2')
    else:
        reasons.append('OI/fiyat değişim geçmişi henüz yok: ilave yön puanı yok')
    return shifts, reasons, True


def make_report(scan, risk, now=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(scan.get('results'), list):
        raise ValueError('Tarama listesi yok')
    scan_fresh = freshness(scan.get('updated_at'), now)
    risk_fresh = freshness(risk.get('updated_at'), now)
    same_scan = scan.get('updated_at') == (risk.get('analysis_source') or {}).get('updated_at')
    sources = risk.get('sources') or {}
    macro = risk.get('macro') or {}
    calendar_ok = macro.get('event_gate') == 'OLAY_YOK' and all(
        (sources.get(k) or {}).get('status') == 'OK' for k in ('bls_calendar', 'fed_calendar'))
    no_event = calendar_ok and not (macro.get('active_events') or [])
    treasury_ok = (sources.get('us_treasury') or {}).get('status') == 'OK' and (macro.get('treasury') or {}).get('fresh_daily') is True
    global_ok = scan_fresh and risk_fresh and same_scan and no_event and treasury_ok
    overall_notes = []
    if not scan_fresh: overall_notes.append('Coin taraması eski')
    if not risk_fresh: overall_notes.append('Ekonomik rapor eski')
    if not same_scan: overall_notes.append('Ekonomik rapor başka taramaya ait')
    if not calendar_ok: overall_notes.append('Resmî takvim eksik veya kritik ekonomik olay var')
    if not treasury_ok: overall_notes.append('Tahvil verisi doğrulanmamış/eski')
    mshift, mnotes = macro_impact(risk) if risk_fresh and same_scan else ({'LONG':0,'SHORT':0},['Makro rapor eski/uyumsuz; puana katılmadı'])
    by_coin = {c.get('coin'):c for c in risk.get('coins',[]) if isinstance(c,dict)}
    rows = []
    for r in scan['results']:
        if not isinstance(r, dict):
            continue
        row = dict(r)
        tech = technical_scores(r)
        if tech is None:
            row.update(long_puani=None, short_puani=None, yon_esigi=60, yon_adayi='YOK',
                       makro_etki={'LONG':0,'SHORT':0}, makro_notlari=['Teknik veri eksik'],
                       makro_durumu='VERI_EKSIK', karar='BEKLE', puan=None)
            rows.append(row)
            continue
        deriv = by_coin.get(r.get('coin')) if risk_fresh and same_scan else None
        if deriv is not None:
            dshift, dnotes, derivative_ok = derivatives_impact(deriv)
        else:
            dshift, dnotes, derivative_ok = {'LONG':0,'SHORT':0}, ['Bu coin için güncel funding/OI kontrolü yok'], False
        net = {side: int(clamp(dshift[side]+mshift[side], -MAX_NET_EFFECT, MAX_NET_EFFECT)) for side in ('LONG','SHORT')}
        combined = {side: int(clamp(tech[side]+net[side])) for side in ('LONG','SHORT')}
        side = 'LONG' if combined['LONG'] >= combined['SHORT'] else 'SHORT'
        gap = combined[side] - combined['SHORT' if side=='LONG' else 'LONG']
        candidate = combined[side] >= CANDIDATE_THRESHOLD and gap >= 8
        # A 60 score creates a direction candidate, NEVER an automatic trade approval.
        safe = (global_ok and derivative_ok and deriv.get('status') == 'IZLE' and
                r.get('trend_onayi') is True and r.get('btc_filtre') is True and
                r.get('likidite_onayi') is True and all(r.get('yonler',{}).get(p)==side for p in WEIGHTS))
        approved = bool(candidate and safe and combined[side]>=APPROVAL_THRESHOLD and gap>=12)
        status = ('GUNCEL' if safe else
                  'OLAY_RISKI' if not calendar_ok else
                  'VERI_EKSIK' if not global_ok or not derivative_ok else
                  'RISK_UYARISI' if deriv.get('status')=='RISK_UYARISI' else 'FILTRE_BEKLE')
        row.update( teknik_long=tech['LONG'], teknik_short=tech['SHORT'],
                   long_puani=combined['LONG'], short_puani=combined['SHORT'],
                   makro_etki=net, makro_notlari=mnotes+dnotes,
                   makro_durumu=status, yon_esigi=CANDIDATE_THRESHOLD,
                   yon_adayi=side if candidate else 'YOK',
                   aday_yon=side, puan=max(combined.values()),
                   karar=(side+' ADAY' if approved else 'BEKLE'),
                   makro_onay=approved, risk_freni=not approved)
        if not approved:
            # Old entry/stop/target must not survive invalidation.
            for key in ('giris_referans','stop','hedef','risk_odul'):
                row.pop(key, None)
        rows.append(row)
    rows.sort(key=lambda r: (r.get('durum')!='ANALIZ_EDILDI', -(r.get('puan') or 0), str(r.get('coin'))))
    count=sum(r.get('karar') in ('LONG ADAY','SHORT ADAY') for r in rows)
    return {'version':2, 'updated_at': now.isoformat(), 'source':'OKX',
            'source_scan_at': scan.get('updated_at'), 'source_risk_at':risk.get('updated_at'),
            'market':'USDT-SWAP', 'eligible_count': scan.get('eligible_count',len(rows)),
            'analyzed_count':scan.get('analyzed_count'), 'error_count':scan.get('error_count'),
            'approved_count':count, 'candidate_threshold':CANDIDATE_THRESHOLD,
            'approval_threshold':APPROVAL_THRESHOLD, 'economic_gate':'ACIK' if global_ok else 'BEKLE',
            'economic_warnings':overall_notes, 'economic_notes':mnotes,
            'risk_covered_count':len(by_coin), 'scores_are_probabilities':False,
            'results':rows}


def self_test():
    now = datetime(2026,11,5,10,0,tzinfo=timezone.utc)
    stamp=now.isoformat()
    base={'coin':'AAA-USDT-SWAP','durum':'ANALIZ_EDILDI',
          'yonler':{'1Dutc':'SHORT','4H':'SHORT','1H':'SHORT','15m':'SHORT'},
          'rsi15m':25,'macd15m':-1,'hacim_orani':1.5,'likidite_onayi':True,
          'btc_filtre':True,'trend_onayi':True,'puan':90,'karar':'SHORT ADAY'}
    scan={'updated_at':stamp,'results':[base],'eligible_count':1,'analyzed_count':1,'error_count':0}
    risk={'updated_at':stamp, 'analysis_source':{'updated_at':stamp},
          'sources':{'bls_calendar':{'status':'OK'},'fed_calendar':{'status':'OK'},'us_treasury':{'status':'OK'}},
          'macro':{'event_gate':'OLAY_YOK','active_events':[], 'treasury':{'fresh_daily':True},
                   'broad_usd_proxy':{'status':'OK','change_pct':0.6}},
          'coins':[{'coin':'AAA-USDT-SWAP','status':'IZLE','funding_rate':0.0001,
                    'oi_usd':1000,'oi_change_pct':5,'last_price_change_pct':-1}]}
    report=make_report(scan,risk,now)
    a=report['results'][0]
    assert a['short_puani'] > a['teknik_short'] and a['long_puani'] < a['teknik_long'],a
    assert a['karar']=='SHORT ADAY',a
    risk['macro']['event_gate']='ETKINLIK_RISKI'
    b=make_report(scan,risk,now)['results'][0]
    assert b['karar']=='BEKLE'
    risk['macro']['event_gate']='OLAY_YOK'
    risk['coins']=[]
    c=make_report(scan,risk,now)['results'][0]
    assert c['karar']=='BEKLE' and c['makro_durumu']=='VERI_EKSIK'
    scan['updated_at']='2026-11-04T10:00:00+00:00'
    d=make_report(scan,risk,now)['results'][0]
    assert d['karar']=='BEKLE'
    print('SELF_TEST_OK: 60 esik, LONG/SHORT etkileri, zaman damgasi, risk freni')


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
