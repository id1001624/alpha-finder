"""
XQ 選股清單更新腳本
自動從 Yahoo Finance 抓取歷史價格,並更新到 XQ 匯出的 CSV 檔案

功能:
- 掃描 XQ_exports 資料夾下所有 CSV 檔案
- 抓取每支股票最近 1/3/5 日的歷史價格與量能指標
- 新增欄位: chg_1d_pct, chg_3d_pct, chg_5d_pct, vol_strength, short_trade_score,
  swing_score, momentum_mix, continuation_grade, prob_next_day, prob_day2, decision_tag_hint
- 執行完畢後顯示各檔案短炒分數 Top 5
- 不在 XQ_exports 產生 *_updated.csv，統一更新 `repo_outputs/ai_ready/latest/xq_short_term_updated.csv`
- 自動處理中文編碼問題

使用方式:
    python scripts/update_xq_with_history.py
    python scripts/update_xq_with_history.py --file 妖股來吧起來0206.csv
"""

import os
import sys
import shutil
import json
from pathlib import Path
from datetime import datetime, timedelta
import argparse
import time
import re


def _fix_ssl_cert_path():
    """修復 SSL 憑證路徑中文問題（與 main.py 相同）"""
    try:
        import certifi
        original = certifi.where()
        try:
            original.encode('ascii')
            return
        except UnicodeEncodeError:
            pass

        safe_dir = os.path.join(os.path.expanduser('~'), '.alpha_finder_certs')
        os.makedirs(safe_dir, exist_ok=True)
        safe_cert = os.path.join(safe_dir, 'cacert.pem')

        if not os.path.exists(safe_cert) or \
           os.path.getmtime(original) > os.path.getmtime(safe_cert):
            shutil.copy2(original, safe_cert)

        os.environ['CURL_CA_BUNDLE'] = safe_cert
        os.environ['SSL_CERT_FILE'] = safe_cert
        os.environ['REQUESTS_CA_BUNDLE'] = safe_cert
        os.environ['SSL_NO_VERIFY'] = '0'
    except Exception:
        pass


_fix_ssl_cert_path()

import pandas as pd
import yfinance as yf

# 1/3/5 日核心設定（短炒版）
LOOKBACK_WINDOWS = [1, 3, 5]
MAX_LOOKBACK = max(LOOKBACK_WINDOWS)
FETCH_BUFFER_DAYS = 20

COL_1D_CHANGE_PCT = "chg_1d_pct"
COL_3D_CHANGE_PCT = "chg_3d_pct"
COL_5D_CHANGE_PCT = "chg_5d_pct"
COL_5D_AVG_PRICE = "avg_price_5d"
COL_5D_HIGH = "high_5d"
COL_5D_LOW = "low_5d"
COL_YDAY_VOLUME = "yday_volume"
COL_5D_AVG_VOLUME = "avg_volume_5d"
COL_VOL_STRENGTH = "vol_strength"
COL_DOLLAR_VOL_M = "dollar_volume_m"
COL_SHORT_SCORE = "short_trade_score"
COL_SWING_SCORE = "swing_score"
COL_MOMENTUM_MIX = "momentum_mix"
COL_CONTINUATION_GRADE = "continuation_grade"
COL_PROB_NEXT_DAY = "prob_next_day"
COL_PROB_DAY2 = "prob_day2"
COL_REVERSAL_FLAGS = "reversal_flags"
COL_DECISION_TAG_HINT = "decision_tag_hint"
COL_AI_QUERY_HINT = "ai_query_hint"

# 自動尋找 XQ_exports 資料夾 (優先順序: 腳本同層 > 腳本上層 > 當前工作目錄)
def find_xq_exports_dir():
    """智慧尋找 XQ_exports 資料夾,支援腳本任意位置執行"""
    script_dir = Path(__file__).parent
    
    # 1. 檢查腳本所在目錄
    candidate1 = script_dir / "XQ_exports"
    if candidate1.exists():
        return candidate1
    
    # 2. 檢查腳本上一層目錄
    candidate2 = script_dir.parent / "XQ_exports"
    if candidate2.exists():
        return candidate2
    
    # 3. 檢查當前工作目錄
    candidate3 = Path.cwd() / "XQ_exports"
    if candidate3.exists():
        return candidate3
    
    # 找不到就返回預設位置 (腳本上層)
    return script_dir.parent / "XQ_exports"

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from power_awake import keep_system_awake

try:
    from config import AI_READY_OUTPUT_ENABLED, AI_READY_OUTPUT_DIR
except Exception:
    AI_READY_OUTPUT_ENABLED = True
    AI_READY_OUTPUT_DIR = "repo_outputs/ai_ready"

