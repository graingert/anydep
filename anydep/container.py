from collections import deque
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from typing import (
    AsyncGenerator,
    Callable,
    Deque,
    Dict,
    Hashable,
    List,
    Set,
    Tuple,
    cast,
    overload,
)

from anydep.concurrency import wrap_call
from anydep.exceptions import (
    CircularDependencyError,
    DuplicateScopeError,
    UnknownScopeError,
)
from anydep.models import (
    AsyncGeneratorProvider,
    CallableProvider,
    CoroutineProvider,
    Dependant,
    Dependency,
    DependencyProvider,
    DependencyType,
    GeneratorProvider,
    Scope,
)
from anydep.tasks import Task


class ContainerState:
    def __init__(self) -> None:
        self.binds: Dict[DependencyProvider, Dependant[Dependency]] = {}
        self.cached_values: Dict[Hashable, Dict[DependencyProvider, Dependency]] = {}
        self.stacks: Dict[Scope, AsyncExitStack] = {}
        self.scopes: List[Scope] = []

    def copy(self) -> "ContainerState":
        new = ContainerState()
        new.binds = self.binds.copy()
        new.cached_values = self.cached_values.copy()
        new.stacks = self.stacks.copy()
        new.scopes = self.scopes.copy()
        return new

    @asynccontextmanager
    async def enter_scope(self, scope: Scope) -> AsyncGenerator[None, None]:
        if scope in self.stacks:
            raise DuplicateScopeError(f"Scope {scope} has already been entered!")
        async with AsyncExitStack() as stack:
            self.scopes.append(scope)
            self.stacks[scope] = stack
            bound_providers = self.binds
            self.binds = bound_providers.copy()
            self.cached_values[scope] = {}
            try:
                yield
            finally:
                self.stacks.pop(scope)
                self.binds = bound_providers
                self.cached_values.pop(scope)
                self.scopes.pop()


class Container:
    def __init__(self) -> None:
        self.context = ContextVar[ContainerState]("context")
        state = ContainerState()
        self.context.set(state)

    @property
    def state(self) -> ContainerState:
        return self.context.get()

    @overload
    def bind(
        self, target: AsyncGeneratorProvider[DependencyType], source: AsyncGeneratorProvider[DependencyType]
    ) -> None:
        ...  # pragma: no cover

    @overload
    def bind(self, target: CoroutineProvider[DependencyType], source: CoroutineProvider[DependencyType]) -> None:
        ...  # pragma: no cover

    @overload
    def bind(self, target: GeneratorProvider[DependencyType], source: GeneratorProvider[DependencyType]) -> None:
        ...  # pragma: no cover

    @overload
    def bind(self, target: CallableProvider[DependencyType], source: CallableProvider[DependencyType]) -> None:
        ...  # pragma: no cover

    def bind(self, target: DependencyProvider, source: DependencyProvider) -> None:
        self.state.binds[target] = Dependant(source)  # type: ignore
        for cached_values in self.state.cached_values.values():
            if target in cached_values:
                cached_values.pop(target)

    @asynccontextmanager
    async def enter_global_scope(self, scope: Scope) -> AsyncGenerator[None, None]:
        async with self.state.enter_scope(scope):
            yield

    @asynccontextmanager
    async def enter_local_scope(self, scope: Scope) -> AsyncGenerator[None, None]:
        current = self.state
        new = current.copy()
        token = self.context.set(new)
        try:
            async with self.state.enter_scope(scope):
                yield
        finally:
            self.context.reset(token)

    def _resolve_scope(self, dependant_scope: Scope) -> Hashable:
        if dependant_scope is False:
            return False
        elif dependant_scope is None:
            if len(self.state.scopes) == 0:
                raise UnknownScopeError(
                    "No current scope in container."
                    " You must set a scope before you can execute or resolve any dependencies."
                )
            return self.state.scopes[-1]  # current scope
        else:
            return dependant_scope

    def _task_from_cached_value(
        self, dependant: Dependant, value: DependencyType
    ) -> Task[Callable[[], DependencyType]]:
        async def retrieve():
            return value

        return Task(dependant=dependant, call=retrieve, dependencies={}, scope=False)

    def _check_scope(self, scope: Scope):
        if scope is False:
            return
        if scope not in self.state.stacks:  # self._stacks is just an O(1) lookup of current scopes
            raise UnknownScopeError(
                f"Scope {scope} is not known. Did you forget to enter it? Known scopes: {self.state.scopes}"
            )

    def _build_task(
        self,
        *,
        dependant: Dependant[DependencyType],
        task_cache: Dict[Dependant[Dependency], Task[Dependency]],
        call_cache: Dict[DependencyProvider, Dependant[Dependency]],
        seen: Set[Dependant[Dependency]],
    ) -> Tuple[bool, Task[DependencyType]]:

        if dependant in seen:
            raise CircularDependencyError(f"Circular dependency detected including node {dependant}")
        seen.add(dependant)

        scope = self._resolve_scope(dependant.scope)
        task_scope = scope

        if dependant.call in self.state.binds:
            dependant = self.state.binds[dependant.call]  # type: ignore

        if scope is not False:
            if dependant.call in call_cache:
                dependant = call_cache[dependant.call]
            else:
                call_cache[dependant.call] = dependant  # type: ignore

        if dependant in task_cache:
            return True, task_cache[dependant]  # type: ignore

        scope = self._resolve_scope(dependant.scope)
        self._check_scope(scope)
        call = wrap_call(
            cast(Callable[..., DependencyProvider], dependant.call),
            self.state.stacks[scope if scope is not False else self.state.scopes[-1]],
        )

        subtasks = {}
        allow_cache = True
        for param_name, sub_dependant in dependant.dependencies.items():
            allow_cache, subtask = self._build_task(
                dependant=sub_dependant, task_cache=task_cache, call_cache=call_cache, seen=seen
            )
            task_cache[sub_dependant] = subtask
            subtasks[param_name] = subtask

        if allow_cache and scope is not False:
            # try to get cached value
            for cache_scope in reversed(self.state.scopes):
                cached_values = self.state.cached_values[cache_scope]
                if dependant.call in cached_values:
                    value = cached_values[dependant.call]
                    task = self._task_from_cached_value(dependant, value)
                    task_cache[dependant] = task
                    return True, task  # type: ignore

        task = Task(dependant=dependant, call=call, dependencies=subtasks, scope=task_scope)  # type: ignore
        task_cache[dependant] = task
        return False, task  # type: ignore

    async def execute(self, dependant: Dependant[DependencyType]) -> DependencyType:
        task_cache: Dict[Dependant[DependencyProvider], Task[DependencyProvider]] = {}
        _, task = self._build_task(dependant=dependant, task_cache=task_cache, call_cache={}, seen=set())
        result = await task.result()
        scope = self._resolve_scope(task.scope)
        if scope is not False:
            self.state.cached_values[scope][cast(DependencyProvider, task.dependant.call)] = result
        for subtask in task_cache.values():
            scope = self._resolve_scope(subtask.scope)
            if scope is not False:
                v = await subtask.result()
                self.state.cached_values[scope][cast(DependencyProvider, subtask.dependant.call)] = v
        return result

    def get_flat_subdependants(self, dependant: Dependant) -> Set[Dependant]:
        seen: Set[Dependant] = set()
        to_visit: Deque[Dependant] = deque([dependant])
        while to_visit:
            dep = to_visit.popleft()
            seen.add(dep)
            for sub in dep.dependencies.values():
                if sub not in seen:
                    to_visit.append(sub)
        return seen - set([dependant])
