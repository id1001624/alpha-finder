# yfinance SSL 錯誤一頁報告

更新時間：2026-03-18

## 1) 事件摘要

- 問題類型：`curl_cffi.requests.exceptions.SSLError`（curl error 77）
- 主要影響：`yfinance` 取即時/報價資訊時失敗，造成 Phase2 market enrichment 無法取得 yfinance 快照。
- 專案影響層級：中。
  - 在 `INTRADAY_DATA_PROVIDER=auto` 且 Finnhub 可用時，多數流程可 fallback，不致整體中斷。
  - 若直接走 yfinance 路徑，會出現資料缺口或空結果。

## 2) 發生腳本與呼叫點

### 已確認觸發（本機可重現）

- `scripts/build_ai_trading_dataset.py`
  - `_fetch_yfinance_market_snapshot()`（約第 397 行）
  - 於 `ticker_obj.get_info()`（約第 413 行）觸發 SSL 例外
  - 呼叫鏈：`_apply_phase2_market_enrichment()` -> `_fetch_one()` -> `_fetch_yfinance_market_snapshot()`

### 同類風險呼叫點（同 yfinance stack，理論上同風險）

- `ai_trading/intraday_execution_engine.py`
  - `_fetch_intraday_bars_from_yfinance()`（約第 655-660 行）
- `ai_trading/swing_core_engine.py`
  - `yf.Ticker(...).history(...)` fallback（約第 205 行）
- `ai_trading/strategy_context.py`
  - `yf.Ticker(...).history(...)`（約第 180 行）
- `main.py`
  - 多處 `yf.Ticker(...).history(...)` / `yf.Ticker(...)`
- `backtest_earnings.py`
  - `yf.Ticker(...)`

## 3) 完整錯誤（原始 traceback）

重現指令：

```powershell
c:/Users/w6359/OneDrive/文件/alpha-finder/.venv/Scripts/python.exe -c "import scripts.build_ai_trading_dataset as b; import json; print(json.dumps(b._fetch_yfinance_market_snapshot('AAPL'), ensure_ascii=False))"
```

錯誤全文：

```text
Traceback (most recent call last):
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\curl_cffi\requests\session.py", line 640, in request
    c.perform()
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\curl_cffi\curl.py", line 365, in perform
    self._check_error(ret, "perform")
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\curl_cffi\curl.py", line 187, in _check_error
    raise error
curl_cffi.curl.CurlError: Failed to perform, curl: (77) error setting certificate verify locations:  CAfile: C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\certifi\cacert.pem CApath: none. See https://curl.se/libcurl/c/libcurl-errors.html first for more details.

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "<string>", line 1, in <module>
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\scripts\build_ai_trading_dataset.py", line 413, in _fetch_yfinance_market_snapshot
    raw_info = ticker_obj.get_info()
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\yfinance\base.py", line 282, in get_info
    data = self._quote.info
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\yfinance\scrapers\quote.py", line 503, in info
    self._fetch_info()
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\yfinance\scrapers\quote.py", line 612, in _fetch_info
    result = self._fetch(modules=modules)
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\yfinance\scrapers\quote.py", line 588, in _fetch
    result = self._data.get_raw_json(_QUOTE_SUMMARY_URL_ + f"/{self._symbol}", params=params_dict)
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\yfinance\data.py", line 462, in get_raw_json
    response = self.get(url, params=params, timeout=timeout)
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\yfinance\utils.py", line 95, in wrapper
    result = func(*args, **kwargs)
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\yfinance\data.py", line 375, in get
    response = self._make_request(url, request_method = self._session.get, params=params, timeout=timeout)
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\yfinance\utils.py", line 95, in wrapper
    result = func(*args, **kwargs)
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\yfinance\data.py", line 409, in _make_request
    crumb, strategy = self._get_cookie_and_crumb()
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\yfinance\utils.py", line 95, in wrapper
    result = func(*args, **kwargs)
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\yfinance\data.py", line 365, in _get_cookie_and_crumb
    crumb = self._get_cookie_and_crumb_basic(timeout)
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\yfinance\utils.py", line 95, in wrapper
    result = func(*args, **kwargs)
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\yfinance\data.py", line 246, in _get_cookie_and_crumb_basic
    return self._get_crumb_basic(timeout)
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\yfinance\utils.py", line 95, in wrapper
    result = func(*args, **kwargs)
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\yfinance\data.py", line 229, in _get_crumb_basic
    crumb_response = self._session.get(**get_args)
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\curl_cffi\requests\session.py", line 661, in get
    return self.request(method="GET", url=url, **kwargs)
  File "C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\curl_cffi\requests\session.py", line 647, in request
    raise error(str(e), e.code, rsp) from e
curl_cffi.requests.exceptions.SSLError: Failed to perform, curl: (77) error setting certificate verify locations:  CAfile: C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\certifi\cacert.pem CApath: none. See https://curl.se/libcurl/c/libcurl-errors.html first for more details.
```

## 4) 環境資訊

- OS: Windows 11 10.0.26200
- Python: 3.12.10 (venv)
- OpenSSL: OpenSSL 3.0.16 11 Feb 2025
- yfinance: 1.2.0
- curl_cffi: 0.13.0
- certifi: 2026.01.04
- requests: 2.32.5
- pandas: 3.0.1
- certifi 憑證路徑（含 Unicode 目錄）:
  - `C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Lib\site-packages\certifi\cacert.pem`

## 5) 已嘗試修法與結果

1. 設定 `SSL_CERT_FILE` 與 `CURL_CA_BUNDLE` 為 venv 內 certifi 路徑（Unicode 路徑）
   - 結果：仍失敗（同 curl error 77）
2. 將憑證複製到 ASCII 路徑 `C:\alpha_tmp\cacert.pem`，並設定 `SSL_CERT_FILE` + `CURL_CA_BUNDLE` 指向該檔
   - 結果：成功取得 AAPL snapshot（`yfinance_premarket`）

## 6) 目前結論

- 根因高度疑似為：`curl_cffi` 在 Windows 下對 Unicode 憑證路徑處理不穩，導致 CA file 初始化失敗。
- 在現況下，bundle pipeline 應維持 `finnhub` 為優先資料源，yfinance 當 fallback。
- 若需強制 yfinance，可用 ASCII 憑證路徑作為臨時 workaround（環境變數注入）。