XQ_EXPORTS_DIR = find_xq_exports_dir()
BACKTEST_OUTPUT_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
PICK_LOG_FILE = BACKTEST_OUTPUT_DIR / "xq_pick_log.csv"
DAILY_PICKS_DIR = BACKTEST_OUTPUT_DIR / "daily_xq_picks"
TOP_PICKS_PER_FILE = 10
AI_XQ_TARGET_FILE = "xq_short_term_updated.csv"
AI_XQ_MANIFEST_FILE = "xq_short_term_manifest.json"

COLUMN_RENAME_MAP = {
    "序號": "index",
    "编号": "index",
    "代碼": "symbol",
    "代号": "symbol",
    "代號": "symbol",
    "商品": "name",
    "成交": "value",
    "漲幅%": "change_pct",
    "總量": "volume",
    "區間漲幅%": "range_change_pct",
}


def detect_encoding(file_path):
    """
    自動偵測 CSV 檔案編碼
    XQ 匯出通常是 BIG5 或 CP950 (繁體中文)
    """
    encodings = ['cp950', 'big5', 'utf-8', 'gbk', 'utf-8-sig']
    
    for encoding in encodings:
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                f.read()
            print(f"✅ 偵測到編碼: {encoding}")
            return encoding
        except (UnicodeDecodeError, LookupError):
            continue
    
    print("⚠️ 無法偵測編碼,使用預設 cp950")
    return 'cp950'


def read_xq_csv(file_path):
    """
    讀取 XQ 匯出的 CSV 檔案
    
    XQ 格式:
    - 第 1 行: 報告標題
    - 第 2 行: 日期資訊
    - 第 3 行: 說明
    - 第 4 行: 欄位名稱
    - 第 5 行起: 實際數據
    
    Returns:
        DataFrame: 包含股票代碼和資訊的資料表
    """
    encoding = detect_encoding(file_path)
    
    try:
        # 跳過前 3 行標題,從第 4 行讀取欄位名稱
        df = pd.read_csv(file_path, encoding=encoding, skiprows=3)
        
        # 移除第一欄的序號 (如果全是數字)
        if df.columns[0].strip() in ['序號', '编号', ''] or df.iloc[:, 0].dtype in ['int64', 'float64']:
            # 檢查第一欄是否為純數字
            try:
                pd.to_numeric(df.iloc[:, 0], errors='raise')
                # 是序號,可以刪除
                df = df.iloc[:, 1:]
            except:
                pass
        
        # 清理欄位名稱
        df.columns = df.columns.str.strip()
        
        print(f"📊 讀取到 {len(df)} 支股票")
        print(f"欄位: {list(df.columns)[:6]}...")  # 只顯示前 6 個欄位
        
        return df
            
    except Exception as e:
        print(f"❌ 讀取 CSV 失敗: {e}")
        # 嘗試手動解析
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                lines = f.readlines()
            
            # 找到欄位標題行 (包含 "代號" 或 "Symbol" 的那行)
            header_row = None
            for i, line in enumerate(lines):
                if '代號' in line or 'Symbol' in line or '代号' in line:
                    header_row = i
                    break
            
            if header_row is not None:
                df = pd.read_csv(file_path, encoding=encoding, skiprows=header_row)
                df.columns = df.columns.str.strip()
                print(f"📊 手動解析成功,讀取到 {len(df)} 支股票")
                return df
            else:
                print(f"❌ 無法找到欄位標題行")
                return None
        except Exception as e2:
            print(f"❌ 手動解析也失敗: {e2}")
            return None


def extract_ticker(row, ticker_column=None):
    """
    從資料列中提取股票代碼
    
    Args:
        row: DataFrame 的一列
        ticker_column: 股票代碼欄位名稱
    
    Returns:
        str: 股票代碼 (例如: "AAPL.US" -> "AAPL")
    """
    if ticker_column and ticker_column in row.index:
        ticker = str(row[ticker_column]).strip()
    else:
        # 自動尋找包含 .US 的欄位 (通常是第一或第二欄)
        for value in row.values[:3]:  # 只檢查前 3 欄
            value_str = str(value).strip()
            if '.US' in value_str or (len(value_str) <= 5 and value_str.isupper()):
                ticker = value_str
                break
        else:
            # 預設使用第一欄 (假設已移除序號)
            ticker = str(row.iloc[0]).strip()
    
    # 移除 .US 後綴
    ticker = ticker.replace('.US', '').replace('.us', '')
    
    return ticker


