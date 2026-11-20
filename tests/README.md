# 测试与验证脚本目录说明

为保持日常回归与专项探针清晰分离，`tests/` 按职责划分为 4 个层次：

| 位置 | 说明 | 运行方式 |
| --- | --- | --- |
| `tests/test_*.py` | 离线自动回归套件（由 `conftest.py` 阻断 Python socket 与 libcurl，隔离到临时目录） | `.\.venv\Scripts\python.exe -m pytest -q` |
| `tests/check_startup.py` | 本地 HTTP 启停与端口回退验证（不连上游） | `.\.venv\Scripts\python.exe tests/check_startup.py` |
| `tests/checks/` | 专项验证、内存/性能探针与 Windows 便携包验收脚本（`check_*.py`） | 按需单独执行，详见 [docs/WINDOWS_RELEASE.md](../docs/WINDOWS_RELEASE.md) |
| `tests/js/` | Node / Playwright JS 行为测试脚本（`atelier_shell_state.cjs`、`home_sse.cjs` 等） | 由对应 pytest 自动调用或 `node tests/js/<file>` |
| `tests/legacy/` | 历史联调/压测脚本（导入时可能联网或写入数据，已在 `conftest.py` 排除采集） | 仅作历史参考，勿在个人实例运行 |
