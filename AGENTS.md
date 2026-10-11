# AGENTS.md

## 政策與溝通

- 本檔是本 repository 唯一代理開發政策；可執行細節以 scripts 與設定為準。
- 最終回覆使用繁體中文；程式碼、命令與 commit message 可使用英文。
- 不得為了承載回覆新增 Markdown 文件；README 與文件只記錄長期使用契約。
- 開始實作須說明品質優先，不以最小修改或向後相容限制設計。
  完成時如實說明具體取捨、介面變化及是否恰好相容。
- 依 SOLID、KISS、YAGNI、內聚性與低耦合決定抽象邊界。必要局部重構可納入；
  實質擴大任務、改變未涵蓋公開行為或資料遷移須先取得使用者同意。
- 直接涉及的 legacy compatibility path 應移除，不保留 shim；不清理無關程式。

## 工具責任

- 本專案擁有跨 H2H repository 的開發工具、可安裝 CLI 與可重建工具快照。
  不擁有 Core schema、runtime facade、adapter 行為或各 consumer 的業務 gate。
- 共用 Python 實作的唯一手動維護來源是 `src/h2h_dev_tools/`。
  Consumer 的 generated snapshot 必須由 `h2h-dev-tools sync` 產生，禁止手改。
- Consumer hooks 使用納入自身 Git tree 的 snapshot，離線驗證完整 payload 與
  manifest；不得以任意 sibling checkout 或已安裝的最新版替換 candidate 工具。
- Snapshot 更新是明確動作，不可在 hooks、一般 pytest 或 merge gate 中下載、
  安裝、同步或自動修正檔案。產物來源版本及 payload hash 必須可驗證。
- 工具不得依賴 `h2hdb`、consumer runtime、Pillow 或 FastAPI。
  Consumer 只能在開發流程使用本工具；不得加入 consumer runtime dependencies
  或由 shipped consumer `src/` import 本工具。
- 不因外觀相似統一專案特有的 timeout、formal、schema、distribution 或 DB checks。
  各 repository 的政策與必要驗收仍由該 repository 擁有。

## Git 與整合

- 唯讀分析不建立 branch。修改 tracked files 前使用
  `scripts/detect-primary-branch.sh` 判定 primary 並建立專用 task branch。
  本 repository 預期 primary 為 `master`；有歧義時設定 local
  `workflow.primaryBranch`，不得猜測或連網查詢。
- 不 stash、reset、clean、覆寫或混入既有使用者修改；dirty checkout 從 committed
  primary 建立獨立 worktree。Task 與 primary 只需 common ancestor。
- 所有非 merge commit 使用 Conventional Commits；breaking change 使用 `!`
  或 `BREAKING CHANGE:`。不得使用 `--no-verify`。
- 任務完成執行 `scripts/git-flow-merge.sh`，以 `--no-ff` 整合；實際 staged
  merge candidate 由 pre-merge hook 檢查版本、文件或完整 gate。
  失敗須 abort merge 並保留 task branch。成功才安全移除 task worktree 及
  `git branch -d`。合併後 follow-up 使用新 task branch。
- Primary 禁止 rebase。Hooks 安裝設定 `pull.rebase=false`、primary rebase=false
  與 `pull.ff=only`。No-op cleanup 只在 task tip 已包含於 primary 時進行。
- 本機 task branch、commit、merge 及安全清理已獲授權。
  Fetch、pull、push、remote branch、tag、publish、deploy 及 force 操作
  必須逐次明確授權；既有 PyPI 設定不等於允許發布。

## 版本與依賴

- `pyproject.toml` 的 `[project].version` 是唯一版本來源，固定 `X.Y.Z`。
  1.0 前 `Y` 是 compatibility lane；相容 release 遞增 `Z`，breaking 遞增 `Y`
  並歸零 `Z`。1.0 後使用 Semantic Versioning。
