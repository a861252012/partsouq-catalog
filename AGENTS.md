# AGENTS.md

## Coding Style（硬性規定）

1. **禁止意義不明的別名**
   - SQL 不得把 table 改成語意模糊的短別名
     （例：`nhtsa_sync_runs AS runs`、`scheduled_job_runs AS jobs` 這類
     讀者需要回查才能理解的縮寫）。若 table 名過長，別名必須保留完整語意
     （例：`nhtsa_sync_runs AS sync_run`），或直接使用完整表名。
   - 變數命名同上：禁止 `r`、`tmp2`、`d` 這類需回推才懂的名字。
   - class 命名必須直接表達職責，禁止抽象代稱。
2. 註解與文件以繁體中文書寫；識別字（變數/函式/class）用英文。
3. 每個里程碑更新 `docs/progress-log-*.md`，commit 以 `[skip ci]` 標記
   本機已跑過全套關卡者。

## 品質關卡（提交前必跑）

```zsh
uv run --locked ruff check && uv run --locked ruff format --check
uv run --locked mypy --strict <更動的檔案>
PARTSOUQ_DB_NAME=partsouq_catalog_test NHTSA_TEST_MYSQL=1 UNIFIED_TEST_MYSQL=1 \
  uv run --locked pytest -W error -q --strict-config --strict-markers
```

注意：新增 migration 或 env-gated 測試後，全套 pytest 的 skip 總數
（目前 268）與 `tests/e2e/test_catalog_migration_runner.py` 內各
ledger 刪除清單必須同步檢查。

## 營運備忘

- 全量 crawl 以 daemon 模式啟動（證據系統強制 trigger_mode='daemon'）；
  LaunchAgent 與手動 daemon 不可並存。
- 完整歷程見 `docs/progress-log-2026-08-23.md`。
