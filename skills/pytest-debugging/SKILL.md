---
name: pytest-debugging
description: 修改实现前，先从范围最小的失败测试开始诊断 pytest 测试套件。
---

# Pytest 调试

1. 使用 `pytest -q path::test_name` 运行当前已知范围最小的失败测试。
2. 阅读完整 traceback 和相关实现。
3. 编辑前先明确说明具体原因。
4. 使用 `replace_in_file` 进行聚焦修改。
5. 先重新运行小范围测试，再运行验收命令。
6. 不得仅为获得通过结果而删除、跳过、弱化或重写测试。

