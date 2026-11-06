# 贡献指南

RepoFix 支持 Python 3.11 及以上版本。请创建虚拟环境、安装开发依赖组；修改 Docker 执行逻辑时，还需要构建默认运行镜像：

```powershell
uv sync --group dev
docker build -f docker/runner.Dockerfile -t repofix-runner:py312 .
```

提交 Pull Request 前请运行：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pytest -q -m "not docker"
uv run python examples/offline_evaluation.py
uv run python -m build
```

Docker 可用时，请运行 `uv run pytest -q -m docker`。测试普通文件夹输入时，必须使用本仓库之外的临时目录，否则 Git 会按预期把该文件夹识别为父 Worktree 的一部分。

请保持修改范围聚焦。新增工具需要提供 JSON Schema、可信执行边界的严格参数模型、策略行为、结构化结果，以及无效输入测试。修改恢复机制时必须包含故障时间线，并且不得静默重放状态不确定的副作用。

提交中不得包含 API 密钥、本地 `.env` 文件、私有基准仓库或 RepoFix 控制器生成的数据。真实模型评测报告必须注明模型、服务提供方、任务集、预算、日期，以及是否进行了重复尝试。
