"""Publish state tree and state path history of the report viewer."""
from __future__ import annotations

from dataclasses import dataclass
import json
import typing
from typing import Any

from qtpy import QtWidgets, QtCore, QtGui

from ayon_core.lib.icon_definitions import MaterialSymbolsIcon
from ayon_core.pipeline.publish.state import (
    CONTEXT_ID,
    STEP_SOURCE_OUTSIDE,
    BadgeOp,
    StateBadges,
    StatePath,
)
from ayon_core.tools.utils import get_qt_icon

if typing.TYPE_CHECKING:
    from ayon_core.pipeline.publish.report import (
        PublishPluginReportInfo,
        PublishReport,
    )
    from ayon_core.pipeline.publish.state import (
        PublishStateHistory,
        StateHistoryEntry,
    )

# Id of node in 'StateTreeModel', Qt would convert python containers
STATE_NODE_ROLE = QtCore.Qt.UserRole + 1
STATE_BADGES_ROLE = QtCore.Qt.UserRole + 4
STATE_DIM_BADGES_ROLE = QtCore.Qt.UserRole + 5
STATE_FILLED_ROLE = QtCore.Qt.UserRole + 6
HISTORY_STEP_ROLE = QtCore.Qt.UserRole + 11

BADGES_COLUMN = 0
KEY_COLUMN = 1
VALUE_COLUMN = 2
# Item in this column holds node data and children rows
ANCHOR_COLUMN = BADGES_COLUMN

HISTORY_ORDER_COLUMN = 0
HISTORY_PLUGIN_COLUMN = 1
HISTORY_OP_COLUMN = 2
HISTORY_VALUE_COLUMN = 3

OUTSIDE_PLUGINS_LABEL = "Outside plugins"

# Order of badges in the tree, also priority of key color
BADGE_ORDER = ("removed", "added", "modified", "read")
BADGE_COLORS = {
    "modified": "#e5c07b",
    "added": "#77ae24",
    "read": "#5ea9e6",
    "removed": "#ff4a4a",
}
BADGE_ICONS = {
    "modified": "edit",
    "added": "add",
    "read": "visibility",
    "removed": "delete",
}
BADGE_TOOLTIPS = {
    "modified": "Modified",
    "added": "Added",
    "read": "Read",
    "removed": "Removed",
}

# Rows without changes and steps after the last selected step
DIMMED_COLOR = "#7a8089"

_MAX_VALUE_TEXT = 300


def _format_value(value: Any) -> str:
    """Short single line representation of serialized value."""
    if isinstance(value, dict):
        return f"{{{len(value)} items}}"
    if isinstance(value, list):
        return f"[{len(value)} items]"
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value)
    text = text.replace("\n", "\\n")
    if len(text) > _MAX_VALUE_TEXT:
        text = text[:_MAX_VALUE_TEXT] + "..."
    return text


def _format_path(path: StatePath) -> str:
    if not path:
        return "<root>"
    parts = []
    for key in path:
        if isinstance(key, int):
            parts.append(f"[{key}]")
        elif parts:
            parts.append(f".{key}")
        else:
            parts.append(str(key))
    return "".join(parts)


def _get_badge_icon(op: str) -> QtGui.QIcon:
    return get_qt_icon(MaterialSymbolsIcon(
        name=BADGE_ICONS[op], color=BADGE_COLORS[op]
    ))


@dataclass
class StateNode:
    """Item of state tree."""
    root: str
    path: StatePath
    value: Any


def _set_font_style(
    item: QtGui.QStandardItem,
    *,
    bold: bool = False,
    italic: bool = False,
    strike_out: bool = False,
) -> None:
    font = item.font()
    if bold:
        font.setBold(True)
    if italic:
        font.setItalic(True)
    if strike_out:
        font.setStrikeOut(True)
    item.setFont(font)


