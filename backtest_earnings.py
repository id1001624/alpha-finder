#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Finder 財報回測腳本
用已知的財報爆發股回測，找出 main.py 篩選漏洞

已知案例（2026-02-24 ~ 02-26）：
  ❌ 未抓到（不在全量數據）：
    - CRCL  財報 2/25  漲 +35%  (EPS beat +250%)
    - CAVA  財報 2/24  漲 +26%  (同店 +12.9%)
    - AXON  財報 2/24  漲 +17%  (TASER 訂單 +35%)
  ✅ 有抓到（在全量數據）：
    - ZETA  財報 2/24  漲 +13%
    - BEAM  財報 2/24
    - DRS   財報 2/24
    - WULF  財報 2/26
    - CIFR  財報 2/24
    - WLK   財報 2/24
"""

import os
import sys
import time
import pandas as pd
import requests
import yfinance as yf

from app_logging import install_builtin_print_logging


install_builtin_print_logging()

# 載入 SSL 修復
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from main import _fix_ssl_cert_path
    _fix_ssl_cert_path()
except (ImportError, ModuleNotFoundError, OSError, PermissionError):
    pass

RUNTIME_DATA_ERRORS = (
    OSError,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
    IndexError,
    requests.RequestException,
)

from config import (
    SELECTED_INDICES, MAX_STOCKS_TO_PROCESS,
    EARNINGS_MIN_MCAP, EARNINGS_MIN_VOLUME, MAX_EARNINGS_MERGE,
    EARNINGS_RESERVED_SLOTS,
    FINNHUB_API_KEY,
)

# ============ 回測用股票清單 ============
BACKTEST_STOCKS = {
    # 未抓到的（應該要抓到）
    'CRCL': {'earnings_date': '2026-02-25', 'post_change': 35.0, 'status': '❌ 未抓到'},
    'CAVA': {'earnings_date': '2026-02-24', 'post_change': 26.0, 'status': '❌ 未抓到'},
    'AXON': {'earnings_date': '2026-02-24', 'post_change': 17.0, 'status': '❌ 未抓到'},
    # 有抓到的（驗證為何能通過）
    'ZETA': {'earnings_date': '2026-02-24', 'post_change': 13.0, 'status': '✅ 有抓到'},
    'BEAM': {'earnings_date': '2026-02-24', 'post_change': 10.0, 'status': '✅ 有抓到'},
    'DRS':  {'earnings_date': '2026-02-24', 'post_change': 10.0, 'status': '✅ 有抓到'},
    'WULF': {'earnings_date': '2026-02-26', 'post_change': 15.0, 'status': '✅ 有抓到'},
    'CIFR': {'earnings_date': '2026-02-24', 'post_change': 12.0, 'status': '✅ 有抓到'},
    'WLK':  {'earnings_date': '2026-02-24', 'post_change': 10.0, 'status': '✅ 有抓到'},
    # 早期漏掉的大漲股
    'VRT':  {'earnings_date': '2025-12-31', 'post_change': 57.0, 'status': '❌ 未抓到（長期）'},
}


def check_index_membership(ticker: str) -> dict:
    """檢查股票是否在我們的指數範圍內"""
    try:
        from finviz.screener import Screener
        for idx in SELECTED_INDICES:
            screener = Screener(filters=[idx], table='Overview')
            tickers_in_idx = [s['Ticker'] for s in screener]
            if ticker in tickers_in_idx:
                return {'in_index': True, 'index': idx}
        return {'in_index': False, 'index': None}
    except RUNTIME_DATA_ERRORS as e:
        return {'in_index': 'error', 'index': str(e)}


def check_yfinance_data(ticker: str) -> dict:
    """檢查 yfinance 能取得哪些資料"""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info

        mcap = info.get('marketCap', 0) or 0
        avg_vol = info.get('averageVolume', 0) or 0
        target_price = info.get('targetMeanPrice', None)
        num_analysts = info.get('numberOfAnalystOpinions', 0) or 0
        forward_eps = info.get('forwardEps', None)
        sector = info.get('sector', 'N/A')
        industry = info.get('industry', 'N/A')
        price = info.get('currentPrice', 0) or info.get('regularMarketPrice', 0) or 0

        # 財報日期
        earnings_date = None
        try:
            cal = stock.calendar
            if cal is not None and 'Earnings Date' in cal:
                dates = cal['Earnings Date']
                if len(dates) > 0:
                    earnings_date = str(dates[0])
        except (AttributeError, TypeError, ValueError, KeyError, IndexError):
            pass

        return {
            'price': price,
            'market_cap': mcap,
            'avg_volume': avg_vol,
            'target_price': target_price,
            'num_analysts': num_analysts,
            'forward_eps': forward_eps,
            'sector': sector,
            'industry': industry,
            'earnings_date_yf': earnings_date,
        }
    except RUNTIME_DATA_ERRORS as e:
        return {'error': str(e)}


def check_finnhub_earnings(ticker: str) -> dict:
    """檢查 Finnhub 是否有這檔股票的財報資訊"""
    if not FINNHUB_API_KEY:
        return {'finnhub': 'no_api_key'}

    try:
        url = "https://finnhub.io/api/v1/calendar/earnings"
        params = {
            'from': '2026-02-20',
            'to': '2026-03-05',
            'symbol': ticker,
            'token': FINNHUB_API_KEY,
        }
        r = requests.get(url, params=params, timeout=(3, 8))
        if r.status_code == 200:
            data = r.json()
            earnings = data.get('earningsCalendar', [])
            if earnings:
                item = earnings[0]
                return {
                    'finnhub_date': item.get('date'),
                    'finnhub_hour': item.get('hour'),
                    'finnhub_eps_est': item.get('epsEstimate'),
                    'finnhub_eps_actual': item.get('epsActual'),
                }
            return {'finnhub': 'no_data'}
        return {'finnhub': f'error_{r.status_code}'}
    except RUNTIME_DATA_ERRORS as e:
        return {'finnhub': f'error_{e}'}


def simulate_filters(data: dict) -> dict:
    """模擬 main.py 的各層過濾器，標記每層是否通過"""
    results = {}

    mcap = data.get('market_cap', 0)
    avg_vol = data.get('avg_volume', 0)
    num_analysts = data.get('num_analysts', 0)
    # 過濾器 1: Finviz 基本條件（geo_usa + sh_avgvol_o500）
    results['F1_avg_vol_500k'] = avg_vol >= 500_000

    # 過濾器 2: 信號強度排名（前 MAX_STOCKS_TO_PROCESS）
    # 財報前可能沒有強勢動能，排名可能在 120 之外
    results['F2_signal_rank'] = '需要當日數據'

    # 過濾器 3: Finnhub 補強條件
    results['F3_finnhub_mcap_1B'] = mcap >= EARNINGS_MIN_MCAP
    results['F3_finnhub_vol_500k'] = avg_vol >= EARNINGS_MIN_VOLUME

    # 過濾器 4（已修正）: 不再用分析師數 >= 10 硬截斷
    results['F4_analysts_gte_10'] = True  # 已移除此過濾器
    results['F4_target_price_exists'] = True  # 已移除此過濾器
    results['F4_note'] = f'分析師數={num_analysts}（不再截斷，僅影響 Sheet3 預測情報）'

    # 過濾器 5: 財報預熱條件
    results['F5_earnings_mcap_1B'] = mcap >= EARNINGS_MIN_MCAP

    # 綜合判斷
    killed_by = []
    if not results['F1_avg_vol_500k']:
        killed_by.append('F1: 平均量 < 500k')
    if not results['F3_finnhub_mcap_1B']:
        killed_by.append('F3: 市值 < 1B（Finnhub 補強被跳過）')

    results['killed_by'] = killed_by
    results['would_survive'] = len(killed_by) == 0

    return results


def run_backtest():
    """執行回測"""
    print("=" * 70)
    print("  Alpha Finder 財報回測腳本".center(60))
    print("  用已知爆發股驗證篩選漏洞".center(60))
    print("=" * 70)

    print("\n當前設定：")
    print(f"  指數範圍: {SELECTED_INDICES}")
    print(f"  最大處理數: {MAX_STOCKS_TO_PROCESS}")
    print(f"  財報股保留名額: {EARNINGS_RESERVED_SLOTS}")
    print(f"  財報補強市值門檻: ${EARNINGS_MIN_MCAP/1e9:.1f}B")
    print(f"  財報補強量能門檻: {EARNINGS_MIN_VOLUME/1e3:.0f}k")
    print(f"  補強上限: {MAX_EARNINGS_MERGE}")
    print("  步驟 2.2: 已移除分析師硬截斷（僅保留無效資料清除）")
    print(f"  Finnhub API: {'✅ 已設定' if FINNHUB_API_KEY else '❌ 未設定'}")

    all_results = []

    for ticker, meta in BACKTEST_STOCKS.items():
        print(f"\n{'─' * 60}")
        print(f"  回測 {ticker} | {meta['status']} | 財報後漲幅 +{meta['post_change']}%")
        print(f"{'─' * 60}")

        # 1. 檢查 yfinance 資料
        print("  [1] 查詢 yfinance 資料...", end=' ')
        yf_data = check_yfinance_data(ticker)
        if 'error' in yf_data:
            print(f"[X] {yf_data['error']}")
            continue
        print("[OK]")

        mcap_str = f"${yf_data['market_cap']/1e9:.2f}B" if yf_data['market_cap'] > 1e9 else f"${yf_data['market_cap']/1e6:.0f}M"
        print(f"    市值: {mcap_str}")
        print(f"    平均量: {yf_data['avg_volume']:,.0f}")
        print(f"    分析師數: {yf_data['num_analysts']}")
        print(f"    目標價: ${yf_data['target_price']}" if yf_data['target_price'] else "    目標價: 無")
        print(f"    產業: {yf_data['sector']} / {yf_data['industry']}")
        print(f"    yfinance 財報日: {yf_data.get('earnings_date_yf', 'N/A')}")

        # 2. 檢查 Finnhub
        print("  [2] 查詢 Finnhub...", end=' ')
        fh_data = check_finnhub_earnings(ticker)
        if 'finnhub_date' in fh_data:
            print(f"[OK] 日期: {fh_data['finnhub_date']} {fh_data.get('finnhub_hour', '')}")
        else:
            print(f"[!] {fh_data.get('finnhub', 'N/A')}")

        # 3. 模擬過濾器
        print("  [3] 模擬過濾器...")
        filter_results = simulate_filters(yf_data)

        for key, val in filter_results.items():
            if key in ('killed_by', 'would_survive'):
                continue
            status = '✅' if val == True else ('❌' if val == False else '⚠️')
            print(f"    {status} {key}: {val}")

        if filter_results['would_survive']:
            print("  \n  ✅ 結論: 此股票能通過所有過濾器")
        else:
            print("  \n  ❌ 結論: 此股票被以下過濾器殺掉:")
            for reason in filter_results['killed_by']:
                print(f"    → {reason}")

        all_results.append({
            'Ticker': ticker,
            'Status': meta['status'],
            'Post_Change': meta['post_change'],
            'Market_Cap': yf_data.get('market_cap', 0),
            'Avg_Volume': yf_data.get('avg_volume', 0),
            'Num_Analysts': yf_data.get('num_analysts', 0),
            'Target_Price': yf_data.get('target_price'),
            'Would_Survive': filter_results['would_survive'],
            'Killed_By': ' | '.join(filter_results['killed_by']),
        })

        time.sleep(0.3)

    # ============ 匯總報告 ============
    print("\n\n" + "=" * 70)
    print("  回測匯總報告".center(60))
    print("=" * 70)

    df = pd.DataFrame(all_results)
    if len(df) == 0:
        print("  無回測結果")
        return

    print(f"\n  總共回測: {len(df)} 檔")
    print(f"  ✅ 能通過過濾器: {df['Would_Survive'].sum()} 檔")
    print(f"  ❌ 被過濾器殺掉: {(~df['Would_Survive']).sum()} 檔")

    # 過濾器殺傷力分析
    print("\n  ── 各過濾器殺傷力 ──")
    killed_reasons = {}
    for _, row in df.iterrows():
        if row['Killed_By']:
            for reason in row['Killed_By'].split(' | '):
                filter_name = reason.split(':')[0].strip()
                killed_reasons[filter_name] = killed_reasons.get(filter_name, 0) + 1

    for reason, count in sorted(killed_reasons.items(), key=lambda x: -x[1]):
        pct = count / len(df) * 100
        print(f"    {reason}: 殺掉 {count} 檔 ({pct:.0f}%)")

    # 建議修改
    print("\n  ── 建議修改 ──")

    analysts_killed = sum(1 for _, r in df.iterrows() if 'F4' in r.get('Killed_By', ''))
    if analysts_killed > 0:
        # 找出被殺的股票的分析師數分布
        killed_analysts = df[df['Killed_By'].str.contains('F4', na=False)]['Num_Analysts']
        if len(killed_analysts) > 0:
            print("    1. 分析師門檻 10 → 建議改為 3")
            print(f"       被殺的股票分析師數: {killed_analysts.tolist()}")
            print(f"       改為 3 可救回 {analysts_killed} 檔")

    mcap_killed = sum(1 for _, r in df.iterrows() if 'F3' in r.get('Killed_By', ''))
    if mcap_killed > 0:
        print("    2. Finnhub 補強市值門檻 1B → 建議改為 500M")
        print(f"       可多補抓 {mcap_killed} 檔中小型財報股")

    print("\n    3. 全量數據應包含所有通過 yfinance 補充的股票")
    print("       不應在上傳前再用分析師數過濾")
    print("       分析師過濾應只用於「預測情報」軌道")

    # 輸出 CSV
    csv_path = os.path.join(os.path.dirname(__file__), 'backtest_results.csv')
    df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    print(f"\n  回測結果已存: {csv_path}")

    print("\n" + "=" * 70)
    print("  回測完成！".center(60))
    print("=" * 70)


if __name__ == "__main__":
    run_backtest()
