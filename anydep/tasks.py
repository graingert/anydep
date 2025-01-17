from typing import Any, Awaitable, Callable, Dict, Generic

import anyio

from anydep.models import (
    Dependant,
    DependencyProvider,
    DependencyProviderType,
    DependencyType,
    Scope,
)

_UNSET = object()


class Task(Generic[DependencyType]):
    def __init__(
        self,
        dependant: Dependant[DependencyProviderType[DependencyType]],
        scope: Scope,
        call: Callable[..., Awaitable[DependencyType]],
        dependencies: Dict[str, "Task[DependencyProvider]"],
    ) -> None:
        self.dependant = dependant
        self.scope = scope
        self.call = call
        self.dependencies = dependencies
        self._result: Any = _UNSET
        self._lock = anyio.Lock()

    async def result(self):
        async with self._lock:
            if self._result is _UNSET:
                async with anyio.create_task_group() as tg:
                    for subtask in self.dependencies.values():
                        tg.start_soon(subtask.result)
                values = dict.fromkeys(self.dependencies.keys(), None)
                for k in values.keys():
                    values[k] = await self.dependencies[k].result()
                self._result = await self.call(**values)
            return self._result