class StateTreeModel(QtGui.QStandardItemModel):
    """Model showing serialized publish state with change badges.

    Children of containers are created lazily, when parent is expanded.

    """
    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self.setHorizontalHeaderLabels(["", "Key", "Value"])
        self._badges: dict[tuple[str, StatePath], set[BadgeOp]] = {}
        self._dim_badges: dict[tuple[str, StatePath], set[str]] = {}
        self._removed_values: dict[tuple[str, StatePath], Any] = {}
        # Removed child keys by root and parent path
        self._removed_children: dict[
            tuple[str, StatePath], list[Any]
        ] = {}
        # Root, path and value by node id
        self._nodes: dict[int, StateNode] = {}

    def get_node(self, index: QtCore.QModelIndex) -> StateNode | None:
        """Get root, path and value of item."""
        if not index.isValid():
            return None
        node_id = index.siblingAtColumn(ANCHOR_COLUMN).data(STATE_NODE_ROLE)
        if node_id is None:
            return None
        return self._nodes.get(node_id)

    def should_auto_expand(self, index: QtCore.QModelIndex) -> bool:
        """Roots and items with changed children are expanded."""
        node = self.get_node(index)
        if node is None:
            return False
        return not node.path or (node.root, node.path) in self._dim_badges

    def set_state(
        self,
        roots: list[tuple[str, str]],
        state: dict[str, Any],
        badges: StateBadges | None,
    ) -> None:
        """Show state of roots.

        Args:
            roots (list[tuple[str, str]]): Root ids with labels.
            state (dict[str, Any]): Serialized state.
            badges (StateBadges | None): Badges to show.

        """
        self.removeRows(0, self.rowCount())
        self._badges = {}
        self._dim_badges = {}
        self._removed_values = {}
        self._removed_children = {}
        self._nodes = {}
        if badges is not None:
            self._badges = badges.badges
            self._removed_values = badges.removed_values
            for (root, path), ops in badges.badges.items():
                for idx in range(len(path)):
                    self._dim_badges.setdefault(
                        (root, path[:idx]), set()
                    ).update(ops)
                if "removed" in ops and path:
                    self._removed_children.setdefault(
                        (root, path[:-1]), []
                    ).append(path[-1])

        root_item = self.invisibleRootItem()
        for root, label in roots:
            removed = root not in state
            if removed and (root, ()) not in self._removed_values:
                continue
            value = (
                self._removed_values.get((root, ()))
                if removed
                else state[root]
            )
            root_item.appendRow(
                self._create_row(root, (), label, value, removed)
            )

    def fill_children(self, index: QtCore.QModelIndex) -> None:
        """Create children of item if not created yet."""
        if not index.isValid():
            return
        item = self.itemFromIndex(index.siblingAtColumn(ANCHOR_COLUMN))
        node = self.get_node(index)
        if item is None or node is None or item.data(STATE_FILLED_ROLE):
            return
        item.setData(True, STATE_FILLED_ROLE)
        item.removeRows(0, item.rowCount())

        root, path, value = node.root, node.path, node.value
        removed_keys = list(self._removed_children.get((root, path), []))
        if isinstance(value, dict):
            children = list(value.items())
        else:
            children = list(enumerate(value))
            # Removed list items are compared to the indexes after removal
            removed_keys.sort()

        existing = {key for key, _ in children}
        rows = []
        for key, child_value in children:
            rows.append(self._create_row(
                root, path + (key,), str(key), child_value, False
            ))
        for key in removed_keys:
            if key in existing:
                continue
            child_path = path + (key,)
            rows.append(self._create_row(
                root,
                child_path,
                str(key),
                self._removed_values.get((root, child_path)),
                True,
            ))
        for row in rows:
            item.appendRow(row)

    def _create_row(
        self,
        root: str,
        path: StatePath,
        label: str,
        value: Any,
        removed: bool,
    ) -> list[QtGui.QStandardItem]:
        key = (root, path)
        ops = set(self._badges.get(key, ()))
        if removed:
            ops.add("removed")
        dim_ops = self._dim_badges.get(key, set()) - ops

        key_item = QtGui.QStandardItem(label)
        key_item.setEditable(False)
        if not path:
            _set_font_style(key_item, bold=True)
        for op in BADGE_ORDER:
            # Key that exists was re-added after removal
            if op == "removed" and not removed:
                continue
            if op in ops:
                key_item.setForeground(QtGui.QColor(BADGE_COLORS[op]))
                break
        if removed:
            _set_font_style(key_item, strike_out=True)
        # Dim rows without changes, only when changes are shown
        is_dimmed = bool(self._badges) and not ops and not dim_ops
        if is_dimmed:
            key_item.setForeground(QtGui.QColor(DIMMED_COLOR))

        badges_item = QtGui.QStandardItem()
        badges_item.setEditable(False)
        node_id = len(self._nodes)
        self._nodes[node_id] = StateNode(root, path, value)
        badges_item.setData(node_id, STATE_NODE_ROLE)
        badges_item.setData(False, STATE_FILLED_ROLE)
        badges_item.setData(
            [op for op in BADGE_ORDER if op in ops], STATE_BADGES_ROLE
        )
        badges_item.setData(
            [op for op in BADGE_ORDER if op in dim_ops],
            STATE_DIM_BADGES_ROLE,
        )
        tooltip_parts = [BADGE_TOOLTIPS[op] for op in BADGE_ORDER if op in ops]
        if dim_ops:
            tooltip_parts.append("Children: " + ", ".join(
                BADGE_TOOLTIPS[op].lower()
                for op in BADGE_ORDER
                if op in dim_ops
            ))
        if tooltip_parts:
            badges_item.setToolTip("\n".join(tooltip_parts))

        value_text = _format_value(value)
        value_item = QtGui.QStandardItem(value_text)
        value_item.setEditable(False)
        value_item.setToolTip(value_text)
        if removed:
            value_item.setForeground(QtGui.QColor(BADGE_COLORS["removed"]))
            _set_font_style(value_item, strike_out=True)
        elif is_dimmed:
            value_item.setForeground(QtGui.QColor(DIMMED_COLOR))

        if isinstance(value, (dict, list)) and value:
            # Placeholder so the item can be expanded
            badges_item.appendRow([QtGui.QStandardItem()])
        return [badges_item, key_item, value_item]