def fetch_history(ticker, days=MAX_LOOKBACK):
    """
    從 Yahoo Finance 抓取指定天數的歷史價格
    
    Args:
        ticker: 股票代碼 (例如: "AAPL")
        days: 回溯天數
    
    Returns:
        DataFrame: 歷史價格資料 (包含 Open, High, Low, Close, Volume)
    """
    try:
        # 計算起始日期 (多抓幾天確保有足夠交易日)
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days + FETCH_BUFFER_DAYS)
        
        # 下載數據
        stock = yf.Ticker(ticker)
        hist = stock.history(start=start_date, end=end_date)
        
        if hist.empty:
            print(f"⚠️ {ticker}: 無歷史數據")
            return None
        
        # 取最近 N 個交易日
        hist = hist.tail(max(days, MAX_LOOKBACK + 1))
        
        return hist
    
    except Exception as e:
        print(f"❌ {ticker}: 抓取失敗 - {e}")
        return None


def _calc_change_pct(close_series, days):
    if close_series is None or len(close_series) < days + 1:
        return None
    current_price = close_series.iloc[-1]
    base_price = close_series.iloc[-(days + 1)]
    if pd.isna(current_price) or pd.isna(base_price) or base_price == 0:
        return None
    return round(((current_price / base_price) - 1) * 100, 2)


def _calc_short_trade_score(chg_1d, chg_3d, chg_5d, vol_strength):
    vals = [chg_1d, chg_3d, chg_5d, vol_strength]
    if any(v is None or pd.isna(v) for v in vals):
        return None

    score = (
        (float(chg_1d) * 0.45) +
        (float(chg_3d) * 0.35) +
        (float(chg_5d) * 0.20) +
        (max(float(vol_strength) - 1.0, 0) * 8.0)
    )
    return round(score, 2)


def _calc_swing_score(chg_3d, chg_5d, vol_strength):
    vals = [chg_3d, chg_5d, vol_strength]
    if any(v is None or pd.isna(v) for v in vals):
        return None

    score = (
        (float(chg_3d) * 0.35) +
        (float(chg_5d) * 0.35) +
        (float(vol_strength) * 0.30)
    )
    return round(score, 2)


def _calc_reversal_levels(chg_1d, chg_5d, vol_strength):
    vals = [chg_1d, chg_5d, vol_strength]
    if any(v is None or pd.isna(v) for v in vals):
        return 0, "none"

    levels = 0
    flags = []

    if float(chg_1d) > 15 and float(vol_strength) < 1.5:
        levels += 1
        flags.append("high_spike_low_volume")

    if float(chg_5d) > 25 and float(chg_1d) < -1:
        levels += 1
        flags.append("five_day_run_pullback")

    if float(chg_1d) < -3:
        levels += 1
        flags.append("daily_negative_break")

    return min(levels, 2), "|".join(flags) if flags else "none"


def _downgrade_grade(grade, levels):
    order = ["A", "B", "C", "D"]
    if grade not in order:
        return "D"
    idx = order.index(grade)
    idx = min(idx + max(int(levels), 0), len(order) - 1)
    return order[idx]


def _grade_to_probability_ranges(grade):
    mapping = {
        "A": ("70-80%", "60-70%"),
        "B": ("55-68%", "48-60%"),
        "C": ("45-58%", "38-50%"),
        "D": ("30-45%", "25-40%"),
    }
    return mapping.get(grade, ("30-45%", "25-40%"))


def _calc_continuation_outlook(short_score, swing_score, chg_1d, chg_5d, vol_strength):
    vals = [short_score, swing_score, chg_1d, chg_5d, vol_strength]
    if any(v is None or pd.isna(v) for v in vals):
        return {
            COL_MOMENTUM_MIX: None,
            COL_CONTINUATION_GRADE: None,
            COL_PROB_NEXT_DAY: None,
            COL_PROB_DAY2: None,
            COL_REVERSAL_FLAGS: "none",
        }

    momentum_mix = round((float(short_score) * 0.6) + (float(swing_score) * 0.4), 2)

    if momentum_mix >= 16 and float(vol_strength) >= 2.0:
        base_grade = "A"
    elif momentum_mix >= 12:
        base_grade = "B"
    elif momentum_mix >= 8:
        base_grade = "C"
    else:
        base_grade = "D"

    penalty_levels, reversal_flags = _calc_reversal_levels(chg_1d, chg_5d, vol_strength)
    final_grade = _downgrade_grade(base_grade, penalty_levels)
    prob_next_day, prob_day2 = _grade_to_probability_ranges(final_grade)

    return {
        COL_MOMENTUM_MIX: momentum_mix,
        COL_CONTINUATION_GRADE: final_grade,
        COL_PROB_NEXT_DAY: prob_next_day,
        COL_PROB_DAY2: prob_day2,
        COL_REVERSAL_FLAGS: reversal_flags,
    }


