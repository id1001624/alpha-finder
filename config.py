# Alpha Finder 配置文件
import os

# ============ 市場和指數配置 ============
# 🎯 根據投資策略選擇要監控的市場/指數
# 💡 建議: 使用單個指數達到最佳效果，避免過度限制搜尋結果
# ⚠️  同時使用多個市場和指數會導致篩選條件過嚴格，返回 0 結果

SELECTED_EXCHANGES = []  # 留空表示使用所有市場

SELECTED_INDICES = [
    'idx_sp500',           # ✅ S&P 500 - 美股大盤 500 強
    'idx_ndx',             # ✅ Nasdaq 100 - 科技股為主
    'idx_russell2000',     # ✅ Russell 2000 - 小型股
    # 'idx_philly_smh',    # Philadelphia Semiconductor - 半導體
    # 'idx_dji',           # Dow Jones - 道瓊指數
]

# ============ Finviz 設定 ============
# 最多爬取幾頁（每頁 20 筆）
# 提高至 5 頁以覆蓋龍頭股（NVDA、TSLA 等大盤股容易排名靠後）
MAX_PAGES = 5

# ============ Yahoo Finance 設定 ============
# 最多處理幾檔股票（避免 API 限制）
# 提高至 120 以涵蓋全市場強勢股
MAX_STOCKS_TO_PROCESS = 120

# API 請求延遲（秒）
API_DELAY = 0.5

# ============ 篩選條件 ============
# 起飛清單
LAUNCH_MIN_GAIN = 3.0          # 最低漲幅 %
LAUNCH_MIN_REL_VOL = 1.8       # 最低相對量能倍數
LAUNCH_MIN_PRICE = 5.0         # 最低股價
LAUNCH_MIN_MCAP = 100_000_000  # 最低市值（美元）

# 財報預熱
EARNINGS_DAYS_AHEAD = 7              # 未來幾天內的財報
EARNINGS_MIN_MCAP = 1_000_000_000    # 最低市值（美元）
EARNINGS_MIN_VOLUME = 500_000        # 最低平均成交量（Finnhub 補強用）
MAX_EARNINGS_MERGE = 80              # Finnhub 財報補抓最大數量（防止爆量）
EARNINGS_RESERVED_SLOTS = 40         # 財報股保留名額（不受信號排名截斷）

# 財報拆分（新）
EARNINGS_LOOKAHEAD_DAYS = int(os.getenv("EARNINGS_LOOKAHEAD_DAYS", "14"))
EARNINGS_LOOKBACK_DAYS = int(os.getenv("EARNINGS_LOOKBACK_DAYS", "5"))
EARNINGS_SNIPER_DAYS = int(os.getenv("EARNINGS_SNIPER_DAYS", "3"))

# 財報市值分級門檻（新）
EARNINGS_TIER1_MCAP_MIN = int(os.getenv("EARNINGS_TIER1_MCAP_MIN", "2000000000"))
EARNINGS_TIER2_MCAP_MIN = int(os.getenv("EARNINGS_TIER2_MCAP_MIN", "300000000"))

# 預測情報
ANALYST_MIN_UPSIDE = 30.0      # 最低上漲空間 %
ANALYST_MIN_COUNT = 3          # 最低分析師數量

# ============ 評級設定 ============
# A 級條件：量能倍數門檻
RATING_A_REL_VOL = 2.5
# A 級條件：上漲空間門檻
RATING_A_UPSIDE = 50.0

# ============ 輸出設定 ============
# 每個清單輸出幾檔股票
TOP_N_STOCKS = 3

# ============ 大盤環境濾網（MVP，可關閉） ============
# 啟用後：若 SPY 跌破 MA20，將自動提高部分選股門檻
MARKET_FILTER_ENABLED = os.getenv("MARKET_FILTER_ENABLED", "false").lower() == "true"
MARKET_FILTER_SYMBOL = os.getenv("MARKET_FILTER_SYMBOL", "SPY").upper()
MARKET_FILTER_MA_WINDOW = int(os.getenv("MARKET_FILTER_MA_WINDOW", "20"))
MARKET_FILTER_LOOKBACK_DAYS = int(os.getenv("MARKET_FILTER_LOOKBACK_DAYS", "60"))