class StateBadgesDelegate(QtWidgets.QStyledItemDelegate):
    """Paint badge icons, badges of children are dimmed."""
    _icon_size = 14
    _spacing = 2

    def sizeHint(self, option, index):
        count = len(index.data(STATE_BADGES_ROLE) or []) + len(
            index.data(STATE_DIM_BADGES_ROLE) or []
        )
        width = max(1, count) * (self._icon_size + self._spacing) + 4
        return QtCore.QSize(width, self._icon_size + 4)

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        ops = index.data(STATE_BADGES_ROLE) or []
        dim_ops = index.data(STATE_DIM_BADGES_ROLE) or []
        if not ops and not dim_ops:
            return

        size = self._icon_size
        rect = option.rect
        pos_x = rect.x() + 2
        pos_y = rect.y() + int((rect.height() - size) / 2)
        painter.save()
        for opacity, items in ((1.0, ops), (0.35, dim_ops)):
            painter.setOpacity(opacity)
            for op in items:
                pixmap = _get_badge_icon(op).pixmap(size, size)
                painter.drawPixmap(pos_x, pos_y, size, size, pixmap)
                pos_x += size + self._spacing
        painter.restore()


class StateHistoryModel(QtGui.QStandardItemModel):
    """History of a state path, one row per step that touched it."""
    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._reset_header()

    def _reset_header(self) -> None:
        self.setHorizontalHeaderLabels(["Order", "Plugin", "Op", "Value"])

    def set_history(
        self,
        history: PublishStateHistory | None,
        entries: list[StateHistoryEntry],
        plugin_infos: dict[str, PublishPluginReportInfo],
        instance_labels: dict[str, str],
        selected_steps: set[int],
    ) -> None:
        """Show history entries of a state path.

        Args:
            history (PublishStateHistory | None): Recorded state history.
            entries (list[StateHistoryEntry]): Entries to show.
            plugin_infos (dict[str, PublishPluginReportInfo]): Plugins
                by id.
            instance_labels (dict[str, str]): Instance labels by id.
            selected_steps (set[int]): Selected step indexes. Plugin of
                selected step is bold, steps after the last selected step
                are dimmed.

        """
        self.removeRows(0, self.rowCount())
        if history is None:
            return
        last_selected = max(selected_steps) if selected_steps else None
        for entry in entries:
            step = history.steps[entry.step_index]
            order_text = ""
            plugin_tooltip = ""
            if step.source == STEP_SOURCE_OUTSIDE:
                plugin_label = OUTSIDE_PLUGINS_LABEL
            else:
                plugin_info = plugin_infos.get(step.plugin_id or "")
                plugin_label = step.plugin_id or ""
                if plugin_info is not None:
                    plugin_label = plugin_info.label or plugin_info.name
                    order_text = f"{plugin_info.order:g}"
                instance_label = instance_labels.get(
                    step.instance_id or "", step.instance_id or ""
                )
                plugin_tooltip = f"{plugin_label}\nInstance: {instance_label}"

            if entry.op == "read":
                value_text = ""
            elif entry.op == "removed":
                value_text = _format_value(entry.old_value)
            elif entry.op == "modified":
                value_text = "{} → {}".format(
                    _format_value(entry.old_value),
                    _format_value(entry.value),
                )
            else:
                value_text = _format_value(entry.value)

            texts = [
                order_text,
                plugin_label,
                BADGE_TOOLTIPS[entry.op],
                value_text,
            ]
            items = []
            for text in texts:
                item = QtGui.QStandardItem(text)
                item.setEditable(False)
                item.setToolTip(text)
                item.setData(entry.step_index, HISTORY_STEP_ROLE)
                items.append(item)
            plugin_item = items[HISTORY_PLUGIN_COLUMN]
            op_item = items[HISTORY_OP_COLUMN]
            if plugin_tooltip:
                plugin_item.setToolTip(plugin_tooltip)
            if entry.path:
                op_item.setToolTip(
                    f"{BADGE_TOOLTIPS[entry.op]}: {_format_path(entry.path)}"
                )
            op_item.setIcon(_get_badge_icon(entry.op))

            is_future = (
                last_selected is not None
                and entry.step_index > last_selected
            )
            if is_future:
                for item in items:
                    item.setForeground(QtGui.QColor(DIMMED_COLOR))
            else:
                op_item.setForeground(QtGui.QColor(BADGE_COLORS[entry.op]))
            if entry.step_index in selected_steps:
                _set_font_style(plugin_item, bold=True)
            if step.source == STEP_SOURCE_OUTSIDE:
                _set_font_style(plugin_item, italic=True)
            self.appendRow(items)


