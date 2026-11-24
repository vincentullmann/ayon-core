"""Recording of publish state changes made by publish plugins.

The publish "state" is 'context.data' and 'data' of every instance. The
recorder stores the serialized state before the first plugin runs and one
delta per plugin step. Every view (state at a step, badges for selected
steps, history of a path) is computed from those.

"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import logging
import math
import typing
from typing import (
    Any,
    Generator,
    ItemsView,
    Iterable,
    Iterator,
    KeysView,
    Literal,
    ValuesView,
)

import pyblish.api
import pyblish.plugin

CONTEXT_ID = "context"

StateOp = Literal["added", "modified", "removed"]
BadgeOp = Literal["added", "modified", "removed", "read"]
StepSource = Literal["plugin", "outside plugins"]
StatePath = tuple[Any, ...]
# Serialized state: root id ('context' or instance id) -> serialized data
State = dict[str, Any]

STEP_SOURCE_PLUGIN: StepSource = "plugin"
STEP_SOURCE_OUTSIDE: StepSource = "outside plugins"
CYCLE_MARKER = "<cycle>"
# Context keys managed by pyblish inside 'pyblish.plugin.process'
IGNORED_CONTEXT_KEYS = frozenset({"results"})

log = logging.getLogger(__name__)


# --- Serialization ---
def _serialize_key(key: Any) -> str:
    if isinstance(key, str):
        return key
    # Same conversion as 'json' does for basic types
    if key is None or isinstance(key, (bool, int, float)):
        return str(key).lower() if isinstance(key, bool) else str(key)
    return _serialize_object(key)


def _serialize_object(value: Any) -> str:
    try:
        value_repr = repr(value)
    except Exception:
        value_repr = "<unrepresentable>"
    return f"{value_repr} ({type(value).__name__})"


def serialize_value(value: Any, _stack: set[int] | None = None) -> Any:
    """Convert value to JSON-native data.

    Dicts and lists are recursed. Tuples and sets become lists. Any other
    value becomes the string '"<repr> (TypeName)"'. Cycles are replaced
    with '"<cycle>"' marker.

    Args:
        value (Any): Value to serialize.

    Returns:
        Any: JSON-native value.

    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value

    is_dict = isinstance(value, dict)
    is_list = (
        isinstance(value, (list, tuple, set, frozenset))
        # Pyblish Context and Instance are lists
        and not isinstance(value, pyblish.plugin.AbstractEntity)
    )
    if not is_dict and not is_list:
        return _serialize_object(value)

    if _stack is None:
        _stack = set()
    value_id = id(value)
    if value_id in _stack:
        return CYCLE_MARKER
    _stack.add(value_id)
    try:
        if is_dict:
            return {
                _serialize_key(key): serialize_value(item, _stack)
                for key, item in dict.items(value)
            }
        items = value
        if isinstance(value, (set, frozenset)):
            try:
                items = sorted(value)
            except TypeError:
                items = list(value)
        return [serialize_value(item, _stack) for item in items]
    finally:
        _stack.discard(value_id)


# --- Diffing ---
@dataclass
class StateChange:
    """Single change of a state path made in a step.

    Attributes:
        op (StateOp): 'added', 'modified' or 'removed'.
        root (str): Root id, 'context' or instance id.
        path (StatePath): Nested path in root data. Empty path means
            the whole root was added or removed.
        value (Any): New value, 'None' for 'removed'.

    """
    op: StateOp
    root: str
    path: StatePath
    value: Any = None

    def to_data(self) -> dict[str, Any]:
        data = {"op": self.op, "root": self.root, "path": list(self.path)}
        if self.op != "removed":
            data["value"] = self.value
        return data

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> StateChange:
        return cls(
            op=data["op"],
            root=data["root"],
            path=tuple(data["path"]),
            value=data.get("value"),
        )


def _values_equal(old: Any, new: Any) -> bool:
    if type(old) is not type(new):
        return False
    if isinstance(old, float) and math.isnan(old) and math.isnan(new):
        return True
    try:
        return old == new
    except Exception:
        return False


