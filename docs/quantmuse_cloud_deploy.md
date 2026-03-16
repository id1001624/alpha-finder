# QuantMuse 雲端部署清單（Discord Bot 主機）

本清單給 Oracle Cloud Ubuntu 主機使用，目標是讓 Alpha Finder bot 啟用 native QuantMuse 路徑，並保留 fallback（模組失效時不會中斷主流程）。

## 0. 前提

- Bot 主機使用 systemd 服務：alpha-finder-discord-bot.service
- 主程式目錄：/opt/alpha-finder
- 環境檔：/etc/alpha-finder/discord-bot.env
- 服務使用者：alphafinder

## 1. SSH 進主機

    ssh -i ~/.ssh/alpha-finder-bot.key ubuntu@你的主機IP

## 2. 建立 QuantMuse 路徑（建議用原始碼模式）

建議放在 /opt/quantmuse，讓 data_service 目錄可被匯入。

    sudo mkdir -p /opt/quantmuse
    sudo chown -R alphafinder:alphafinder /opt/quantmuse
    sudo -u alphafinder git clone https://github.com/0xemmkty/quantmuse.git /opt/quantmuse

若已存在可改成更新：

    sudo -u alphafinder git -C /opt/quantmuse pull --ff-only

## 3. 安裝 QuantMuse 依賴到 bot venv

    sudo -u alphafinder /opt/alpha-finder/.venv/bin/pip install -U pip
    sudo -u alphafinder /opt/alpha-finder/.venv/bin/pip install -r /opt/quantmuse/requirements.txt

如果 QuantMuse 沒有 requirements.txt，至少補齊你策略會用到的套件（例如 pandas、numpy、transformers、openai、langchain）。

## 4. 設定 bot env

編輯 /etc/alpha-finder/discord-bot.env，新增或確認以下值：

    QUANTMUSE_ENABLED=true
    QUANTMUSE_PATH=/opt/quantmuse
    QUANTMUSE_LLM_PROVIDER=local
    QUANTMUSE_LLM_MODEL=sshleifer/tiny-gpt2
    QUANTMUSE_VECTOR_DB_PATH=repo_outputs/backtest/trade_memory_vector.db

說明：

- `QUANTMUSE_LLM_PROVIDER=local` 時，請務必指定 `QUANTMUSE_LLM_MODEL`，避免預設模型在雲端主機下載過大或初始化失敗。
- 首次啟動會下載模型權重，可能花 1~3 分鐘屬正常。

若你要走 OpenAI provider：

    QUANTMUSE_LLM_PROVIDER=openai
    OPENAI_API_KEY=你的金鑰

## 5. systemd reload + restart

    sudo systemctl daemon-reload
    sudo systemctl restart alpha-finder-discord-bot.service
    sudo systemctl --no-pager --full status alpha-finder-discord-bot.service

## 6. 線上驗證（QuantMuse 能否 native 匯入）

先在同一台主機、同一個 env 下驗證：

    cd /opt/alpha-finder
    sudo -u alphafinder env $(sudo cat /etc/alpha-finder/discord-bot.env | xargs) /opt/alpha-finder/.venv/bin/python scripts/verify_quantmuse_runtime.py

期望結果：

- quantmuse_capabilities.available = true
- reason = ok
- module_name 會是 data_service.ai 或 quantmuse.data_service.ai 其中之一

若 available = false：

- 先檢查 QUANTMUSE_PATH 是否正確
- 再檢查 /opt/alpha-finder/.venv 內依賴是否已安裝

## 7. 服務日誌驗證

    sudo tail -n 120 /var/log/alpha-finder/discord-bot.log

你應看到 bot 正常連線，且沒有 QuantMuse 匯入錯誤導致服務中斷。

## 8. 回滾方案（保留不中斷）

若 QuantMuse 當下有問題，先回到 fallback：

    sudo sed -i 's/^QUANTMUSE_ENABLED=.*/QUANTMUSE_ENABLED=false/' /etc/alpha-finder/discord-bot.env
    sudo systemctl restart alpha-finder-discord-bot.service

此時主流程仍可運作，只是改用 heuristic/rule-based 路徑。

## 9. 與本機 redeploy 腳本配合

若你從 Windows 用 redeploy_discord_bot.ps1 -SyncEnv 同步環境，請記得把 QuantMuse 相關變數也加到本機環境，再同步上去；否則可能被覆蓋掉。