def _build_decision_tag_hint(short_score, chg_1d, chg_5d, vol_strength):
    vals = [short_score, chg_1d, chg_5d, vol_strength]
    if any(v is None or pd.isna(v) for v in vals):
        return "watch"

    short_score = float(short_score)
    chg_1d = float(chg_1d)
    chg_5d = float(chg_5d)
    vol_strength = float(vol_strength)

    overheat = chg_1d > 12 and vol_strength < 1.3
    pullback = chg_5d > 20 and chg_1d < -2

    if short_score < 10 or overheat or pullback:
        return "replace_candidate"
    if short_score >= 20 and vol_strength >= 1.8 and not overheat:
        return "keep"
    return "watch"


def build_ai_query_hint(ticker):
    return (
        f"查詢 {ticker} 最新題材催化、隔夜新聞、財報日與市場共識，"
        "重點確認是否有新公告、法說、指引變動、或同族群輪動。"
    )


def calculate_metrics(hist):
    """
    計算歷史數據的統計指標
    
    Args:
        hist: 歷史價格 DataFrame
        hist: 歷史價格 DataFrame
    
    Returns:
        dict: 包含各種統計指標
    """
    if hist is None or hist.empty:
        return {
            COL_1D_CHANGE_PCT: None,
            COL_3D_CHANGE_PCT: None,
            COL_5D_CHANGE_PCT: None,
            COL_5D_AVG_PRICE: None,
            COL_5D_HIGH: None,
            COL_5D_LOW: None,
            COL_YDAY_VOLUME: None,
            COL_5D_AVG_VOLUME: None,
            COL_VOL_STRENGTH: None,
            COL_DOLLAR_VOL_M: None,
            COL_SHORT_SCORE: None,
            COL_SWING_SCORE: None,
            COL_MOMENTUM_MIX: None,
            COL_CONTINUATION_GRADE: None,
            COL_PROB_NEXT_DAY: None,
            COL_PROB_DAY2: None,
            COL_REVERSAL_FLAGS: "none",
            COL_DECISION_TAG_HINT: "watch",
        }
    
    recent = hist.tail(MAX_LOOKBACK + 1)
    close_series = recent['Close'].dropna()
    high_series = recent['High'].dropna()
    low_series = recent['Low'].dropna()
    volume_series = recent['Volume'].dropna()

    yday_volume = float(volume_series.iloc[-1]) if len(volume_series) >= 1 else None
    avg_volume_5d = float(volume_series.tail(5).mean()) if len(volume_series) >= 5 else None
    vol_strength = None
    if yday_volume is not None and avg_volume_5d and avg_volume_5d > 0:
        vol_strength = round(yday_volume / avg_volume_5d, 2)

    last_close = float(close_series.iloc[-1]) if len(close_series) >= 1 else None
    dollar_volume_m = None
    if last_close is not None and yday_volume is not None:
        dollar_volume_m = round((last_close * yday_volume) / 1_000_000, 2)

    chg_1d = _calc_change_pct(close_series, 1)
    chg_3d = _calc_change_pct(close_series, 3)
    chg_5d = _calc_change_pct(close_series, 5)

    short_score = _calc_short_trade_score(chg_1d, chg_3d, chg_5d, vol_strength)
    swing_score = _calc_swing_score(chg_3d, chg_5d, vol_strength)
    continuation_outlook = _calc_continuation_outlook(short_score, swing_score, chg_1d, chg_5d, vol_strength)
    decision_tag_hint = _build_decision_tag_hint(short_score, chg_1d, chg_5d, vol_strength)

    return {
        COL_1D_CHANGE_PCT: chg_1d,
        COL_3D_CHANGE_PCT: chg_3d,
        COL_5D_CHANGE_PCT: chg_5d,
        COL_5D_AVG_PRICE: round(close_series.tail(5).mean(), 2) if len(close_series) >= 5 else None,
        COL_5D_HIGH: round(high_series.tail(5).max(), 2) if len(high_series) >= 5 else None,
        COL_5D_LOW: round(low_series.tail(5).min(), 2) if len(low_series) >= 5 else None,
        COL_YDAY_VOLUME: int(yday_volume) if yday_volume is not None else None,
        COL_5D_AVG_VOLUME: int(avg_volume_5d) if avg_volume_5d is not None else None,
        COL_VOL_STRENGTH: vol_strength,
        COL_DOLLAR_VOL_M: dollar_volume_m,
        COL_SHORT_SCORE: short_score,
        COL_SWING_SCORE: swing_score,
        COL_MOMENTUM_MIX: continuation_outlook[COL_MOMENTUM_MIX],
        COL_CONTINUATION_GRADE: continuation_outlook[COL_CONTINUATION_GRADE],
        COL_PROB_NEXT_DAY: continuation_outlook[COL_PROB_NEXT_DAY],
        COL_PROB_DAY2: continuation_outlook[COL_PROB_DAY2],
        COL_REVERSAL_FLAGS: continuation_outlook[COL_REVERSAL_FLAGS],
        COL_DECISION_TAG_HINT: decision_tag_hint,
    }