def _diff_values(
    root: str,
    old: Any,
    new: Any,
    path: StatePath,
    output: list[StateChange],
) -> None:
    if _values_equal(old, new):
        return

    if type(old) is not type(new) or not isinstance(old, (dict, list)):
        output.append(StateChange("modified", root, path, new))
        return

    if isinstance(old, dict):
        for key in old:
            if key not in new:
                output.append(StateChange("removed", root, path + (key,)))
        for key, value in new.items():
            if key not in old:
                output.append(
                    StateChange("added", root, path + (key,), value)
                )
            else:
                _diff_values(root, old[key], value, path + (key,), output)
        return

    old_len = len(old)
    new_len = len(new)
    for idx in range(min(old_len, new_len)):
        _diff_values(root, old[idx], new[idx], path + (idx,), output)
    for idx in range(old_len, new_len):
        output.append(StateChange("added", root, path + (idx,), new[idx]))
    # Removed from the end so the changes can be applied in order
    for idx in reversed(range(new_len, old_len)):
        output.append(StateChange("removed", root, path + (idx,)))


def diff_states(old: State, new: State) -> list[StateChange]:
    """Get changes between two serialized states.

    Args:
        old (State): Previous state.
        new (State): Current state.

    Returns:
        list[StateChange]: Changes that turn 'old' into 'new'.

    """
    output: list[StateChange] = []
    for root in old:
        if root not in new:
            output.append(StateChange("removed", root, ()))
    for root, data in new.items():
        if root not in old:
            output.append(StateChange("added", root, (), data))
        else:
            _diff_values(root, old[root], data, (), output)
    return output


def _shallow_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return dict(value)
    return list(value)


def apply_changes(state: State, changes: Iterable[StateChange]) -> State:
    """Apply changes to a state.

    Input state is not modified. Containers that are not changed are
    shared between input and output state, so neither of them must
    be modified in place.

    Args:
        state (State): State to apply changes to.
        changes (Iterable[StateChange]): Changes to apply.

    Returns:
        State: New state.

    """
    new_state = dict(state)
    # Ids of containers that were already copied for this output
    copied: set[int] = set()
    for change in changes:
        if not change.path:
            if change.op == "removed":
                new_state.pop(change.root, None)
            else:
                new_state[change.root] = change.value
            continue

        parent = new_state[change.root]
        if id(parent) not in copied:
            parent = _shallow_copy(parent)
            copied.add(id(parent))
            new_state[change.root] = parent

        for key in change.path[:-1]:
            child = parent[key]
            if id(child) not in copied:
                child = _shallow_copy(child)
                copied.add(id(child))
                parent[key] = child
            parent = child

        key = change.path[-1]
        if change.op == "removed":
            del parent[key]
        elif (
            change.op == "added"
            and isinstance(parent, list)
            and key == len(parent)
        ):
            parent.append(change.value)
        else:
            parent[key] = change.value
    return new_state


_MISSING = object()


def get_path_value(state: State, root: str, path: StatePath) -> Any:
    """Get value of a path in state.

    Returns:
        Any: Value or '_MISSING' sentinel if path does not exist.

    """
    value = state.get(root, _MISSING)
    for key in path:
        if value is _MISSING:
            break
        try:
            value = value[key]
        except (KeyError, IndexError, TypeError):
            return _MISSING
    return value


