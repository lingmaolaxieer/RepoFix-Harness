# 评测指南

`repofix eval manifest.json` 会从固定、干净的 Git 提交运行每项任务，并记录会话、轨迹、补丁、测试输出、轮次、工具调用、耗时和独立通过/失败结果。

清单格式：

```json
[
  {
    "id": "empty-list-001",
    "repository": "D:/benchmarks/empty-list",
    "tags": ["python", "edge-case"],
    "task": {
      "issue": "mean([]) should return None instead of raising",
      "acceptance_commands": ["pytest -q"],
      "protected_tests": "D:/benchmarks/hidden/empty-list"
    }
  }
]
```

比较不同配置时，应保持模型、任务、temperature 和预算一致。根据轨迹报告通过率、轮次、工具调用、实际耗时和 Token 用量。开发任务必须与留出测试集分离。私有任务集的结果不得描述为 SWE-bench 成绩。

## 离线 Harness 评测

无需 API 密钥即可运行确定性评测：

```powershell
uv run python examples/offline_evaluation.py
```

该命令会创建两个临时 Git 项目，使用 `ScriptedModel` 驱动修复、注入保护测试、验证导出的补丁，并写入 `docs/evaluation-results/offline-latest.json`。它用于检查编排与验证流程是否发生回归。由于工具选择由脚本预设，其通过率不能证明模型的自主修复质量。

一份注明日期且已脱敏的真实模型冒烟评测报告位于 [`evaluation-results/real-model-smoke-2026-11-06.json`](evaluation-results/real-model-smoke-2026-11-06.json)。报告记录了两个合成任务各一次尝试的结果，不将其描述为通用修复成功率或公开基准成绩。

## 真实模型评测报告规范

只有在固定仓库提交、依赖环境、任务提示词、保护检查、模型配置和预算后，才应运行 `repofix eval manifest.json`。请保留原始报告和失败轨迹。有效的报告应注明日期、模型/服务提供方、尝试策略、任务集来源、通过率、工具调用、Token 和耗时。服务提供方未返回用量时，应报告为“不可用”，而不能解释为零成本。