- 本專案 shipped 開發工具本身就是 release surface；`src/`、CLI、snapshot
  格式或構建 metadata 的改變須升版。只有本 repo 自用 tests、scripts、hooks、CI、
  文件或品質設定的修改不升版。未知路徑須 fail closed 並明確分類。
- 既有專案的一個 task 只升版一次，以獨立
  `chore(release): bump version to X.Y.Z` commit 提交。
  Breaking／feature 訊號只採計實際修改 release surface 的 commits。
- 空 repository 的初次 release 明確限定為 `0.1.0`，須從真正 empty baseline
  建立並驗證 candidate dependency audit；不是一般版本檢查的豁免。
- 每次 release 執行 `scripts/audit-dependencies.py --review-note "..."`，人工
  檢查 release notes、Python 相容性及現有 bounds 外最新版，將與版本及完整
  dependency manifest 綁定的 `.release/dependency-audit.json` 納入提交。
  Audit 產生器可連網；commit 與 merge 只能驗證既有 evidence。
- `requires-python` 使用 `>=3.14`。依賴原則使用 `>=` lower bound，合理 upper
  bound 或排除須有具體相容性理由；不得為求純 stdlib 重寫 PEP 440／508 parser。
- 單一乾淨 checkout 必須可重建；不得固定依賴 sibling repo。
  不提交或使用 `uv.lock`、`package-lock.json` 作為重建輸入。
- Python tools 來自 `.venv`；Markdown tool 來自本 repo `node_modules`。
  Node 安裝使用 `npm install --package-lock=false`。

## 品質與驗收

- `pyproject.toml` 是 Ruff 與 strict mypy 唯一設定來源。使用 Ruff formatter，
  不使用 Black、`mypy.ini` 或系統全域品質工具。
- `noqa` 與 `type: ignore` 必須指定規則／error code 並有理由。
  禁止為了通過檢查而全域放寬設定。停用 Ruff 規則須在設定旁說明。
- `scripts/format.sh` 明確修改檔案；`scripts/check-fast.sh` 只讀、離線且不寫
  Ruff／mypy cache。非文件 commit 執行 fast gate。
- `scripts/check_change_scope.py` 以 exact Git tree/index 差異選擇 profile。
  Documentation gate 從 candidate 匯出 Markdown，驗 whitespace、README reference
  及本機 Markdown lint。未知路徑、mode/type 變化與空差異不得降級為文件。
- `scripts/release-gate.py` 驗證實際 staged two-parent merge candidate，執行版本
  及適用 profile，檢查前後 candidate 一致；失敗不得產生 merge commit。
- Full gate 執行 fast、完整離線 pytest、fresh sdist-derived wheel build 及隔離
  installed wheel／CLI smoke。明確手動呼叫 fast/full 不因文件差異而降級。
- Wheel smoke 僅複製已安裝 `packaging` distribution 供離線 resolver，禁止借用
  editable source 或整個 development site。此結果不代表 registry-backed clean
  dependency resolution；需要該證據時另以明確 wheels／index 執行並分別回報。
- 新功能涵蓋正常、邊界、錯誤路徑；bug fix 加 regression test。Git 測試使用
  disposable local repositories；不連 production、live account 或外部 network。
  Flaky 視為失敗；skip/xfail 須理由，xfail 預設 strict。
- GitHub Actions 只保留 trusted publishing 或本機無法驗證的平台檢查，
  不複製另一套一般 gate。首次發布可明確 dispatch exact version，仍須 default
  branch guard；一般 push 必須是已存在版本的遞增才發布。

## 完成回報

說明品質優先是否遵守及具體取捨；判定不向後相容或品質優先後恰好相容，列明
介面／格式影響與移除的 compatibility path。若 breaking，判斷一次性轉換是否
可行、是否已提供、用法與限制；不可行時精確說明保留及人工重建範圍。
回報版本、impact、dependency audit、commits、檢查及未執行項目、primary 與 merge
commit、task branch/worktree 清理狀態，以及是否仍未 push、publish、deploy。