# --- Read tracking ---
class TrackingDict(pyblish.plugin._Dict):
    """Pyblish data dictionary that records which keys are read.

    Reads are recorded only when '_state_reads' is set to a set, which
    happens while a publish plugin processes. Nested reads are recorded
    as read of the top-level key.

    """
    _state_reads: set[str] | None = None

    def _mark_read(self, key: Any) -> None:
        reads = self._state_reads
        if reads is not None:
            try:
                reads.add(_serialize_key(key))
            except Exception:
                pass

    def _mark_all_read(self) -> None:
        reads = self._state_reads
        if reads is not None:
            reads.update(_serialize_key(key) for key in dict.keys(self))

    def __getitem__(self, key: Any) -> Any:
        self._mark_read(key)
        return super().__getitem__(key)

    def get(self, key: Any, default: Any = None) -> Any:
        self._mark_read(key)
        return super().get(key, default)

    def __contains__(self, key: Any) -> bool:
        self._mark_read(key)
        return super().__contains__(key)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        self._mark_read(key)
        return super().setdefault(key, default)

    def pop(self, key: Any, *args: Any) -> Any:
        self._mark_read(key)
        return super().pop(key, *args)

    def __iter__(self) -> Iterator[Any]:
        self._mark_all_read()
        return super().__iter__()

    def keys(self) -> KeysView[Any]:
        self._mark_all_read()
        return super().keys()

    def values(self) -> ValuesView[Any]:
        self._mark_all_read()
        return super().values()

    def items(self) -> ItemsView[Any, Any]:
        self._mark_all_read()
        return super().items()

    def copy(self) -> dict[Any, Any]:
        self._mark_all_read()
        return super().copy()


def install_tracking_dict(
    entity: pyblish.api.Context | pyblish.api.Instance,
) -> TrackingDict | None:
    """Make 'data' of pyblish entity a tracking dictionary.

    The class of the existing object is swapped, so references to the
    data held by plugins stay valid.

    Returns:
        TrackingDict | None: Tracking dictionary or None if data
            have unexpected type.

    """
    data = entity._data
    if isinstance(data, TrackingDict):
        return data
    if type(data) is not pyblish.plugin._Dict:
        return None
    data.__class__ = TrackingDict
    return typing.cast(TrackingDict, data)