# Bear 模式（SPY < MA20）門檻
MARKET_FILTER_BEAR_LAUNCH_MIN_GAIN = float(os.getenv("MARKET_FILTER_BEAR_LAUNCH_MIN_GAIN", "4.5"))
MARKET_FILTER_BEAR_LAUNCH_MIN_REL_VOL = float(os.getenv("MARKET_FILTER_BEAR_LAUNCH_MIN_REL_VOL", "2.5"))
MARKET_FILTER_BEAR_EARNINGS_MIN_MCAP = int(os.getenv("MARKET_FILTER_BEAR_EARNINGS_MIN_MCAP", "2000000000"))
MARKET_FILTER_BEAR_ANALYST_MIN_UPSIDE = float(os.getenv("MARKET_FILTER_BEAR_ANALYST_MIN_UPSIDE", "40.0"))

# ============ Google Sheets 設定（選用）============
GSHEET_ENABLED = True
GSHEET_NAME = "Alpha_Sniper_Daily_Report"
GSHEET_CREDENTIALS_FILE = "credentials.json"
GSHEET_UPLOAD_DAILY_REPORT = True
GSHEET_UPLOAD_FULL_DATA = False

# ============ 本地每日輸出（推薦） ============
# 每日刷新的資料優先存 repo，本地給 AI 讀取更穩定
LOCAL_OUTPUT_ENABLED = True
LOCAL_OUTPUT_DIR = "repo_outputs/daily_refresh"
LOCAL_OUTPUT_KEEP_DAYS = 14

# ============ API金鑰設定 ============
# Finnhub 免費 API (60 call/min) - 財報日期 + 分析師目標價
# 註冊: https://finnhub.io/register
# 使用環境變數：FINNHUB_API_KEY
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

# ============ 彩票股設定 (Track F) ============
LOTTERY_MIN_GAIN = 10.0        # 單日漲幅 >10%
LOTTERY_MIN_REL_VOL = 3.0      # 量能倍數 >3
LOTTERY_MAX_MCAP = 50_000_000_000  # 市值 <$50B

# ============ A/B/C 評級設定 ============
# A 級條件
GRADE_A_UPSIDE = 30.0          # 上漲空間 >30%
GRADE_A_ANALYSTS = 15          # 分析師數 >15
GRADE_A_SECTORS = ['Technology', 'Healthcare', 'Utilities', 'Industrials']

# B 級條件
GRADE_B_UPSIDE = 15.0          # 上漲空間 >15%
GRADE_B_7D_GAIN = 8.0          # 7日漲幅 >8%

# ============ 通知設定（選用）============
NOTIFICATION_ENABLED = False
NOTIFICATION_EMAIL = "your-email@gmail.com"
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

# ============ TradingView Webhook 訊號設定 ============
USE_TRADINGVIEW_SIGNALS = True
TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "")
SIGNAL_STORE_PATH = os.getenv("SIGNAL_STORE_PATH", "signals.db")
SIGNAL_MAX_AGE_MINUTES = int(os.getenv("SIGNAL_MAX_AGE_MINUTES", "240"))
SIGNAL_REQUIRE_SAME_DAY = os.getenv("SIGNAL_REQUIRE_SAME_DAY", "true").lower() == "true"
ALLOW_PLAIN_TEXT_WEBHOOK = os.getenv("ALLOW_PLAIN_TEXT_WEBHOOK", "false").lower() == "true"
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8000"))

# ============ Demo / TV 補圖清單設定 ============
DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() == "true"
TV_LIST_LIMIT = int(os.getenv("TV_LIST_LIMIT", "6"))