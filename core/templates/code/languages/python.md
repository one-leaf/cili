## 语言约束（{{language}}）

- 版本：Python 3.11 及以上；用 `python -m venv .venv` 建虚拟环境。
- 依赖：清单用 `pyproject.toml` 或 `requirements.txt`；虚拟环境目录不入库。
- 测试：pytest，测试文件 `test_*.py`，放 `tests/`。
- 格式与检查：ruff（lint + format）；提交前跑 `ruff check .`。
- 风格：公共函数写类型注解；抛具体异常，不用裸 `except`。
- 文件读写显式 `encoding="utf-8"`。