# --- Recorded data ---
@dataclass
class StateStep:
    """State changes of one plugin step.

    Attributes:
        plugin_id (str | None): Plugin id, 'None' for changes made
            outside plugins.
        instance_id (str | None): Instance id, 'context' for context
            plugins, 'None' for changes made outside plugins.
        source (StepSource): 'plugin' or 'outside plugins'.
        changes (list[StateChange]): Changes made in the step.
        reads (dict[str, list[str]]): Top-level keys read by root.

    """
    plugin_id: str | None
    instance_id: str | None
    source: StepSource
    changes: list[StateChange] = field(default_factory=list)
    reads: dict[str, list[str]] = field(default_factory=dict)

    def to_data(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "instance_id": self.instance_id,
            "source": self.source,
            "changes": [change.to_data() for change in self.changes],
            "reads": {root: list(keys) for root, keys in self.reads.items()},
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> StateStep:
        return cls(
            plugin_id=data.get("plugin_id"),
            instance_id=data.get("instance_id"),
            source=data.get("source", STEP_SOURCE_PLUGIN),
            changes=[
                StateChange.from_data(item)
                for item in data.get("changes", [])
            ],
            reads={
                root: list(keys)
                for root, keys in data.get("reads", {}).items()
            },
        )

    def get_roots(self) -> set[str]:
        roots = {change.root for change in self.changes}
        roots.update(self.reads)
        return roots


@dataclass
class StateHistoryEntry:
    """Single entry of history of a state path.

    Attributes:
        step_index (int): Index of step.
        op (BadgeOp): What happened with the path.
        path (StatePath): Path that was touched, can be the path itself,
            its parent or its child.
        value (Any): New value, 'None' for 'removed' and 'read'.
        old_value (Any): Value before the step, 'None' if it did not
            exist.

    """
    step_index: int
    op: BadgeOp
    path: StatePath
    value: Any = None
    old_value: Any = None


@dataclass
class StateBadges:
    """Badges aggregated over steps.

    Attributes:
        badges (dict[tuple[str, StatePath], set[BadgeOp]]): Ops by
            root and path.
        removed_values (dict[tuple[str, StatePath], Any]): Value before
            removal of removed paths.

    """
    badges: dict[tuple[str, StatePath], set[BadgeOp]] = field(
        default_factory=dict
    )
    removed_values: dict[tuple[str, StatePath], Any] = field(
        default_factory=dict
    )


def _is_related_path(path_a: StatePath, path_b: StatePath) -> bool:
    size = min(len(path_a), len(path_b))
    return path_a[:size] == path_b[:size]


class PublishStateHistory:
    """Initial publish state and changes of every step.

    Args:
        initial_state (State | None): State before first plugin.
        steps (list[StateStep] | None): Recorded steps.

    """
    def __init__(
        self,
        initial_state: State | None = None,
        steps: list[StateStep] | None = None,
    ) -> None:
        self.initial_state: State = initial_state or {}
        self.steps: list[StateStep] = steps or []
        # Cached state after step index, states share unchanged data
        self._states_cache: list[State] = []

    def to_data(self) -> dict[str, Any]:
        return {
            "initial_state": self.initial_state,
            "steps": [step.to_data() for step in self.steps],
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> PublishStateHistory:
        return cls(
            initial_state=data.get("initial_state") or {},
            steps=[
                StateStep.from_data(step) for step in data.get("steps", [])
            ],
        )

    def add_step(self, step: StateStep) -> None:
        self.steps.append(step)

    def state_at(self, step_index: int) -> State:
        """Get state after step.

        Args:
            step_index (int): Index of step, '-1' for the initial state.

        Returns:
            State: State after step. Must not be modified.

        """
        if step_index < 0 or not self.steps:
            return self.initial_state
        step_index = min(step_index, len(self.steps) - 1)
        cache = self._states_cache
        while len(cache) <= step_index:
            previous = cache[-1] if cache else self.initial_state
            cache.append(
                apply_changes(previous, self.steps[len(cache)].changes)
            )
        return cache[step_index]

    def final_state(self) -> State:
        return self.state_at(len(self.steps) - 1)

    def changes_for(self, step_indexes: Iterable[int]) -> StateBadges:
        """Aggregate badges over steps.

        Args:
            step_indexes (Iterable[int]): Indexes of steps.

        Returns:
            StateBadges: Aggregated badges.

        """
        output = StateBadges()
        for step_index in sorted(set(step_indexes)):
            step = self.steps[step_index]
            for root, keys in step.reads.items():
                for key in keys:
                    output.badges.setdefault((root, (key,)), set()).add(
                        "read"
                    )
            if not step.changes:
                continue
            for change in step.changes:
                key = (change.root, change.path)
                output.badges.setdefault(key, set()).add(change.op)
                if change.op == "removed":
                    output.removed_values[key] = get_path_value(
                        self.state_at(step_index - 1),
                        change.root,
                        change.path,
                    )
        for key, value in tuple(output.removed_values.items()):
            if value is _MISSING:
                output.removed_values[key] = None
        return output

    def history_for(
        self, root: str, path: StatePath
    ) -> list[StateHistoryEntry]:
        """List steps that touched the path, its parent or its child.

        Args:
            root (str): Root id.
            path (StatePath): Path in root.

        Returns:
            list[StateHistoryEntry]: History entries in step order.

        """
        path = tuple(path)
        output = []
        for step_index, step in enumerate(self.steps):
            for read_key in step.reads.get(root, []):
                read_path = (read_key,)
                if _is_related_path(read_path, path):
                    output.append(StateHistoryEntry(
                        step_index, "read", read_path
                    ))
            for change in step.changes:
                if change.root != root:
                    continue
                if not _is_related_path(change.path, path):
                    continue
                old_value = get_path_value(
                    self.state_at(step_index - 1), root, change.path
                )
                if old_value is _MISSING:
                    old_value = None
                output.append(StateHistoryEntry(
                    step_index,
                    change.op,
                    change.path,
                    value=change.value,
                    old_value=old_value,
                ))
        return output

    def get_step_indexes(
        self,
        plugin_ids: set[str] | None = None,
        instance_ids: set[str] | None = None,
    ) -> list[int]:
        """Get indexes of steps matching selection.

        Steps made outside plugins match only when no plugin is selected.
        A step matches instance selection when it was processed on
        the instance, or it changed data of the instance.

        Args:
            plugin_ids (set[str] | None): Selected plugin ids.
            instance_ids (set[str] | None): Selected instance ids.

        Returns:
            list[int]: Indexes of matching steps.

        """
        output = []
        for step_index, step in enumerate(self.steps):
            if plugin_ids and step.plugin_id not in plugin_ids:
                continue
            if (
                instance_ids
                and step.instance_id not in instance_ids
                and not any(
                    change.root in instance_ids for change in step.changes
                )
            ):
                continue
            output.append(step_index)
        return output


# --- Recorder ---
class PublishStateRecorder:
    """Record publish state changes made by publish plugins.

    Call 'begin_step' before a plugin is processed, process the plugin
    inside 'track_reads' context and call 'end_step' afterwards.

    Recording errors are logged and never interrupt publishing.

    """
    def __init__(self) -> None:
        self._history = PublishStateHistory()
        self._last_state: State | None = None
        self._dicts_by_root: dict[str, TrackingDict] = {}
        self._reads_by_root: dict[str, set[str]] = {}

    @property
    def history(self) -> PublishStateHistory:
        return self._history

    def begin_step(self, context: pyblish.api.Context) -> None:
        """Prepare a plugin step.

        Captures the initial state on first call, or records changes made
        since the last step as a step made outside plugins.

        """
        try:
            self._begin_step(context)
        except Exception:
            log.warning("Failed to record publish state", exc_info=True)

    @contextmanager
    def track_reads(self) -> Generator[None, None, None]:
        """Record reads of publish data while the context is active."""
        reads_by_root: dict[str, set[str]] = {}
        for root, data in self._dicts_by_root.items():
            reads = set()
            reads_by_root[root] = reads
            data._state_reads = reads
        self._reads_by_root = reads_by_root
        try:
            yield
        finally:
            for data in self._dicts_by_root.values():
                data._state_reads = None

    def end_step(
        self,
        context: pyblish.api.Context,
        plugin_id: str,
        instance_id: str,
    ) -> None:
        """Record changes and reads of a plugin step."""
        try:
            self._end_step(context, plugin_id, instance_id)
        except Exception:
            log.warning("Failed to record publish state", exc_info=True)
        finally:
            self._reads_by_root = {}

    def _get_entities(
        self, context: pyblish.api.Context
    ) -> list[tuple[str, pyblish.api.Context | pyblish.api.Instance]]:
        entities = [(CONTEXT_ID, context)]
        entities.extend((instance.id, instance) for instance in context)
        return entities

    def _serialize(self, context: pyblish.api.Context) -> State:
        state = {
            root: serialize_value(entity.data)
            for root, entity in self._get_entities(context)
        }
        for key in IGNORED_CONTEXT_KEYS:
            state[CONTEXT_ID].pop(key, None)
        return state

    def _begin_step(self, context: pyblish.api.Context) -> None:
        dicts_by_root = {}
        for root, entity in self._get_entities(context):
            data = install_tracking_dict(entity)
            if data is not None:
                dicts_by_root[root] = data
        self._dicts_by_root = dicts_by_root

        state = self._serialize(context)
        if self._last_state is None:
            self._history.initial_state = state
        else:
            changes = diff_states(self._last_state, state)
            if changes:
                self._history.add_step(StateStep(
                    plugin_id=None,
                    instance_id=None,
                    source=STEP_SOURCE_OUTSIDE,
                    changes=changes,
                ))
        self._last_state = state

    def _end_step(
        self,
        context: pyblish.api.Context,
        plugin_id: str,
        instance_id: str,
    ) -> None:
        state = self._serialize(context)
        changes = []
        if self._last_state is not None:
            changes = diff_states(self._last_state, state)
        else:
            self._history.initial_state = state
        self._last_state = state
        context_reads = self._reads_by_root.get(CONTEXT_ID)
        if context_reads:
            context_reads.difference_update(IGNORED_CONTEXT_KEYS)
        reads = {
            root: sorted(keys)
            for root, keys in self._reads_by_root.items()
            if keys
        }
        self._history.add_step(StateStep(
            plugin_id=plugin_id,
            instance_id=instance_id,
            source=STEP_SOURCE_PLUGIN,
            changes=changes,
            reads=reads,
        ))
