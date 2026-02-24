---
applyTo: 'always'
---

# Git Commit Message Rules

自動化 Git commit 和 push 時必須遵循的規則。

---

## 📋 Commit Message 格式

### 基本結構

```
<type>(<scope>): <subject>

<body>

<footer>
```

---

## 🏷️ Type 前綴 (必須)

| Type | 中文說明 | 使用情況 |
|------|----------|----------|
| `feat` | 新功能 | 新增特性、方法、類別 |
| `fix` | 修復 | 修復 bug、錯誤 |
| `docs` | 文件 | 更新 README、註解、文件 |
| `style` | 風格 | 程式碼格式、空白、分號 |
| `refactor` | 重構 | 程式碼重構，不改功能 |
| `perf` | 性能 | 改進性能、效能優化 |
| `test` | 測試 | 增加或修改測試 |
| `chore` | 維護 | 依賴更新、工具配置 |
| `ci` | CI/CD | GitHub Actions 配置 |
| `revert` | 回滾 | 撤銷之前的 commit |

---

## 📝 Message 規則

### ✅ 必須規則

1. **Type 前綴必須存在** — 不允許無前綴的 commit
   ```
   ❌ 錯誤: 新增 Python 基礎課程
   ✅ 正確: feat: 新增 Python 基礎課程
   ```

2. **Subject (標題) 必須用繁體中文** — 簡潔、現在式
   ```
   ✅ feat: 新增自動抽籤機功能
   ✅ fix: 修復 DevTools Network 卡頓問題
   ✅ docs: 更新 PROJECT_CONTEXT.md
   ```

3. **Subject 字數限制** — 50 字以內
   ```
   ❌ 過長: feat: 新增功能，包括自動抽籤、隨機選擇、名單管理等多項功能
   ✅ 適當: feat: 新增自動抽籤與隨機選擇功能
   ```

4. **不使用句號** — Subject 末尾不加標點
   ```
   ❌ feat: 新增功能。
   ✅ feat: 新增功能
   ```

5. **Body (詳細說明) 用繁體中文** — 可選，但複雜改動必須寫
   ```
   feat: 新增 Bug Report 模板
   
   - 新增 Jira 標準格式
   - 支援截圖上傳
   - 自動填充重現步驟
   ```

---

## 📌 Scope (範圍) — 可選

```
feat(python): 新增變數教程
feat(devtools): 改進 Network tab 篩選
fix(obsidian): 修復連結遺漏問題
```

常用 scope:
- `python` — Python 課程相關
- `devtools` — Chrome DevTools 筆記
- `obsidian` — Obsidian 筆記結構
- `project` — 專案結構、配置
- `readme` — README 更新
- `ci` — CI/CD 配置

---

## 🚫 Footer (可選，特殊情況使用)

### Breaking Change
```
feat: 重構測試框架

BREAKING CHANGE: API 文件位置從 docs/api 改為 docs/reference
```

### 關聯 Issue
```
fix: 修復登入流程

Fixes #123
```

---

## ✅ AI 自動 Commit 檢查清單

執行 commit 前確認：
- [ ] 有 Type 前綴 (feat, fix, docs 等)
- [ ] Subject 用繁體中文
- [ ] Subject 50 字以內
- [ ] Subject 末尾無句號
- [ ] 改動檔案與 commit message 相符
- [ ] 不包含臨時檔案 (.tmp, __pycache__ 等)

---

## 💡 完整 Commit 示例

```bash
# 簡單改動
git commit -m "feat: 新增自動抽籤機"

# 帶 scope 和 body
git commit -m "feat(python): 新增自動抽籤機

- 支援 CSV 檔案匯入
- 隨機演算法採用 random.shuffle
- 提供命令列界面"

# 修復 bug
git commit -m "fix(devtools): 修復 Network 篩選失效"

# 文件更新
git commit -m "docs: 更新 PROJECT_CONTEXT.md 進度"
```

---

## 🔧 自動化時的特殊規則

**AI 批量操作時**：
- 若改動超過 3 個檔案，改用 `chore` 或 `refactor`
- 若涉及多個功能，分開多個 commit
- 移動/重構檔案優先用 `refactor`

```bash
# ❌ 不推薦: 一個 commit 混雜多個功能
git commit -m "feat: 新增功能、修復、更新文件"

# ✅ 推薦: 分開 commit
git commit -m "feat: 新增自動抽籤機"
git commit -m "fix: 修復 DevTools 篩選"
git commit -m "docs: 更新學習進度"
```