def print_top_movers(df, ticker_column):
    """
    顯示指定天數的漲幅 Top 5

    Args:
        df: 更新後的 DataFrame
        ticker_column: 股票代碼欄位名稱
        days: 天數
    """
    score_col = COL_SHORT_SCORE
    if score_col not in df.columns:
        print(f"⚠️ 找不到欄位: {score_col}")
        return

    temp = df.copy()
    if ticker_column in temp.columns:
        temp['_ticker'] = temp[ticker_column].astype(str).str.strip()
    else:
        temp['_ticker'] = temp.apply(lambda row: extract_ticker(row), axis=1)

    for metric_col in [score_col, COL_1D_CHANGE_PCT, COL_3D_CHANGE_PCT, COL_5D_CHANGE_PCT, COL_VOL_STRENGTH]:
        if metric_col in temp.columns:
            temp[metric_col] = pd.to_numeric(temp[metric_col], errors='coerce')

    top5 = temp.dropna(subset=[score_col]).sort_values(score_col, ascending=False).head(5)

    if top5.empty:
        print("⚠️ 無可用的短炒分數數據")
        return

    print("\nShort-trade score Top 5:")
    for _, row in top5.iterrows():
        print(
            f"{row['_ticker']}: score={row.get(COL_SHORT_SCORE)} | "
            f"swing={row.get(COL_SWING_SCORE)} | "
            f"grade={row.get(COL_CONTINUATION_GRADE)} | "
            f"1d={row.get(COL_1D_CHANGE_PCT)}% | "
            f"3d={row.get(COL_3D_CHANGE_PCT)}% | "
            f"5d={row.get(COL_5D_CHANGE_PCT)}% | "
            f"vol={row.get(COL_VOL_STRENGTH)}x"
        )


def normalize_column_name(name):
    """
    將欄位名稱正規化為 ASCII 字元
    """
    ascii_name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    return ascii_name if ascii_name else None


def rename_columns_to_english(df):
    """
    將所有欄位名稱轉為 ASCII 英文,避免中文標頭造成編碼問題
    """
    rename_map = {}
    used_names = set()

    for idx, col in enumerate(df.columns, start=1):
        clean_col = str(col).strip()

        if clean_col in COLUMN_RENAME_MAP:
            new_name = COLUMN_RENAME_MAP[clean_col]
        elif "收盤價" in clean_col:
            new_name = "close_price"
        else:
            try:
                clean_col.encode("ascii")
                new_name = normalize_column_name(clean_col) or f"col_{idx}"
            except UnicodeEncodeError:
                new_name = f"col_{idx}"

        base_name = new_name
        suffix = 2
        while new_name in used_names:
            new_name = f"{base_name}_{suffix}"
            suffix += 1

        rename_map[col] = new_name
        used_names.add(new_name)

    df = df.rename(columns=rename_map)
    return df, rename_map


def build_top_picks_snapshot(df, ticker_column, source_name, top_n=TOP_PICKS_PER_FILE):
    if df is None or len(df) == 0:
        return pd.DataFrame()

    temp = df.copy()
    if ticker_column in temp.columns:
        temp['_ticker'] = temp[ticker_column].astype(str).str.strip().str.upper()
    else:
        temp['_ticker'] = temp.apply(lambda row: extract_ticker(row).upper(), axis=1)

    numeric_cols = [
        COL_SHORT_SCORE,
        COL_SWING_SCORE,
        COL_MOMENTUM_MIX,
        COL_1D_CHANGE_PCT,
        COL_3D_CHANGE_PCT,
        COL_5D_CHANGE_PCT,
        COL_VOL_STRENGTH,
        COL_DOLLAR_VOL_M,
    ]
    for col in numeric_cols:
        if col in temp.columns:
            temp[col] = pd.to_numeric(temp[col], errors='coerce')

    if COL_SHORT_SCORE in temp.columns:
        ranked = temp.sort_values(COL_SHORT_SCORE, ascending=False)
    elif COL_3D_CHANGE_PCT in temp.columns:
        ranked = temp.sort_values(COL_3D_CHANGE_PCT, ascending=False)
    else:
        ranked = temp

    ranked = ranked.dropna(subset=['_ticker']).head(top_n).copy()
    if len(ranked) == 0:
        return pd.DataFrame()

    scan_date = datetime.now().strftime("%Y-%m-%d")
    ranked.insert(0, 'rank', range(1, len(ranked) + 1))
    ranked.insert(0, 'source_file', source_name)
    ranked.insert(0, 'scan_date', scan_date)

    output_cols = [
        'scan_date',
        'source_file',
        'rank',
        '_ticker',
        COL_SHORT_SCORE,
        COL_SWING_SCORE,
        COL_MOMENTUM_MIX,
        COL_CONTINUATION_GRADE,
        COL_PROB_NEXT_DAY,
        COL_PROB_DAY2,
        COL_REVERSAL_FLAGS,
        COL_DECISION_TAG_HINT,
        COL_1D_CHANGE_PCT,
        COL_3D_CHANGE_PCT,
        COL_5D_CHANGE_PCT,
        COL_VOL_STRENGTH,
        COL_DOLLAR_VOL_M,
        COL_AI_QUERY_HINT,
    ]
    available = [c for c in output_cols if c in ranked.columns]
    snapshot = ranked[available].copy()
    snapshot = snapshot.rename(columns={'_ticker': 'ticker'})
    return snapshot


