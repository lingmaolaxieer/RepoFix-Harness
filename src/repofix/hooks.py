from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from repofix.models import ToolResult


class Hook(Protocol):
    async def before_tool(self, name: str, arguments: dict[str, Any]) -> None: ...
    async def after_tool(self, result: ToolResult) -> ToolResult: ...
    async def before_verify(self) -> None: ...


@dataclass
class HookManager:
    hooks: list[Hook] = field(default_factory=list)

    async def before_tool(self, name: str, arguments: dict[str, Any]) -> None:
        for hook in self.hooks:
            await hook.before_tool(name, arguments)

    async def after_tool(self, result: ToolResult) -> ToolResult:
        for hook in self.hooks:
            result = await hook.after_tool(result)
        return result

    async def before_verify(self) -> None:
        for hook in self.hooks:
            await hook.before_verify()


class NoopHook:
    async def before_tool(self, name: str, arguments: dict[str, Any]) -> None:
        return None

    async def after_tool(self, result: ToolResult) -> ToolResult:
        return result

    async def before_verify(self) -> None:
        return None
