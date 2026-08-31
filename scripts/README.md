# Scripts

## update_version.py

自动更新 `web/static/footer.json` 中的版本号为当前日期（格式：vYYYYMMDD）。

### 使用方式

**手动运行：**
```bash
python scripts/update_version.py
```

**自动运行（推荐）：**
已配置 git pre-commit hook，每次提交时自动更新版本号并添加到暂存区。

### 原理

- 读取 `web/static/footer.json`
- 更新 `version` 字段为当前日期
- 写回文件
- pre-commit hook 会自动将修改加入提交

### 自定义

如需修改版本号格式或逻辑，编辑 `scripts/update_version.py` 中的 `update_version()` 函数。