def save_backtest_pick_log(snapshot_df):
    if snapshot_df is None or len(snapshot_df) == 0:
        return None, None

    BACKTEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_PICKS_DIR.mkdir(parents=True, exist_ok=True)

    daily_file = DAILY_PICKS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_xq_top_picks.csv"
    if daily_file.exists():
        existing_daily = pd.read_csv(daily_file)
        combined_daily = pd.concat([existing_daily, snapshot_df], ignore_index=True)
        combined_daily = combined_daily.drop_duplicates(subset=['scan_date', 'source_file', 'ticker'], keep='last')
        combined_daily.to_csv(daily_file, index=False, encoding='utf-8-sig')
    else:
        snapshot_df.to_csv(daily_file, index=False, encoding='utf-8-sig')

    if PICK_LOG_FILE.exists():
        existing_log = pd.read_csv(PICK_LOG_FILE)
        merged_log = pd.concat([existing_log, snapshot_df], ignore_index=True)
        merged_log = merged_log.drop_duplicates(subset=['scan_date', 'source_file', 'ticker'], keep='last')
        merged_log.to_csv(PICK_LOG_FILE, index=False, encoding='utf-8-sig')
    else:
        snapshot_df.to_csv(PICK_LOG_FILE, index=False, encoding='utf-8-sig')

    return PICK_LOG_FILE, daily_file


def _resolve_ai_ready_base_dir() -> Path:
    raw = Path(AI_READY_OUTPUT_DIR)
    return raw if raw.is_absolute() else (PROJECT_ROOT / raw)


def export_ai_ready_xq_file(source_df: pd.DataFrame) -> Path | None:
    if not AI_READY_OUTPUT_ENABLED:
        return None
    if source_df is None or len(source_df) == 0:
        return None

    base_dir = _resolve_ai_ready_base_dir()
    latest_dir = base_dir / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)

    target = latest_dir / AI_XQ_TARGET_FILE
    source_df.to_csv(target, index=False, encoding='utf-8-sig')
    return target


