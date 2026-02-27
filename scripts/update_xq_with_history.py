"""
XQ 選股清單更新腳本
自動從 Yahoo Finance 抓取歷史價格,並更新到 XQ 匯出的 CSV 檔案

功能:
- 掃描 XQ_exports 資料夾下所有 CSV 檔案
- 抓取每支股票最近 7 日的歷史價格
- 新增欄位: avg_7d, high_7d, low_7d, chg_7d_pct
- 執行完畢後顯示各檔案的 7 日漲幅 Top 5
- 自動處理中文編碼問題

使用方式:
    python scripts/update_xq_with_history.py
    python scripts/update_xq_with_history.py --file 妖股來吧起來0206.csv
"""

import os
import sys
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import datetime, timedelta
import argparse
import time
import re

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
XQ_EXPORTS_DIR = find_xq_exports_dir()

DAYS_7 = 7
COL_7D_AVG = "avg_7d"
COL_7D_HIGH = "high_7d"
COL_7D_LOW = "low_7d"
COL_7D_CHANGE_PCT = "chg_7d_pct"

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


def fetch_history(ticker, days=10):
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
        start_date = end_date - timedelta(days=days+10)
        
        # 下載數據
        stock = yf.Ticker(ticker)
        hist = stock.history(start=start_date, end=end_date)
        
        if hist.empty:
            print(f"⚠️ {ticker}: 無歷史數據")
            return None
        
        # 取最近 N 個交易日
        hist = hist.tail(days)
        
        return hist
    
    except Exception as e:
        print(f"❌ {ticker}: 抓取失敗 - {e}")
        return None


def calculate_metrics(hist, days):
    """
    計算歷史數據的統計指標
    
    Args:
        hist: 歷史價格 DataFrame
        days: 天數 (用於標註)
    
    Returns:
        dict: 包含各種統計指標
    """
    if hist is None or hist.empty:
        return {
            COL_7D_AVG: None,
            COL_7D_HIGH: None,
            COL_7D_LOW: None,
            COL_7D_CHANGE_PCT: None,
        }
    
    # 只取最近 N 天
    recent = hist.tail(days)
    
    return {
        COL_7D_AVG: round(recent['Close'].mean(), 2),
        COL_7D_HIGH: round(recent['High'].max(), 2),
        COL_7D_LOW: round(recent['Low'].min(), 2),
        COL_7D_CHANGE_PCT: round(
            ((recent['Close'].iloc[-1] / recent['Close'].iloc[0] - 1) * 100), 2
        ) if len(recent) > 0 else None,
    }


def print_top_movers(df, ticker_column):
    """
    顯示指定天數的漲幅 Top 5

    Args:
        df: 更新後的 DataFrame
        ticker_column: 股票代碼欄位名稱
        days: 天數 (預設 7)
    """
    change_col = COL_7D_CHANGE_PCT
    if change_col not in df.columns:
        print(f"⚠️ 找不到欄位: {change_col}")
        return

    temp = df.copy()
    if ticker_column in temp.columns:
        temp['_ticker'] = temp[ticker_column].astype(str).str.strip()
    else:
        temp['_ticker'] = temp.apply(lambda row: extract_ticker(row), axis=1)

    temp[change_col] = pd.to_numeric(temp[change_col], errors='coerce')
    top5 = temp.dropna(subset=[change_col]).sort_values(change_col, ascending=False).head(5)

    if top5.empty:
        print("⚠️ 無可用的 7 日漲幅數據")
        return

    print("\n7-day change Top 5:")
    for i, row in top5.iterrows():
        print(f"{row['_ticker']}: {row[change_col]}%")


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
    
    days_list = [DAYS_7]

    # 為 7 日建立新欄位
    df[COL_7D_AVG] = None
    df[COL_7D_HIGH] = None
    df[COL_7D_LOW] = None
    df[COL_7D_CHANGE_PCT] = None
    
    # 逐筆處理股票
    total = len(df)
    success_count = 0
    
    for idx, row in df.iterrows():
        ticker = extract_ticker(row, ticker_column)
        print(f"\n[{idx+1}/{total}] {ticker} ...", end=" ")
        
        # 抓取歷史數據 (固定 7 日)
        hist = fetch_history(ticker, days=days_list[0])
        
        if hist is not None and not hist.empty:
            # 計算 7 日指標
            metrics = calculate_metrics(hist, days_list[0])
            for key, value in metrics.items():
                df.at[idx, key] = value
            
            print(f"✅ 完成")
            success_count += 1
        else:
            print(f"❌ 跳過")
        
        # 避免過度頻繁請求
        time.sleep(0.5)
    
    # 儲存更新後的 CSV
    output_path = file_path.parent / f"{file_path.stem}_updated{file_path.suffix}"
    encoding = "utf-8"
    
    try:
        # 輸出前將欄位改為英文
        df_out, rename_map = rename_columns_to_english(df)
        if ticker_column in rename_map:
            ticker_column = rename_map[ticker_column]

        df_out.to_csv(output_path, index=False, encoding=encoding)
        print(f"\n{'='*60}")
        print(f"✅ 已儲存: {output_path.name}")
        print(f"📊 成功更新: {success_count}/{total} 支股票")
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
    else:
        # 處理所有 CSV 檔案
        csv_files = list(XQ_EXPORTS_DIR.glob("*.csv"))
        
        if not csv_files:
            print(f"❌ 沒有找到任何 CSV 檔案")
            return
        
        print(f"📂 找到 {len(csv_files)} 個 CSV 檔案\n")
        
        for csv_file in csv_files:
            # 跳過已更新的檔案
            if '_updated' in csv_file.stem:
                print(f"⏭️ 跳過: {csv_file.name} (已更新)")
                continue

            result = update_csv_with_history(csv_file, args.ticker_column)
            if result:
                results.append(result)

    if results:
        print("\n===== 每檔 7日漲幅 Top 5 =====")
        for df, ticker_column, source_name in results:
            print(f"\n[{source_name}]")
            print_top_movers(df, ticker_column)
    
    print(f"\n✅ 全部完成!\n")


if __name__ == "__main__":
    main()