class PublishStateWidget(QtWidgets.QWidget):
    """State tree with history of selected path.

    The tree shows state after last selected step, badges are aggregated
    over all selected steps.

    Signals:
        step_hovered (object, object): Plugin id and instance id of
            hovered history entry, 'None' values when nothing is hovered.
        step_clicked (object, object): Plugin id and instance id of
            clicked history entry.

    """
    step_hovered = QtCore.Signal(object, object)
    step_clicked = QtCore.Signal(object, object)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)

        empty_label = QtWidgets.QLabel("No state recorded", self)
        empty_label.setAlignment(QtCore.Qt.AlignCenter)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal, self)
        splitter.setChildrenCollapsible(True)

        state_model = StateTreeModel(self)
        state_view = QtWidgets.QTreeView(splitter)
        state_view.setObjectName("PublishStateView")
        state_view.setModel(state_model)
        state_view.setUniformRowHeights(True)
        state_view.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers
        )
        state_view.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows
        )
        badges_delegate = StateBadgesDelegate(state_view)
        state_view.setItemDelegateForColumn(BADGES_COLUMN, badges_delegate)
        # Badges are first, expand arrows are next to keys
        state_view.setTreePosition(KEY_COLUMN)
        header = state_view.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(
            BADGES_COLUMN, QtWidgets.QHeaderView.ResizeToContents
        )
        state_view.setColumnWidth(KEY_COLUMN, 220)

        history_model = StateHistoryModel(self)
        history_view = QtWidgets.QTreeView(splitter)
        history_view.setObjectName("PublishStateHistoryView")
        history_view.setModel(history_model)
        history_view.setRootIsDecorated(False)
        history_view.setUniformRowHeights(True)
        history_view.setMouseTracking(True)
        history_view.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers
        )
        history_view.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows
        )
        history_view.header().setStretchLastSection(True)
        history_view.viewport().installEventFilter(self)

        splitter.addWidget(state_view)
        splitter.addWidget(history_view)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(empty_label, 1)
        layout.addWidget(splitter, 1)

        state_view.expanded.connect(self._on_state_expanded)
        state_view.selectionModel().currentChanged.connect(
            self._on_state_current_changed
        )
        history_view.entered.connect(self._on_history_entered)
        history_view.clicked.connect(self._on_history_clicked)

        self._empty_label = empty_label
        self._splitter = splitter
        self._state_model = state_model
        self._state_view = state_view
        self._history_model = history_model
        self._history_view = history_view

        self._report: PublishReport | None = None
        self._plugin_ids: set[str] = set()
        self._instance_ids: set[str] = set()
        self._selected_path: tuple[str, StatePath] | None = None
        self._hovered_step: int | None = None
        self._plugin_infos: dict[str, PublishPluginReportInfo] = {}
        self._instance_labels: dict[str, str] = {}
        self._selected_steps: set[int] = set()

        self._update_visibility()

    def set_report(self, report: PublishReport | None) -> None:
        self._report = report
        self._plugin_infos = {}
        self._instance_labels = {}
        if report is not None:
            self._plugin_infos = {
                plugin_info.id: plugin_info
                for plugin_info in report.plugins_info
            }
            self._instance_labels = {
                instance_id: instance_info.label or instance_id
                for instance_id, instance_info
                in report.instances_by_id.items()
            }
            self._instance_labels[CONTEXT_ID] = report.context.label
        self._update_visibility()
        self._refresh()

    def set_selection(
        self, plugin_ids: set[str], instance_ids: set[str]
    ) -> None:
        """Set selected plugins and instances of the report viewer."""
        self._plugin_ids = set(plugin_ids)
        self._instance_ids = set(instance_ids)
        self._refresh()

    def eventFilter(self, watched, event):
        if (
            watched is self._history_view.viewport()
            and event.type() == QtCore.QEvent.Leave
        ):
            self._set_hovered_step(None)
        return super().eventFilter(watched, event)

    def _get_history(self) -> PublishStateHistory | None:
        if self._report is None:
            return None
        return self._report.state

    def _update_visibility(self) -> None:
        has_state = self._get_history() is not None
        self._empty_label.setVisible(not has_state)
        self._splitter.setVisible(has_state)

    def _get_root_label(self, root: str) -> str:
        return self._instance_labels.get(root, root)

    def _refresh(self) -> None:
        history = self._get_history()
        self._selected_steps = set()
        if history is None:
            self._state_model.set_state([], {}, None)
            self._update_history()
            return

        has_selection = bool(self._plugin_ids or self._instance_ids)
        badges = None
        if not has_selection:
            state = history.final_state()
            root_ids = list(state)
        else:
            step_indexes = history.get_step_indexes(
                self._plugin_ids or None, self._instance_ids or None
            )
            self._selected_steps = set(step_indexes)
            if step_indexes:
                state = history.state_at(step_indexes[-1])
                badges = history.changes_for(step_indexes)
            else:
                state = history.final_state()

            steps_roots = set()
            for step_index in step_indexes:
                step = history.steps[step_index]
                steps_roots.add(step.instance_id)
                steps_roots |= step.get_roots()

            if self._instance_ids:
                scope = set(self._instance_ids)
                # Keep root of selected path if selected steps touched it
                if (
                    self._selected_path is not None
                    and self._selected_path[0] in steps_roots
                ):
                    scope.add(self._selected_path[0])
            else:
                scope = steps_roots
            scope.add(CONTEXT_ID)
            root_ids = [CONTEXT_ID]
            known = set(state) | set(history.initial_state)
            if badges is not None:
                known |= {root for root, _ in badges.badges}
            root_ids.extend(
                root
                for root in self._get_root_order(history, state)
                if root in scope and root in known and root != CONTEXT_ID
            )

        roots = [(root, self._get_root_label(root)) for root in root_ids]
        # Removing rows would reset selected path
        selection_model = self._state_view.selectionModel()
        selection_model.blockSignals(True)
        try:
            self._state_model.set_state(roots, state, badges)
        finally:
            selection_model.blockSignals(False)
        self._auto_expand(QtCore.QModelIndex())
        self._restore_selected_path()
        self._update_history()

    def _auto_expand(self, parent: QtCore.QModelIndex) -> None:
        """Expand roots and items leading to changes."""
        model = self._state_model
        for row in range(model.rowCount(parent)):
            index = model.index(row, ANCHOR_COLUMN, parent)
            if not model.should_auto_expand(index):
                continue
            model.fill_children(index)
            self._state_view.expand(index)
            self._auto_expand(index)

    def _get_root_order(
        self, history: PublishStateHistory, state: dict[str, Any]
    ) -> list[str]:
        """All root ids in order of appearance in the report."""
        output = list(history.initial_state)
        seen = set(output)
        for step in history.steps:
            for change in step.changes:
                if change.root not in seen:
                    seen.add(change.root)
                    output.append(change.root)
        output.extend(root for root in state if root not in seen)
        return output

    def _restore_selected_path(self) -> None:
        if self._selected_path is None:
            return
        root, path = self._selected_path
        model = self._state_model
        parent = QtCore.QModelIndex()
        found = QtCore.QModelIndex()
        for depth in range(len(path) + 1):
            match = None
            for row in range(model.rowCount(parent)):
                index = model.index(row, ANCHOR_COLUMN, parent)
                node = model.get_node(index)
                if (
                    node is not None
                    and node.root == root
                    and node.path == path[:depth]
                ):
                    match = index
                    break
            if match is None:
                break
            found = match
            if depth < len(path):
                model.fill_children(match)
                self._state_view.expand(match)
                parent = match

        if not found.isValid():
            return
        selection_model = self._state_view.selectionModel()
        found = found.siblingAtColumn(KEY_COLUMN)
        selection_model.blockSignals(True)
        self._state_view.setCurrentIndex(found)
        selection_model.blockSignals(False)
        self._state_view.scrollTo(found)

    def _update_history(self) -> None:
        history = self._get_history()
        entries = []
        if history is not None and self._selected_path is not None:
            root, path = self._selected_path
            entries = history.history_for(root, path)
        self._history_model.set_history(
            history,
            entries,
            self._plugin_infos,
            self._instance_labels,
            self._selected_steps,
        )
        for column in range(self._history_model.columnCount() - 1):
            self._history_view.resizeColumnToContents(column)

    def _on_state_expanded(self, index: QtCore.QModelIndex) -> None:
        self._state_model.fill_children(index)

    def _on_state_current_changed(
        self, current: QtCore.QModelIndex, _previous: QtCore.QModelIndex
    ) -> None:
        node = self._state_model.get_node(current)
        self._selected_path = None
        if node is not None:
            self._selected_path = (node.root, node.path)
        self._update_history()

    def _get_step_ids(
        self, step_index: int
    ) -> tuple[str | None, str | None]:
        history = self._get_history()
        if history is None or step_index >= len(history.steps):
            return None, None
        step = history.steps[step_index]
        return step.plugin_id, step.instance_id

    def _set_hovered_step(self, step_index: int | None) -> None:
        if step_index == self._hovered_step:
            return
        self._hovered_step = step_index
        if step_index is None:
            self.step_hovered.emit(None, None)
            return
        self.step_hovered.emit(*self._get_step_ids(step_index))

    def _on_history_entered(self, index: QtCore.QModelIndex) -> None:
        step_index = index.data(HISTORY_STEP_ROLE)
        self._set_hovered_step(step_index)

    def _on_history_clicked(self, index: QtCore.QModelIndex) -> None:
        step_index = index.data(HISTORY_STEP_ROLE)
        if step_index is None:
            return
        plugin_id, instance_id = self._get_step_ids(step_index)
        if plugin_id is None:
            return
        self.step_clicked.emit(plugin_id, instance_id)
