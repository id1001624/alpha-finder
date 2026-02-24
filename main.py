#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Finder 每日情報掃描腳本 v2.0
使用 Finviz + Yahoo Finance 自動掃描美股潛力股
作者：Alpha Finder Team
日期：2026-02-23
"""

import sys
import os
import shutil
from datetime import datetime, timedelta

# ===== 最優先：修復 SSL 憑證路徑（中文路徑導致 curl 失敗）=====
# 必須在任何 import 之前執行，確保環境變數生效
def _fix_ssl_cert_path():
    """修復 SSL 憑證路徑中文問題"""
    try:
        import certifi
        original = certifi.where()
        # 只有當路徑含非 ASCII 字元時才需要修復
        try:
            original.encode('ascii')
            return  # 路徑是純 ASCII，無需修復
        except UnicodeEncodeError:
            pass

        # 複製到使用者 home 目錄下的純英文路徑
        safe_dir = os.path.join(os.path.expanduser('~'), '.alpha_finder_certs')
        os.makedirs(safe_dir, exist_ok=True)
        safe_cert = os.path.join(safe_dir, 'cacert.pem')

        # 只在檔案不存在或原始檔案更新時才複製
        if not os.path.exists(safe_cert) or \
           os.path.getmtime(original) > os.path.getmtime(safe_cert):
            shutil.copy2(original, safe_cert)

        # 設定環境變數讓 curl 和 requests 都能找到
        os.environ['CURL_CA_BUNDLE'] = safe_cert
        os.environ['SSL_CERT_FILE'] = safe_cert
        os.environ['REQUESTS_CA_BUNDLE'] = safe_cert
        os.environ['SSL_NO_VERIFY'] = '0'  # 確保不跳過驗證
    except Exception as e:
        # 非關鍵，失敗就跳過
        pass

_fix_ssl_cert_path()

# 現在才 import 其他依賴
import pandas as pd
import time
import warnings
from typing import List, Dict, Tuple
import yfinance as yf

# 從 config.py 導入所有配置常數
from config import (
    SELECTED_EXCHANGES, SELECTED_INDICES,
    MAX_STOCKS_TO_PROCESS, API_DELAY,
    LAUNCH_MIN_GAIN, LAUNCH_MIN_REL_VOL, LAUNCH_MIN_PRICE, LAUNCH_MIN_MCAP,
    EARNINGS_DAYS_AHEAD, EARNINGS_MIN_MCAP,
    ANALYST_MIN_UPSIDE, ANALYST_MIN_COUNT,
    RATING_A_REL_VOL, RATING_A_UPSIDE,
    TOP_N_STOCKS,
    GSHEET_ENABLED, GSHEET_NAME, GSHEET_CREDENTIALS_FILE,
)

# Google Sheets 相關套件
try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    GSHEET_AVAILABLE = True
except ImportError:
    GSHEET_AVAILABLE = False
    print("[!] 警告：未安裝 gspread 或 oauth2client，Google Sheets 功能將被跳過")
    print("    請執行: pip install gspread oauth2client")

warnings.filterwarnings('ignore')


# ============ 工具函數 ============

def retry_on_failure(func, max_retries=3, delay=2.0, backoff=2.0):
    """帶指數退避的重試機制"""
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            wait = delay * (backoff ** attempt)
            print(f"    [重試] 第 {attempt+1} 次失敗: {e}，等待 {wait:.1f}s...")
            time.sleep(wait)


# 優先產業關鍵字
PRIORITY_KEYWORDS = {
    'AI/半導體': ['ai', 'artificial intelligence', 'chip', 'semiconductor', 'nvidia', 'amd', 'gpu', 'neural', 'machine learning'],
    '資料中心': ['data center', 'datacenter', 'cloud computing', 'server', 'storage'],
    '電力設備': ['power', 'electric', 'utility', 'energy equipment', 'grid', 'battery'],
    '工業': ['industrial', 'machinery', 'manufacturing', 'automation'],
    '醫療服務': ['healthcare', 'medical', 'hospital', 'health service'],
    '生技': ['biotech', 'pharmaceutical', 'pharma', 'drug', 'therapy', 'clinical'],
}

# 排除清單
EXCLUDE_ETFS = ['TQQQ', 'SQQQ', 'UPRO', 'SPXL', 'SPXS', 'UVXY', 'SVXY', 'JNUG', 'JDST', 'TMF', 'TNA', 'TZA']
EXCLUDE_KEYWORDS = ['reit', 'trust', 'etf', 'fund']


def print_banner():
    """顯示啟動橫幅"""
    banner = """
    ================================================================
    ||         Alpha Finder 每日情報掃描腳本 v2.0                  ||
    ||         免費版 - Finviz + Yahoo Finance                     ||
    ||         掃描時間: {}                             ||
    ================================================================
    """.format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print(banner)


def parse_finviz_value(value_str: str) -> float:
    """解析 Finviz 數值格式（1.23M, 4.56B, 2.8T, 78.90%）"""
    if not value_str or value_str == '-':
        return 0.0

    value_str = value_str.strip('%').strip()

    multiplier = 1
    if value_str.endswith('T'):
        multiplier = 1e12
        value_str = value_str[:-1]
    elif value_str.endswith('B'):
        multiplier = 1e9
        value_str = value_str[:-1]
    elif value_str.endswith('M'):
        multiplier = 1e6
        value_str = value_str[:-1]
    elif value_str.endswith('K'):
        multiplier = 1e3
        value_str = value_str[:-1]

    try:
        return float(value_str) * multiplier
    except (ValueError, TypeError):
        return 0.0


def classify_sector(sector: str, industry: str) -> Tuple[str, str]:
    """分類產業並評級（A=優先產業, B=一般）"""
    combined = f"{sector} {industry}".lower()

    for priority_sector, keywords in PRIORITY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in combined:
                return priority_sector, 'A'

    return sector, 'B'


def is_excluded(ticker: str, sector: str) -> bool:
    """檢查股票是否應被排除（ETF、REIT 等）"""
    if ticker in EXCLUDE_ETFS:
        return True

    sector_lower = sector.lower()
    for keyword in EXCLUDE_KEYWORDS:
        if keyword in sector_lower:
            return True

    return False


def _apply_sector_classification(df: pd.DataFrame) -> pd.DataFrame:
    """為 DataFrame 加入 Priority_Sector 和 Rating 欄位（共用邏輯）"""
    df['Priority_Sector'] = df.apply(
        lambda r: classify_sector(r['Sector'], r['Industry'])[0], axis=1
    )
    df['Rating'] = df.apply(
        lambda r: classify_sector(r['Sector'], r['Industry'])[1], axis=1
    )
    return df


# ============ Google Sheets 上傳模組 ============

class GoogleSheetsUploader:
    """Google Sheets 上傳器"""

    def __init__(self, credentials_file=GSHEET_CREDENTIALS_FILE,
                 spreadsheet_name=GSHEET_NAME):
        self.credentials_file = credentials_file
        self.spreadsheet_name = spreadsheet_name
        self.gc = None
        self.spreadsheet = None
        self.authenticated = False

    def authenticate(self) -> bool:
        """驗證並連接 Google Sheets"""
        try:
            if not os.path.exists(self.credentials_file):
                print(f"[X] 找不到憑證檔 {self.credentials_file}")
                return False

            print("[*] 正在驗證 Google Sheets...")
            scope = [
                'https://spreadsheets.google.com/feeds',
                'https://www.googleapis.com/auth/drive'
            ]
            credentials = ServiceAccountCredentials.from_json_keyfile_name(
                self.credentials_file, scope
            )
            self.gc = gspread.authorize(credentials)
            self.authenticated = True
            print("  [OK] 驗證成功")
            return True

        except Exception as e:
            print(f"[X] 驗證失敗: {e}")
            return False

    def get_or_create_spreadsheet(self) -> bool:
        """取得或建立試算表"""
        try:
            print(f"[*] 正在開啟試算表: {self.spreadsheet_name}")
            try:
                self.spreadsheet = self.gc.open(self.spreadsheet_name)
                print("  [OK] 試算表已找到")
                return True
            except gspread.SpreadsheetNotFound:
                print(f"[X] 找不到試算表: {self.spreadsheet_name}")
                print(f"   請到 Google Sheets 建立名為 '{self.spreadsheet_name}' 的試算表")
                print(f"   並分享給服務帳號: {self._get_service_account_email()}")
                return False
        except Exception as e:
            print(f"[X] 開啟試算表失敗: {e}")
            return False

    def _get_service_account_email(self) -> str:
        try:
            import json
            with open(self.credentials_file, 'r') as f:
                creds = json.load(f)
                return creds.get('client_email', '不詳')
        except Exception:
            return '不詳'

    def _format_header(self, worksheet):
        """設定工作表標題格式（粗體 + 灰色背景 + 凍結首行）"""
        try:
            if worksheet.row_count == 0:
                return
            header_cells = worksheet.range(1, 1, 1, worksheet.col_count)
            for cell in header_cells:
                cell.text_format = {'bold': True}
                cell.background_color = {'red': 0.8, 'green': 0.8, 'blue': 0.8}
            worksheet.update_cells(header_cells, value_input_option='RAW')

            try:
                worksheet.freeze(rows=1)
            except (AttributeError, Exception):
                pass

            try:
                worksheet.auto_resize_columns(0, worksheet.col_count)
            except Exception:
                pass

            print("    [OK] 標題格式設定完成")
        except Exception as e:
            print(f"    [!] 標題格式設定失敗: {e}")

    def upload_sheet(self, df: pd.DataFrame, sheet_title: str, clear_first=True) -> bool:
        """上傳資料到指定工作表"""
        try:
            print(f"  [*] 上傳資料到工作表: {sheet_title}")

            try:
                worksheet = self.spreadsheet.worksheet(sheet_title)
                if clear_first:
                    worksheet.clear()
            except gspread.WorksheetNotFound:
                worksheet = self.spreadsheet.add_worksheet(
                    title=sheet_title,
                    rows=len(df) + 10,
                    cols=len(df.columns) + 5
                )

            # 準備資料：Header + 內容（NaN 轉空字串）
            data = [df.columns.tolist()]
            for _, row in df.iterrows():
                row_data = []
                for val in row:
                    if pd.isna(val):
                        row_data.append('')
                    elif isinstance(val, (float, int)) and pd.notna(val):
                        row_data.append(val)
                    else:
                        row_data.append(str(val))
                data.append(row_data)

            worksheet.append_rows(data, value_input_option='RAW')
            self._format_header(worksheet)
            print(f"    [OK] 上傳完成: {len(df)} 筆資料")
            return True

        except Exception as e:
            print(f"    [X] 上傳失敗: {e}")
            return False

    def upload_daily_report(self, sheet1: pd.DataFrame, sheet2: pd.DataFrame,
                           sheet3: pd.DataFrame, date_str: str = None) -> bool:
        """上傳每日完整報告（三合一管理模式）"""
        try:
            if date_str is None:
                date_str = datetime.now().strftime("%Y-%m-%d")

            print(f"\n[*] 開始上傳每日報告（日期: {date_str}）")

            top_n = min(TOP_N_STOCKS, 3)
            sheet1_top = sheet1.head(top_n).copy() if len(sheet1) > 0 else pd.DataFrame()
            sheet2_top = sheet2.head(top_n).copy() if len(sheet2) > 0 else pd.DataFrame()
            sheet3_top = sheet3.head(top_n).copy() if len(sheet3) > 0 else pd.DataFrame()

            combined_df = self._build_combined_report(sheet1_top, sheet2_top, sheet3_top)
            success = self.upload_sheet(combined_df, date_str, clear_first=True)

            if success:
                print(f"[OK] 每日報告上傳成功")
                s1, s2, s3 = len(sheet1_top), len(sheet2_top), len(sheet3_top)
                print(f"  資料: 起飛清單({s1}) + 財報預熱({s2}) + 預測情報({s3})")

            return success

        except Exception as e:
            print(f"[X] 上傳每日報告失敗: {e}")
            return False

    def upload_full_data(self, df_enriched: pd.DataFrame, date_str: str = None) -> bool:
        """
        上傳完整 40 檔掃描結果到「全量數據」工作表
        供 Perplexity / ChatGPT 讀取進行 AI 分析
        每次執行覆蓋上次資料（tab 名稱固定，不按日期）
        """
        try:
            if date_str is None:
                date_str = datetime.now().strftime("%Y-%m-%d %H:%M")

            print(f"\n[*] 上傳全量數據（{len(df_enriched)} 檔）到「全量數據」分頁...")

            # 整理輸出欄位，中文化欄名
            col_map = {
                'Ticker':        '代碼',
                'Company':       '公司名稱',
                'Sector':        '產業',
                'Price':         '股價',
                'Daily_Change':  '今日漲幅%',
                'Rel_Volume':    '量能倍數',
                'Market_Cap':    '市值',
                'Target_Price':  '目標價',
                'Upside_Pct':    '上漲空間%',
                'Num_Analysts':  '分析師數',
                'Earnings_Date': '財報日期',
                'News_Headline': '新聞標題',
            }
            available_cols = [c for c in col_map if c in df_enriched.columns]
            output = df_enriched[available_cols].copy()
            output.columns = [col_map[c] for c in available_cols]

            # 數值格式化
            for col in ['今日漲幅%', '量能倍數', '上漲空間%']:
                if col in output.columns:
                    output[col] = output[col].apply(
                        lambda v: round(float(v), 2) if pd.notna(v) else ''
                    )

            # 加入掃描時間 metadata 列
            meta_row = pd.DataFrame([{'代碼': f'⏱ 掃描時間: {date_str}  |  共 {len(output)} 檔  |  資料來源: Finviz + Yahoo Finance'}])
            final_df = pd.concat([meta_row, output], ignore_index=True)

            return self.upload_sheet(final_df, '全量數據', clear_first=True)

        except Exception as e:
            print(f"[X] 上傳全量數據失敗: {e}")
            return False

    def _build_combined_report(self, sheet1: pd.DataFrame, sheet2: pd.DataFrame,
                               sheet3: pd.DataFrame) -> pd.DataFrame:
        """組建綜合報告 DataFrame"""
        frames = []

        if len(sheet1) > 0:
            frames.append(pd.DataFrame([{'========': '=== 起飛清單 Top 3 ==='}]))
            frames.append(sheet1)
            frames.append(pd.DataFrame([{}]))

        if len(sheet2) > 0:
            frames.append(pd.DataFrame([{'========': '=== 財報預熱 Top 3 ==='}]))
            frames.append(sheet2)
            frames.append(pd.DataFrame([{}]))

        if len(sheet3) > 0:
            frames.append(pd.DataFrame([{'========': '=== 預測情報 Top 3 ==='}]))
            frames.append(sheet3)

        if frames:
            return pd.concat(frames, ignore_index=True)
        return pd.DataFrame()


# ============ 資料爬取 ============

def scrape_finviz_screener() -> pd.DataFrame:
    """
    使用 mariostoev/finviz v2.0.0 庫爬取 Finviz Screener

    策略：
    1. 用 Performance 表取得當日漲幅（Change / Perf Day）
    2. 用 Overview 表取得基本資訊（Sector, Industry, Market Cap）
    3. 合併兩表，提供完整數據
    """
    print("\n[步驟 1/4] 正在爬取 Finviz Screener (finviz v2.0.0)...")

    try:
        from finviz.screener import Screener

        # 從 config.py 讀取市場和指數設定
        filters = []
        if SELECTED_EXCHANGES:
            filters.extend(SELECTED_EXCHANGES)
            print(f"  [*] 市場條件: {SELECTED_EXCHANGES}")
        if SELECTED_INDICES:
            filters.extend(SELECTED_INDICES)
            print(f"  [*] 指數條件: {SELECTED_INDICES}")

        # 基本篩選條件
        filters.extend([
            'cap_midover',       # 市值 > 300M
            'sh_price_o5',       # 股價 > $5
        ])
        print(f"  [*] 基本條件: 市值 > 300M, 股價 > $5")

        # ===== 取得 Performance 表 =====
        print(f"  [*] 正在執行篩選器 (Performance 表)...")
        screener_perf = Screener(filters=filters, table='Performance')
        print(f"  [OK] Performance 表: {len(screener_perf)} 檔股票")

        try:
            df_perf = screener_perf.to_dataframe()
        except (AttributeError, Exception):
            rows = [stock for stock in screener_perf]
            df_perf = pd.DataFrame(rows)

        # ===== 取得 Overview 表 =====
        print(f"  [*] 正在取得 Overview 資料...")
        screener_overview = Screener(filters=filters, table='Overview')
        print(f"  [OK] Overview 表: {len(screener_overview)} 檔股票")

        try:
            df_overview = screener_overview.to_dataframe()
        except (AttributeError, Exception):
            rows = [stock for stock in screener_overview]
            df_overview = pd.DataFrame(rows)

        if len(df_perf) == 0 and len(df_overview) == 0:
            print("  [!] 篩選條件返回 0 筆，嘗試寬鬆篩選...")
            screener_overview = Screener(filters=['cap_midover'], table='Overview')
            try:
                df_overview = screener_overview.to_dataframe()
            except (AttributeError, Exception):
                rows = [stock for stock in screener_overview]
                df_overview = pd.DataFrame(rows)

        # ===== 合併兩個表 =====
        print(f"  [*] 合併 Performance + Overview 資料...")

        if len(df_overview) > 0 and len(df_perf) > 0:
            # 從 Performance 表取需要的欄位
            perf_cols = ['Ticker']
            for col in ['Perf Day', 'Perf Week', 'Perf Month', 'Perf Quart',
                        'Perf Half', 'Perf Year', 'Perf YTD', 'Volatility',
                        'Recom', 'Avg Volume', 'Rel Volume', 'Change']:
                if col in df_perf.columns:
                    perf_cols.append(col)

            df_perf_subset = df_perf[perf_cols].copy()

            # 去除 Overview 中跟 Performance 重複的欄位
            overlap_cols = [c for c in df_perf_subset.columns
                          if c in df_overview.columns and c != 'Ticker']
            df_overview_clean = df_overview.drop(columns=overlap_cols, errors='ignore')

            df_merged = df_overview_clean.merge(df_perf_subset, on='Ticker', how='left')
        else:
            df_merged = df_overview if len(df_overview) > 0 else df_perf

        # ===== 轉換為標準化格式 =====
        all_stocks = []
        for _, row in df_merged.iterrows():
            ticker = row.get('Ticker', '')
            sector = str(row.get('Sector', ''))
            industry = str(row.get('Industry', ''))

            if is_excluded(ticker, sector):
                continue

            # 當日漲幅：優先用 Change，其次用 Perf Day
            change_str = str(row.get('Change', row.get('Perf Day', '0%')))
            daily_change = parse_finviz_value(change_str)

            # 週漲幅
            perf_week = parse_finviz_value(str(row.get('Perf Week', '0%')))

            # 相對量能
            rel_vol_str = str(row.get('Rel Volume', '1.0'))
            rel_volume = parse_finviz_value(rel_vol_str) if rel_vol_str not in ('', 'nan', 'None') else 1.0

            stock_data = {
                'Ticker': ticker,
                'Company': row.get('Company', ''),
                'Sector': sector,
                'Industry': industry,
                'Market_Cap_Raw': parse_finviz_value(str(row.get('Market Cap', '0'))),
                'Market_Cap': row.get('Market Cap', ''),
                'Price': parse_finviz_value(str(row.get('Price', '0'))),
                'Volume': parse_finviz_value(str(row.get('Volume', '0'))),
                'Rel_Volume': rel_volume,
                'Daily_Change': daily_change,
                'Perf_Week': perf_week,
            }
            all_stocks.append(stock_data)

        df = pd.DataFrame(all_stocks)

        # ===== 關鍵：按信號強度排序，最強的放前面 =====
        # 綜合評分策略：
        #   1. 當日漲幅為正且越高越好（負漲幅 = 弱勢，分數歸零）
        #   2. 相對量能越大代表市場關注度越高
        #   3. 最終只有「漲+放量」的股票會排在前面
        if len(df) > 0:
            df['_change_score'] = df['Daily_Change'].clip(lower=0)  # 負漲幅歸零
            df['_vol_score'] = (df['Rel_Volume'] - 1).clip(lower=0)  # 量能超過平均的部分
            df['_signal_score'] = df['_change_score'] * 2 + df['_vol_score'] * 3
            df = df.sort_values('_signal_score', ascending=False).reset_index(drop=True)
            df = df.drop(columns=['_change_score', '_vol_score', '_signal_score'])
            top5 = df.head(5)[['Ticker', 'Daily_Change', 'Rel_Volume']].to_string(index=False)
            print(f"  [*] 已按信號強度排序，前 5 強：")
            print(f"{top5}")

        print(f"  [OK] Finviz 爬取完成！共 {len(df)} 檔股票\n")
        return df

    except ImportError:
        print("  [!] finviz 庫未安裝，降級使用示例數據")
        print("  請執行: pip install finviz>=2.0.0\n")
        return create_demo_data()
    except Exception as e:
        print(f"  [X] Finviz 爬蟲失敗: {e}")
        print(f"  使用示例數據進行演示...\n")
        return create_demo_data()


def create_demo_data() -> pd.DataFrame:
    """建立演示數據（當爬蟲失敗時使用）"""
    import random
    random.seed(42)

    demo_stocks = [
        {'Ticker': 'AAPL', 'Company': 'Apple Inc.', 'Sector': 'Technology', 'Industry': 'Consumer Electronics',
         'Market_Cap_Raw': 2.8e12, 'Market_Cap': '2.8T', 'Price': 185.32, 'Volume': 52500000, 'Rel_Volume': 2.1, 'Daily_Change': 4.2, 'Perf_Week': 5.2},
        {'Ticker': 'MSFT', 'Company': 'Microsoft Corp.', 'Sector': 'Technology', 'Industry': 'Software',
         'Market_Cap_Raw': 2.5e12, 'Market_Cap': '2.5T', 'Price': 378.45, 'Volume': 18200000, 'Rel_Volume': 1.9, 'Daily_Change': 3.5, 'Perf_Week': 4.8},
        {'Ticker': 'NVDA', 'Company': 'NVIDIA Corp.', 'Sector': 'Technology', 'Industry': 'Semiconductors',
         'Market_Cap_Raw': 1.1e12, 'Market_Cap': '1.1T', 'Price': 875.23, 'Volume': 24100000, 'Rel_Volume': 1.8, 'Daily_Change': 3.1, 'Perf_Week': 3.5},
        {'Ticker': 'TSLA', 'Company': 'Tesla Inc.', 'Sector': 'Consumer Cyclical', 'Industry': 'Auto Manufacturers',
         'Market_Cap_Raw': 8e11, 'Market_Cap': '800B', 'Price': 242.15, 'Volume': 125000000, 'Rel_Volume': 2.3, 'Daily_Change': 5.8, 'Perf_Week': 6.2},
        {'Ticker': 'JPM', 'Company': 'JPMorgan Chase', 'Sector': 'Financial', 'Industry': 'Banks',
         'Market_Cap_Raw': 4.5e11, 'Market_Cap': '450B', 'Price': 195.50, 'Volume': 8000000, 'Rel_Volume': 1.2, 'Daily_Change': 1.5, 'Perf_Week': 2.1},
        {'Ticker': 'XOM', 'Company': 'Exxon Mobil', 'Sector': 'Energy', 'Industry': 'Oil & Gas',
         'Market_Cap_Raw': 4.2e11, 'Market_Cap': '420B', 'Price': 105.25, 'Volume': 12500000, 'Rel_Volume': 1.5, 'Daily_Change': 3.8, 'Perf_Week': 4.5},
        {'Ticker': 'AMZN', 'Company': 'Amazon.com Inc.', 'Sector': 'Consumer Cyclical', 'Industry': 'Internet - Retail',
         'Market_Cap_Raw': 1.5e12, 'Market_Cap': '1.5T', 'Price': 198.50, 'Volume': 42100000, 'Rel_Volume': 1.6, 'Daily_Change': 2.9, 'Perf_Week': 3.8},
        {'Ticker': 'GOOGL', 'Company': 'Alphabet Inc.', 'Sector': 'Technology', 'Industry': 'Internet - Services',
         'Market_Cap_Raw': 1.2e12, 'Market_Cap': '1.2T', 'Price': 155.25, 'Volume': 19500000, 'Rel_Volume': 1.4, 'Daily_Change': 2.1, 'Perf_Week': 2.9},
        {'Ticker': 'META', 'Company': 'Meta (Facebook)', 'Sector': 'Technology', 'Industry': 'Internet - Services',
         'Market_Cap_Raw': 7.5e11, 'Market_Cap': '750B', 'Price': 485.50, 'Volume': 18000000, 'Rel_Volume': 1.7, 'Daily_Change': 4.5, 'Perf_Week': 5.3},
        {'Ticker': 'AMD', 'Company': 'Advanced Micro Devices', 'Sector': 'Technology', 'Industry': 'Semiconductors',
         'Market_Cap_Raw': 1.8e11, 'Market_Cap': '180B', 'Price': 175.30, 'Volume': 45000000, 'Rel_Volume': 2.4, 'Daily_Change': 6.2, 'Perf_Week': 7.2},
        {'Ticker': 'COIN', 'Company': 'Coinbase Global', 'Sector': 'Technology', 'Industry': 'Financial Services',
         'Market_Cap_Raw': 4.5e10, 'Market_Cap': '45B', 'Price': 130.50, 'Volume': 35000000, 'Rel_Volume': 2.8, 'Daily_Change': 7.5, 'Perf_Week': 8.5},
        {'Ticker': 'MRNA', 'Company': 'Moderna Inc.', 'Sector': 'Healthcare', 'Industry': 'Biotechnology',
         'Market_Cap_Raw': 5.5e10, 'Market_Cap': '55B', 'Price': 65.80, 'Volume': 42000000, 'Rel_Volume': 2.2, 'Daily_Change': 8.1, 'Perf_Week': 9.1},
        {'Ticker': 'BNTX', 'Company': 'BioNTech SE', 'Sector': 'Healthcare', 'Industry': 'Biotechnology',
         'Market_Cap_Raw': 3.5e10, 'Market_Cap': '35B', 'Price': 85.25, 'Volume': 28000000, 'Rel_Volume': 1.9, 'Daily_Change': 5.7, 'Perf_Week': 6.7},
        {'Ticker': 'WMT', 'Company': 'Walmart Inc.', 'Sector': 'Consumer Defensive', 'Industry': 'Retail',
         'Market_Cap_Raw': 3.8e11, 'Market_Cap': '380B', 'Price': 95.30, 'Volume': 7200000, 'Rel_Volume': 1.3, 'Daily_Change': 1.2, 'Perf_Week': 1.8},
        {'Ticker': 'JNJ', 'Company': 'Johnson & Johnson', 'Sector': 'Healthcare', 'Industry': 'Pharmaceuticals',
         'Market_Cap_Raw': 3.6e11, 'Market_Cap': '360B', 'Price': 155.80, 'Volume': 5100000, 'Rel_Volume': 0.9, 'Daily_Change': 0.3, 'Perf_Week': 0.5},
        {'Ticker': 'RIOT', 'Company': 'Riot Platforms', 'Sector': 'Technology', 'Industry': 'Mining',
         'Market_Cap_Raw': 1.2e10, 'Market_Cap': '12B', 'Price': 18.75, 'Volume': 95000000, 'Rel_Volume': 3.2, 'Daily_Change': 11.2, 'Perf_Week': 12.3},
        {'Ticker': 'PG', 'Company': 'Procter & Gamble', 'Sector': 'Consumer Defensive', 'Industry': 'Household Products',
         'Market_Cap_Raw': 3.6e11, 'Market_Cap': '360B', 'Price': 165.25, 'Volume': 4200000, 'Rel_Volume': 0.8, 'Daily_Change': -0.3, 'Perf_Week': -0.3},
    ]

    return pd.DataFrame(demo_stocks)


def enrich_with_yfinance(df: pd.DataFrame) -> pd.DataFrame:
    """使用 Yahoo Finance 補充詳細資料（財報日期、目標價、新聞）"""
    max_stocks = MAX_STOCKS_TO_PROCESS
    print(f"[步驟 2/4] 使用 Yahoo Finance 補充資料（信號最強的 {max_stocks} 檔）...")
    print(f"  [*] 注意：503 檔股票已按漲幅+量能排序，只查詢最強的前 {max_stocks} 檔")

    enriched_data = []
    df_subset = df.head(max_stocks)

    for idx, row in df_subset.iterrows():
        ticker_symbol = row['Ticker']
        print(f"  ({idx+1}/{len(df_subset)}) 查詢 {ticker_symbol}...", end=' ')

        try:
            def _fetch():
                ticker = yf.Ticker(ticker_symbol)
                info = ticker.info

                # 財報日期
                earnings_date = None
                try:
                    calendar = ticker.calendar
                    if calendar is not None and 'Earnings Date' in calendar:
                        dates = calendar['Earnings Date']
                        if len(dates) > 0:
                            earnings_date = dates[0]
                except Exception:
                    pass

                # 分析師目標價
                target_price = info.get('targetMeanPrice', None)
                current_price = row['Price']
                upside_pct = ((target_price - current_price) / current_price * 100) if target_price and current_price > 0 else 0

                # EPS 預估 & 分析師數
                eps_estimate = info.get('forwardEps', None)
                num_analysts = info.get('numberOfAnalystOpinions', 0)

                # 最新新聞標題
                news_headline = ""
                try:
                    news = ticker.news
                    if news and len(news) > 0:
                        news_headline = news[0].get('title', '')[:80]
                except Exception:
                    pass

                return {
                    'Earnings_Date': earnings_date,
                    'Target_Price': target_price,
                    'Upside_Pct': upside_pct,
                    'EPS_Estimate': eps_estimate,
                    'Num_Analysts': num_analysts or 0,
                    'News_Headline': news_headline,
                }

            result = retry_on_failure(_fetch, max_retries=2, delay=1.0)
            enriched_data.append({**row.to_dict(), **result})
            print("[OK]")
            time.sleep(API_DELAY)

        except Exception as e:
            print(f"[X] (錯誤: {e})")
            enriched_data.append({
                **row.to_dict(),
                'Earnings_Date': None,
                'Target_Price': None,
                'Upside_Pct': 0,
                'EPS_Estimate': None,
                'Num_Analysts': 0,
                'News_Headline': '',
            })

    result_df = pd.DataFrame(enriched_data)
    print(f"  [OK] Yahoo Finance 資料補充完成！\n")
    return result_df


def fetch_analyst_target_changes(tickers: List[str]) -> Dict[str, dict]:
    """
    使用 finviz.get_analyst_price_targets() 偵測近期目標價大幅調整

    返回格式: {ticker: {'latest_target': float, 'target_change_pct': float,
                        'rating': str, 'analyst': str, 'date': str}}
    """
    print("  [*] 查詢分析師目標價變動 (finviz)...")
    results = {}

    try:
        import finviz
    except ImportError:
        print("  [!] finviz 庫未安裝，跳過目標價變動查詢")
        return results

    for ticker in tickers[:20]:  # 限制查詢量避免被封
        try:
            targets = finviz.get_analyst_price_targets(ticker, last_ratings=5)
            if targets and len(targets) > 0:
                latest = targets[0]
                target_from = latest.get('target_from', 0) or 0
                target_to = latest.get('target_to', 0) or 0

                # 計算目標價調整幅度
                if target_from > 0 and target_to > 0:
                    change_pct = ((target_to - target_from) / target_from) * 100
                else:
                    change_pct = 0

                results[ticker] = {
                    'latest_target': target_to,
                    'target_change_pct': change_pct,
                    'rating': latest.get('rating', ''),
                    'analyst': latest.get('analyst', ''),
                    'date': latest.get('date', ''),
                }
            time.sleep(0.3)
        except Exception:
            continue

    print(f"  [OK] 取得 {len(results)} 檔分析師目標價變動\n")
    return results


# ============ 篩選函數 ============

def filter_sheet1_launch(df: pd.DataFrame) -> pd.DataFrame:
    """
    Sheet 1 - 起飛清單（起飛信號）
    條件：今日漲幅 > LAUNCH_MIN_GAIN%
          量能倍數 > LAUNCH_MIN_REL_VOL
          股價 > LAUNCH_MIN_PRICE
          市值 > LAUNCH_MIN_MCAP
    """
    print("[步驟 3/4] 篩選起飛清單...")

    filtered = df[
        (df['Daily_Change'] > LAUNCH_MIN_GAIN) &
        (df['Rel_Volume'] > LAUNCH_MIN_REL_VOL) &
        (df['Price'] > LAUNCH_MIN_PRICE) &
        (df['Market_Cap_Raw'] > LAUNCH_MIN_MCAP)
    ].copy()

    if len(filtered) == 0:
        print("  [!] 起飛清單篩選結果為 0，返回空表\n")
        return pd.DataFrame()

    filtered = _apply_sector_classification(filtered)

    # A 級加成：量能特別大的優先產業股票
    filtered['Rating'] = filtered.apply(
        lambda r: 'A' if r['Rating'] == 'A' and r['Rel_Volume'] > RATING_A_REL_VOL else r['Rating'],
        axis=1
    )

    # 排序：評級 > 量能
    filtered['Rating_Score'] = filtered['Rating'].map({'A': 3, 'B': 2, 'C': 1})
    filtered = filtered.sort_values(['Rating_Score', 'Rel_Volume'], ascending=[False, False])

    # 輸出欄位（包含新聞原因）
    cols_map = {
        'Ticker': '股票代碼',
        'Daily_Change': '今日漲幅%',
        'Rel_Volume': '量能倍數',
        'Market_Cap': '市值',
        'Price': '股價',
        'Priority_Sector': '產業',
        'Rating': '評級',
        'News_Headline': '新聞原因',
    }
    available = {k: v for k, v in cols_map.items() if k in filtered.columns}
    output = filtered[list(available.keys())].head(TOP_N_STOCKS).copy()
    output.columns = list(available.values())

    print(f"  [OK] 篩選出 {len(output)} 檔起飛股票\n")
    return output


def filter_sheet2_earnings(df: pd.DataFrame) -> pd.DataFrame:
    """
    Sheet 2 - 財報預熱
    條件：EARNINGS_DAYS_AHEAD 天內有財報
          市值 > EARNINGS_MIN_MCAP
          優先產業優先排序
    """
    print("[步驟 3/4] 篩選財報預熱清單...")

    df_with_earnings = df[df['Earnings_Date'].notna()].copy()

    today = datetime.now()
    df_with_earnings['Days_To_Earnings'] = df_with_earnings['Earnings_Date'].apply(
        lambda x: (pd.to_datetime(x) - today).days if x else 999
    )

    filtered = df_with_earnings[
        (df_with_earnings['Days_To_Earnings'] >= 0) &
        (df_with_earnings['Days_To_Earnings'] <= EARNINGS_DAYS_AHEAD) &
        (df_with_earnings['Market_Cap_Raw'] > EARNINGS_MIN_MCAP)
    ].copy()

    if len(filtered) == 0:
        print("  [!] 沒有找到財報預熱信息，返回空表\n")
        return pd.DataFrame()

    filtered = _apply_sector_classification(filtered)
    filtered['Rating_Score'] = filtered['Rating'].map({'A': 3, 'B': 2, 'C': 1})
    filtered = filtered.sort_values(['Rating_Score', 'Market_Cap_Raw'], ascending=[False, False])

    cols_map = {
        'Ticker': '股票代碼',
        'Earnings_Date': '財報日期',
        'EPS_Estimate': '預估EPS',
        'Market_Cap': '市值',
        'Priority_Sector': '產業',
        'Rating': '評級',
    }
    available = {k: v for k, v in cols_map.items() if k in filtered.columns}
    output = filtered[list(available.keys())].head(TOP_N_STOCKS).copy()
    output.columns = list(available.values())

    print(f"  [OK] 篩選出 {len(output)} 檔財報預熱股票\n")
    return output


def filter_sheet3_analyst(df: pd.DataFrame, target_changes: Dict = None) -> pd.DataFrame:
    """
    Sheet 3 - 預測情報
    條件：目標價上漲空間 > ANALYST_MIN_UPSIDE%
          分析師數 >= ANALYST_MIN_COUNT
    額外：整合 finviz 分析師目標價變動偵測（偵測「大調」事件）
    """
    print("[步驟 3/4] 篩選預測情報...")

    filtered = df[
        (df['Upside_Pct'] > ANALYST_MIN_UPSIDE) &
        (df['Num_Analysts'] >= ANALYST_MIN_COUNT) &
        (df['Target_Price'].notna())
    ].copy()

    if len(filtered) == 0:
        print("  [!] 預測情報篩選結果為 0，返回空表\n")
        return pd.DataFrame()

    filtered = _apply_sector_classification(filtered)
    filtered['Rating'] = filtered.apply(
        lambda r: 'A' if r['Upside_Pct'] > RATING_A_UPSIDE else r['Rating'], axis=1
    )

    # 整合分析師目標價大幅調整資訊
    filtered['目標價調整'] = ''
    filtered['事件類型'] = '分析師看好'
    if target_changes:
        for idx, row in filtered.iterrows():
            tc = target_changes.get(row['Ticker'])
            if tc:
                change_pct = tc.get('target_change_pct', 0)
                if abs(change_pct) > 10:  # 調整幅度 > 10% 才標記為「大調」
                    direction = '上調' if change_pct > 0 else '下調'
                    filtered.at[idx, '目標價調整'] = f"{direction} {abs(change_pct):.0f}% ({tc.get('analyst', '')})"
                    filtered.at[idx, '事件類型'] = f'目標價{direction}'

    filtered = filtered.sort_values('Upside_Pct', ascending=False)

    cols_map = {
        'Ticker': '股票代碼',
        '事件類型': '事件類型',
        'Target_Price': '目標價',
        'Upside_Pct': '預期漲幅%',
        'Num_Analysts': '分析師數',
        '目標價調整': '目標價調整',
        'Priority_Sector': '產業',
        'Rating': '評級',
    }
    available = {k: v for k, v in cols_map.items() if k in filtered.columns}
    output = filtered[list(available.keys())].head(TOP_N_STOCKS).copy()
    output.columns = list(available.values())

    print(f"  [OK] 篩選出 {len(output)} 檔預測標的\n")
    return output



def display_summary(sheet1: pd.DataFrame, sheet2: pd.DataFrame, sheet3: pd.DataFrame):
    """顯示掃描結果摘要"""
    print("\n" + "=" * 70)
    print("  Alpha Finder 每日掃描結果摘要".center(60))
    print("=" * 70)

    col_ticker = '股票代碼'

    print(f"\n>> 起飛清單 Top {TOP_N_STOCKS}")
    if len(sheet1) > 0:
        for i, (_, row) in enumerate(sheet1.iterrows(), 1):
            change = row.get('今日漲幅%', 0)
            vol = row.get('量能倍數', 0)
            rating = row.get('評級', 'N/A')
            news = row.get('新聞原因', '')
            news_str = f" | 原因: {news}" if news else ""
            print(f"  {i}. {row[col_ticker]} - 漲幅 {change:.1f}% | 量能 {vol:.1f}x | 評級 {rating}{news_str}")
    else:
        print("  無符合條件的股票")

    print(f"\n>> 財報預熱 Top {TOP_N_STOCKS}")
    if len(sheet2) > 0:
        for i, (_, row) in enumerate(sheet2.iterrows(), 1):
            print(f"  {i}. {row[col_ticker]} - 財報 {row.get('財報日期', 'N/A')} | 產業 {row.get('產業', 'N/A')} | 評級 {row.get('評級', 'N/A')}")
    else:
        print("  無符合條件的股票")

    print(f"\n>> 預測情報 Top {TOP_N_STOCKS}")
    if len(sheet3) > 0:
        for i, (_, row) in enumerate(sheet3.iterrows(), 1):
            upside = row.get('預期漲幅%', 0)
            target = row.get('目標價', 0)
            change_info = row.get('目標價調整', '')
            change_str = f" | {change_info}" if change_info else ""
            print(f"  {i}. {row[col_ticker]} - 上漲空間 {upside:.1f}% | 目標價 ${target:.2f} | 評級 {row.get('評級', 'N/A')}{change_str}")
    else:
        print("  無符合條件的股票")

    print("\n" + "=" * 70)
    print("  掃描完成！請查看 CSV 檔案瞭解詳細資訊".center(60))
    print("=" * 70 + "\n")


# ============ 主程式 ============

def main():
    """主程序入口"""
    try:
        print_banner()

        # 步驟 1: 爬取 Finviz
        df_finviz = scrape_finviz_screener()

        if len(df_finviz) == 0:
            print("[X] 錯誤：未從 Finviz 取得任何數據")
            sys.exit(1)

        # 步驟 2: 補充 Yahoo Finance 資料
        df_enriched = enrich_with_yfinance(df_finviz)

        # 步驟 2.5: 查詢分析師目標價變動（用於 Sheet 3 強化）
        tickers_for_analyst = df_enriched['Ticker'].tolist()
        target_changes = fetch_analyst_target_changes(tickers_for_analyst)

        # 步驟 3: 篩選三個清單
        sheet1 = filter_sheet1_launch(df_enriched)
        sheet2 = filter_sheet2_earnings(df_enriched)
        sheet3 = filter_sheet3_analyst(df_enriched, target_changes=target_changes)

        # 顯示摘要（terminal 確認用）
        display_summary(sheet1, sheet2, sheet3)

        # 步驟 4: 上傳到 Google Sheets
        if GSHEET_AVAILABLE and GSHEET_ENABLED:
            print("\n" + "=" * 70)
            print("  Google Sheets 上傳".center(60))
            print("=" * 70)

            uploader = GoogleSheetsUploader()

            if uploader.authenticate():
                if uploader.get_or_create_spreadsheet():
                    # 上傳全量數據（AI 分析用）
                    uploader.upload_full_data(df_enriched)
                    # 上傳每日精選報告（Top 3 三合一）
                    uploader.upload_daily_report(sheet1, sheet2, sheet3)
                else:
                    print("[!] 跳過 Google Sheets 上傳")
            else:
                print("[!] 跳過 Google Sheets 上傳")

        print("\n[DONE] Alpha Finder 掃描完成！祝交易順利！")

    except KeyboardInterrupt:
        print("\n\n[!] 使用者中斷執行")
        sys.exit(0)
    except Exception as e:
        print(f"\n\n[X] 嚴重錯誤: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