def write_ai_ready_xq_manifest(source_file: Path, exported_file: Path, row_count: int) -> Path | None:
    if not AI_READY_OUTPUT_ENABLED:
        return None

    latest_dir = exported_file.parent
    manifest_path = latest_dir / AI_XQ_MANIFEST_FILE
    source_mtime = datetime.fromtimestamp(source_file.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
    manifest = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'source_file': source_file.name,
        'source_path': str(source_file),
        'source_modified_at': source_mtime,
        'export_file': exported_file.name,
        'row_count': int(row_count),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest_path


def _candidate_csv_files() -> list[Path]:
    return sorted(
        [file for file in XQ_EXPORTS_DIR.glob('*.csv') if '_updated' not in file.stem],
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )


def _is_file_modified_today(file_path: Path, now_dt: datetime | None = None) -> bool:
    ref_dt = now_dt or datetime.now()
    file_dt = datetime.fromtimestamp(file_path.stat().st_mtime)
    return file_dt.date() == ref_dt.date()


def _describe_file_mtime(file_path: Path) -> str:
    return datetime.fromtimestamp(file_path.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')


def update_csv_with_history(file_path, ticker_column=None):
    """
    更新 CSV 檔案,新增歷史價格數據
    
    Args:
        file_path: CSV 檔案路徑
        ticker_column: 股票代碼欄位名稱
    """
    print(f"\n{'='*60}")
    print(f"📁 處理檔案: {file_path.name}")
    print(f"{'='*60}")
    
    # 讀取 CSV
    df = read_xq_csv(file_path)
    if df is None:
        return None
    
    # 偵測股票代碼欄位
    if ticker_column is None:
        # 尋找包含 .US 或股票代碼的欄位
        for col in df.columns:
            sample_values = df[col].astype(str).head(10)
            # 檢查是否包含 .US 後綴或符合股票代碼格式
            if sample_values.str.contains('.US', case=False).any():
                ticker_column = col
                print(f"🔍 偵測到股票代碼欄位: {ticker_column}")
                break
            # 檢查欄位名稱
            if any(keyword in str(col).lower() for keyword in ['代號', 'symbol', 'ticker', '代号', '股票']):
                ticker_column = col
                print(f"🔍 偵測到股票代碼欄位: {ticker_column}")
                break
        
        # 如果還是找不到,假設第一欄是代碼
        if ticker_column is None:
            ticker_column = df.columns[0]
            print(f"⚠️ 無法偵測代碼欄位,使用第一欄: {ticker_column}")
    
    # 建立新欄位（1/3/5 日 + 短炒評分）
    for col in [
        COL_1D_CHANGE_PCT,
        COL_3D_CHANGE_PCT,
        COL_5D_CHANGE_PCT,
        COL_5D_AVG_PRICE,
        COL_5D_HIGH,
        COL_5D_LOW,
        COL_YDAY_VOLUME,
        COL_5D_AVG_VOLUME,
        COL_VOL_STRENGTH,
        COL_DOLLAR_VOL_M,
        COL_SHORT_SCORE,
        COL_SWING_SCORE,
        COL_MOMENTUM_MIX,
        COL_CONTINUATION_GRADE,
        COL_PROB_NEXT_DAY,
        COL_PROB_DAY2,
        COL_REVERSAL_FLAGS,
        COL_DECISION_TAG_HINT,
        COL_AI_QUERY_HINT,
    ]:
        df[col] = None
    
    # 逐筆處理股票
    total = len(df)
    success_count = 0
    
    for idx, row in df.iterrows():
        ticker = extract_ticker(row, ticker_column)
        print(f"\n[{idx+1}/{total}] {ticker} ...", end=" ")
        
        # 抓取歷史數據 (1/3/5 日所需)
        hist = fetch_history(ticker, days=MAX_LOOKBACK)
        
        if hist is not None and not hist.empty:
            metrics = calculate_metrics(hist)
            for key, value in metrics.items():
                df.at[idx, key] = value
            df.at[idx, COL_AI_QUERY_HINT] = build_ai_query_hint(ticker)
            
            print(f"✅ 完成")
            success_count += 1
        else:
            print(f"❌ 跳過")
        
        # 避免過度頻繁請求
        time.sleep(0.5)
    
    try:
        # 輸出前將欄位改為英文（僅供後續寫入 ai_ready）
        df_out, rename_map = rename_columns_to_english(df)
        if ticker_column in rename_map:
            ticker_column = rename_map[ticker_column]

        print(f"\n{'='*60}")
        print(f"✅ 已處理: {file_path.name}")
        print(f"📊 成功更新: {success_count}/{total} 支股票")
        print(f"🧭 輸出目標: repo_outputs/ai_ready/latest/{AI_XQ_TARGET_FILE}")
        print(f"{'='*60}\n")
        return df_out, ticker_column, file_path.name
    except Exception as e:
        print(f"❌ 儲存失敗: {e}")
        return None


def main():
    """主程式"""
    parser = argparse.ArgumentParser(description='更新 XQ 選股清單的歷史價格')
    parser.add_argument(
        '--file',
        type=str,
        help='指定單一 CSV 檔案名稱 (例如: 妖股來吧起來0206.csv)'
    )
    parser.add_argument(
        '--ticker-column',
        type=str,
        help='股票代碼欄位名稱'
    )
    parser.add_argument(
        '--dir',
        type=str,
        help='指定 XQ_exports 資料夾路徑 (預設自動尋找)'
    )
    parser.add_argument(
        '--all-files',
        action='store_true',
        help='處理 XQ_exports 內所有 CSV（預設只處理最新一份）'
    )
    parser.add_argument(
        '--allow-stale',
        action='store_true',
        help='允許使用非今日匯出的 XQ CSV（預設禁止，避免舊資料覆蓋今日 bundle）'
    )
    
    args = parser.parse_args()
    
    # 如果用戶指定了資料夾,使用指定的路徑
    global XQ_EXPORTS_DIR
    if args.dir:
        XQ_EXPORTS_DIR = Path(args.dir)
        print(f"📁 使用指定路徑: {XQ_EXPORTS_DIR}")
    else:
        print(f"📁 自動偵測到路徑: {XQ_EXPORTS_DIR}")
    
    print(f"""
╔═══════════════════════════════════════════════════════════╗
║   XQ 選股清單 - Yahoo Finance 歷史價格更新工具           ║
╚═══════════════════════════════════════════════════════════╝
    """)
    
    # 檢查資料夾是否存在
    if not XQ_EXPORTS_DIR.exists():
        print(f"❌ 找不到資料夾: {XQ_EXPORTS_DIR}")
        print(f"\n請確認:")
        print(f"  1. XQ_exports 資料夾是否存在")
        print(f"  2. 或使用 --dir 參數指定路徑:")
        print(f"     python {Path(__file__).name} --dir C:\\路徑\\XQ_exports")
        print(f"\n自動搜尋順序:")
        print(f"  1. 腳本所在目錄: {Path(__file__).parent / 'XQ_exports'}")
        print(f"  2. 腳本上層目錄: {Path(__file__).parent.parent / 'XQ_exports'}")
        print(f"  3. 當前工作目錄: {Path.cwd() / 'XQ_exports'}")
        return

    # 處理單一檔案或所有 CSV
    results = []

    if args.file:
        file_path = XQ_EXPORTS_DIR / args.file
        if file_path.exists():
            result = update_csv_with_history(file_path, args.ticker_column)
            if result:
                results.append(result)
        else:
            print(f"❌ 找不到檔案: {file_path}")
            return 2
    else:
        csv_files = _candidate_csv_files()

        if not csv_files:
            print(f"❌ 沒有找到任何 CSV 檔案")
            return 2

        if args.all_files:
            selected_files = csv_files
            print(f"📂 找到 {len(selected_files)} 個 CSV 檔案，將全部處理\n")
        else:
            latest_file = csv_files[0]
            if not args.allow_stale and not _is_file_modified_today(latest_file):
                print(f"❌ 偵測到最新 XQ 匯出不是今天的檔案：{latest_file.name}")
                print(f"   最後修改時間：{_describe_file_mtime(latest_file)}")
                print("   為避免舊 XQ 資料被重新包進今日 ai_ready_bundle，已中止本次流程。")
                print("   若你確定要沿用舊檔，請手動執行：python .\\scripts\\update_xq_with_history.py --allow-stale")
                return 3
            selected_files = [latest_file]
            print(f"📂 自動模式只處理最新 XQ 匯出：{latest_file.name} | mtime={_describe_file_mtime(latest_file)}\n")

        for csv_file in selected_files:
            result = update_csv_with_history(csv_file, args.ticker_column)
            if result:
                results.append(result)

    if results:
        pick_snapshots = []
        best_ai_xq_df = None
        best_source_name = None
        best_source_path = None
        best_score_count = -1

        print("\n===== 每檔短炒分數 Top 5 =====")
        for df, ticker_column, source_name in results:
            print(f"\n[{source_name}]")
            print_top_movers(df, ticker_column)

            score_count = 0
            if COL_SHORT_SCORE in df.columns:
                score_count = pd.to_numeric(df[COL_SHORT_SCORE], errors='coerce').notna().sum()
            if score_count > best_score_count:
                best_score_count = score_count
                best_ai_xq_df = df
                best_source_name = source_name
                best_source_path = XQ_EXPORTS_DIR / source_name

            snapshot = build_top_picks_snapshot(df, ticker_column, source_name)
            if len(snapshot) > 0:
                pick_snapshots.append(snapshot)

        if pick_snapshots:
            combined_snapshot = pd.concat(pick_snapshots, ignore_index=True)
            log_file, daily_file = save_backtest_pick_log(combined_snapshot)
            print("\n===== 回測用 XQ 入選名單已記錄 =====")
            print(f"主檔: {log_file}")
            print(f"每日檔: {daily_file}")

        ai_target = export_ai_ready_xq_file(best_ai_xq_df)
        if ai_target is not None:
            manifest_path = None
            if best_source_path is not None and best_source_path.exists():
                manifest_path = write_ai_ready_xq_manifest(best_source_path, ai_target, len(best_ai_xq_df))
            print("\n===== AI 五檔快捷輸出已更新 =====")
            print(f"XQ 檔案: {ai_target}")
            if best_source_name:
                print(f"來源 XQ: {best_source_name}")
            if manifest_path is not None:
                print(f"XQ manifest: {manifest_path}")
    
    print(f"\n✅ 全部完成!\n")
    return 0


if __name__ == "__main__":
    with keep_system_awake():
        raise SystemExit(main())
