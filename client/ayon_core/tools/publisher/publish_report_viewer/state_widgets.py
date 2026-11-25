"""Publish state tree and state path history of the report viewer."""
from __future__ import annotations

from dataclasses import dataclass
import difflib
import json
import typing
from typing import Any

from qtpy import QtWidgets, QtCore, QtGui

from ayon_core.lib.icon_definitions import MaterialSymbolsIcon
from ayon_core.pipeline.publish.state import (
    _MISSING,
    CONTEXT_ID,
    STEP_SOURCE_OUTSIDE,
    BadgeOp,
    StateBadges,
    StatePath,
    get_path_value,
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
# Badge counts of item and its children, as list of 'op:count' strings
STATE_COUNTS_ROLE = QtCore.Qt.UserRole + 4
STATE_FILLED_ROLE = QtCore.Qt.UserRole + 6
HISTORY_STEP_ROLE = QtCore.Qt.UserRole + 11
HISTORY_PLUGIN_ROLE = QtCore.Qt.UserRole + 12
HISTORY_HIGHLIGHT_ROLE = QtCore.Qt.UserRole + 13
# Colored parts of value as list of [text, color], color '' is default
HISTORY_SEGMENTS_ROLE = QtCore.Qt.UserRole + 14
# Label of divider row between publish phases
HISTORY_DIVIDER_ROLE = QtCore.Qt.UserRole + 15
# Background of rows of selected plugins and instances
HISTORY_SELECTED_ROLE = QtCore.Qt.UserRole + 16
SELECTED_STEP_BG = QtGui.QColor(60, 130, 230, 51)
# Hovered plugin in plugins list and plugins that ran before it
HOVER_PLUGIN_BG = QtGui.QColor(190, 130, 235, 90)
HOVER_PRIOR_BG = QtGui.QColor(190, 130, 235, 22)

KEY_COLUMN = 0
VALUE_COLUMN = 1
# Item in this column holds node data and children rows
ANCHOR_COLUMN = KEY_COLUMN

HISTORY_ORDER_COLUMN = 0
HISTORY_PLUGIN_COLUMN = 1
HISTORY_OP_COLUMN = 2
HISTORY_VALUE_COLUMN = 3

OUTSIDE_PLUGINS_LABEL = "Outside plugins"

# Publish phases by plugin order, upper bound is exclusive
PUBLISH_PHASES = (
    (1, "Collect"),
    (2, "Validate"),
    (3, "Extract"),
    (float("inf"), "Integrate"),
)


def _get_phase(order: float) -> str:
    for upper, label in PUBLISH_PHASES:
        if order < upper:
            return label
    return PUBLISH_PHASES[-1][1]


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
# Signs shown with counts on the right side of the state tree
BADGE_SIGNS = {
    "modified": "~",
    "added": "+",
    "read": "○",
    "removed": "-",
}
BADGE_TOOLTIPS = {
    "modified": "Modified",
    "added": "Added",
    "read": "Read",
    "removed": "Removed",
}

# Rows without changes
DIMMED_COLOR = "#7a8089"
DEFAULT_VALUE_COLOR = "#c8ccd2"

# Order of badges in the status line
STATUS_LINE_ORDER = ("added", "read", "modified", "removed", "unchanged")
STATUS_LINE_LABELS = {
    "added": "added",
    "read": "read",
    "modified": "modified",
    "removed": "removed",
    "unchanged": "unchanged",
}
STATUS_LINE_ICONS = {
    **BADGE_ICONS,
    "unchanged": "equal",
}
STATUS_LINE_ICON_SIZE = 14

_MAX_VALUE_TEXT = 300


def _format_value(value: Any, full: bool = False) -> str:
    """Short single line representation of serialized value.

    Args:
        value (Any): Serialized value.
        full (bool): Show containers as 'str' instead of item count.

    """
    if isinstance(value, (dict, list, tuple, set)) and not full:
        if isinstance(value, dict):
            return f"{{{len(value)} items}}"
        return f"[{len(value)} items]"
    if isinstance(value, (dict, list, tuple, set)):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        text = json.dumps(value)
    text = text.replace("\n", "\\n")
    if len(text) > _MAX_VALUE_TEXT:
        text = text[:_MAX_VALUE_TEXT] + "..."
    return text


_MAX_TOOLTIP_VALUE_TEXT = 1500


def _format_full_value(value: Any) -> str:
    """Full value for tooltip, missing value is shown as empty string."""
    if value is _MISSING:
        return '""'
    if isinstance(value, (dict, list, tuple, set)):
        text = str(value)
    elif isinstance(value, str):
        text = f'"{value}"'
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > _MAX_TOOLTIP_VALUE_TEXT:
        text = text[:_MAX_TOOLTIP_VALUE_TEXT] + "..."
    return text


def _diff_segments(old: Any, new: Any) -> list[tuple[str, str]] | None:
    """Value of container with parts colored by what changed.

    Args:
        old (Any): Value before the change.
        new (Any): Value after the change.

    Returns:
        list[tuple[str, str]] | None: Text parts with op of the part
            ('' for unchanged), 'None' if values are not both lists or
            both dicts.

    """
    if isinstance(old, tuple):
        old = list(old)
    if isinstance(new, tuple):
        new = list(new)
    items: list[tuple[str, str]]
    if isinstance(old, list) and isinstance(new, list):
        items = []
        matcher = difflib.SequenceMatcher(
            None,
            [repr(item) for item in old],
            [repr(item) for item in new],
            autojunk=False,
        )
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                items.extend((repr(item), "") for item in new[j1:j2])
                continue
            paired = min(i2 - i1, j2 - j1) if tag == "replace" else 0
            items.extend(
                (repr(item), "modified") for item in new[j1:j1 + paired]
            )
            items.extend(
                (repr(item), "added") for item in new[j1 + paired:j2]
            )
            items.extend(
                (repr(item), "removed") for item in old[i1 + paired:i2]
            )
        brackets = ("[", "]")
    elif isinstance(old, dict) and isinstance(new, dict):
        # Removed keys stay after the key that preceded them
        order = list(new)
        previous = None
        for key in old:
            if key not in new:
                position = (
                    0 if previous is None else order.index(previous) + 1
                )
                order.insert(position, key)
            previous = key
        items = []
        for key in order:
            if key not in new:
                items.append((f"{key!r}: {old[key]!r}", "removed"))
                continue
            text = f"{key!r}: {new[key]!r}"
            if key not in old:
                items.append((text, "added"))
            elif old[key] != new[key]:
                items.append((text, "modified"))
            else:
                items.append((text, ""))
        brackets = ("{", "}")
    else:
        return None

    segments = [(brackets[0], "")]
    for idx, (text, op) in enumerate(items):
        if idx:
            segments.append((", ", ""))
        segments.append((text, op))
    segments.append((brackets[1], ""))
    return segments


def _format_missing(value: Any) -> str:
    if value is _MISSING:
        return ""
    return _format_value(value, full=True)


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


def _get_status_icon(op: str, color: str) -> QtGui.QIcon:
    return get_qt_icon(MaterialSymbolsIcon(
        name=STATUS_LINE_ICONS[op], color=color
    ))


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
) -> None:
    font = item.font()
    if bold:
        font.setBold(True)
    if italic:
        font.setItalic(True)
    item.setFont(font)


class StateTreeModel(QtGui.QStandardItemModel):
    """Model showing serialized publish state with change badges.

    Children of containers are created lazily, when parent is expanded.

    """
    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self.setHorizontalHeaderLabels(["Key", "Value"])
        self._badges: dict[tuple[str, StatePath], set[BadgeOp]] = {}
        self._dim_badges: dict[tuple[str, StatePath], set[str]] = {}
        # Badge counts of children by root and parent path
        self._child_counts: dict[
            tuple[str, StatePath], dict[str, int]
        ] = {}
        self._removed_values: dict[tuple[str, StatePath], Any] = {}
        # Removed child keys by root and parent path
        self._removed_children: dict[
            tuple[str, StatePath], list[Any]
        ] = {}
        # Root, path and value by node id
        self._nodes: dict[int, StateNode] = {}
        # Only rows with these badges (or with such children) are shown,
        #   'None' shows all rows
        self._status_filter: set[str] | None = None
        # Rows matching text of filter, or having matching children
        self._text_filter = ""
        self._text_matched: set[tuple[str, StatePath]] = set()
        self._text_visible: set[tuple[str, StatePath]] = set()
        self._text_has_children: set[tuple[str, StatePath]] = set()

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
        key = (node.root, node.path)
        if self._text_filter:
            return not node.path or key in self._text_has_children
        return not node.path or key in self._dim_badges

    def set_state(
        self,
        roots: list[tuple[str, str]],
        state: dict[str, Any],
        badges: StateBadges | None,
        status_filter: set[str] | None = None,
        text_filter: str = "",
    ) -> None:
        """Show state of roots.

        Args:
            roots (list[tuple[str, str]]): Root ids with labels.
            state (dict[str, Any]): Serialized state.
            badges (StateBadges | None): Badges to show.
            status_filter (set[str] | None): Show only rows with
                these badges, all rows are shown if 'None'.
            text_filter (str): Show only rows with the text in key or
                value, or with children that have it.

        """
        self._status_filter = (
            None if status_filter is None else set(status_filter)
        )
        self._text_filter = text_filter.strip().lower()
        self._text_matched = set()
        self._text_visible = set()
        self._text_has_children = set()
        self.removeRows(0, self.rowCount())
        self._badges = {}
        self._dim_badges = {}
        self._child_counts = {}
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
                    counts = self._child_counts.setdefault(
                        (root, path[:idx]), {}
                    )
                    for op in ops:
                        counts[op] = counts.get(op, 0) + 1
                if "removed" in ops and path:
                    self._removed_children.setdefault(
                        (root, path[:-1]), []
                    ).append(path[-1])

        root_item = self.invisibleRootItem()
        root_values = []
        for root, label in roots:
            removed = root not in state
            if removed and (root, ()) not in self._removed_values:
                continue
            value = (
                self._removed_values.get((root, ()))
                if removed
                else state[root]
            )
            root_values.append((root, label, value, removed))
            if self._text_filter:
                self._scan_text(root, (), label, value)

        for root, label, value, removed in root_values:
            if not self._matches_filter(root, (), removed):
                continue
            root_item.appendRow(
                self._create_row(root, (), label, value, removed)
            )

    def _scan_text(
        self, root: str, path: StatePath, label: str, value: Any
    ) -> bool:
        """Find rows matching text filter and their parents."""
        text = self._text_filter
        key = (root, path)
        matched = text in label.lower() or text in _format_value(
            value
        ).lower()
        has_children = False
        if isinstance(value, (dict, list)):
            if isinstance(value, dict):
                children = list(value.items())
            else:
                children = list(enumerate(value))
            existing = {child_key for child_key, _ in children}
            for child_key in self._removed_children.get(key, []):
                if child_key in existing:
                    continue
                children.append((
                    child_key,
                    self._removed_values.get(
                        (root, path + (child_key,))
                    ),
                ))
            for child_key, child_value in children:
                if self._scan_text(
                    root, path + (child_key,), str(child_key), child_value
                ):
                    has_children = True
        if matched:
            self._text_matched.add(key)
        if has_children:
            self._text_has_children.add(key)
        if matched or has_children:
            self._text_visible.add(key)
        return matched or has_children

    def _matches_filter(
        self, root: str, path: StatePath, removed: bool
    ) -> bool:
        if self._text_filter:
            # Children of matching row are all shown
            if (root, path) not in self._text_visible and not any(
                (root, path[:idx]) in self._text_matched
                for idx in range(len(path))
            ):
                return False
        if self._status_filter is None:
            return True
        key = (root, path)
        ops = set(self._badges.get(key, ()))
        if removed:
            ops.add("removed")
        ops |= self._dim_badges.get(key, set())
        if not ops:
            ops.add("unchanged")
        return bool(ops & self._status_filter)

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
            if not self._matches_filter(root, path + (key,), False):
                continue
            rows.append(self._create_row(
                root, path + (key,), str(key), child_value, False
            ))
        for key in removed_keys:
            if key in existing:
                continue
            child_path = path + (key,)
            if not self._matches_filter(root, child_path, True):
                continue
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
        counted_ops = set(ops)
        # Item is modified if anything below it was changed
        if (
            not removed
            and "added" not in ops
            and dim_ops & {"modified", "added", "removed"}
        ):
            ops.add("modified")

        # Same color for key and value
        color = None
        for op in BADGE_ORDER:
            # Key that exists was re-added after removal
            if op == "removed" and not removed:
                continue
            if op in ops:
                color = QtGui.QColor(BADGE_COLORS[op])
                break
        # Dim rows without changes, only when changes are shown
        is_dimmed = bool(self._badges) and not ops and not dim_ops
        if color is None and is_dimmed:
            color = QtGui.QColor(DIMMED_COLOR)

        key_item = QtGui.QStandardItem(label)
        key_item.setEditable(False)
        if not path:
            _set_font_style(key_item, bold=True)

        value_text = _format_value(value)
        value_item = QtGui.QStandardItem(value_text)
        value_item.setEditable(False)
        value_item.setToolTip(value_text)

        if color is not None:
            key_item.setForeground(color)
            value_item.setForeground(color)

        node_id = len(self._nodes)
        self._nodes[node_id] = StateNode(root, path, value)
        key_item.setData(node_id, STATE_NODE_ROLE)
        key_item.setData(False, STATE_FILLED_ROLE)

        # Counts of changes in children and own badges
        counts = dict(self._child_counts.get(key, {}))
        for op in counted_ops:
            counts[op] = counts.get(op, 0) + 1
        key_item.setData(
            [
                f"{op}:{counts[op]}"
                for op in BADGE_ORDER
                if counts.get(op)
            ],
            STATE_COUNTS_ROLE,
        )
        tooltip_parts = [BADGE_TOOLTIPS[op] for op in BADGE_ORDER if op in ops]
        if dim_ops:
            tooltip_parts.append("Children: " + ", ".join(
                BADGE_TOOLTIPS[op].lower()
                for op in BADGE_ORDER
                if op in dim_ops
            ))
        if tooltip_parts:
            key_item.setToolTip("\n".join(tooltip_parts))

        has_children = bool(value) and isinstance(value, (dict, list))
        if has_children and self._status_filter is not None:
            has_children = bool(
                self._dim_badges.get(key, set()) & self._status_filter
            ) or "unchanged" in self._status_filter
        if has_children and self._text_filter:
            has_children = key in self._text_visible or any(
                (root, path[:idx]) in self._text_matched
                for idx in range(len(path))
            )
        if has_children:
            # Placeholder so the item can be expanded
            key_item.appendRow([QtGui.QStandardItem()])
        return [key_item, value_item]


class StateHistoryDelegate(QtWidgets.QStyledItemDelegate):
    """Paint translucent hover highlight behind the row text.

    Stylesheet of the view ignores 'BackgroundRole' of items and paints
    opaque item background, so highlighted rows are painted with the
    application style instead. Items with colored parts of text are
    painted part by part.
    """
    def paint(self, painter, option, index):
        divider = index.data(HISTORY_DIVIDER_ROLE)
        if divider is not None:
            self._paint_divider(painter, option, divider)
            return
        color = index.data(HISTORY_HIGHLIGHT_ROLE)
        selected_color = index.data(HISTORY_SELECTED_ROLE)
        segments = index.data(HISTORY_SEGMENTS_ROLE)
        if color is None and selected_color is None and not segments:
            super().paint(painter, option, index)
            return
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        has_background = color is not None or selected_color is not None
        if selected_color is not None:
            painter.fillRect(opt.rect, selected_color)
        if color is not None:
            painter.fillRect(opt.rect, color)
        widget = opt.widget
        # Stylesheet of the view would paint opaque background
        style = (
            QtWidgets.QApplication.style()
            if has_background
            else widget.style() if widget else QtWidgets.QApplication.style()
        )
        if segments:
            opt.text = ""
        style.drawControl(
            QtWidgets.QStyle.CE_ItemViewItem,
            opt,
            painter,
            None if has_background else widget,
        )
        if not segments:
            return
        is_selected = bool(opt.state & QtWidgets.QStyle.State_Selected)
        text_rect = style.subElementRect(
            QtWidgets.QStyle.SE_ItemViewItemText, opt, widget
        )
        painter.save()
        painter.setClipRect(text_rect)
        painter.setFont(opt.font)
        metrics = painter.fontMetrics()
        default_color = opt.palette.color(
            QtGui.QPalette.HighlightedText
            if is_selected
            else QtGui.QPalette.Text
        )
        pos_x = text_rect.x() + 3
        for text, color_name in segments:
            painter.setPen(
                QtGui.QColor(color_name)
                if color_name and not is_selected
                else default_color
            )
            width = metrics.horizontalAdvance(text)
            painter.drawText(
                QtCore.QRect(pos_x, text_rect.y(), width, text_rect.height()),
                QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
                text,
            )
            pos_x += width
            if pos_x > text_rect.right():
                break
        painter.restore()

    @staticmethod
    def _paint_divider(painter, option, label):
        rect = option.rect
        painter.save()
        line_color = QtGui.QColor(DIMMED_COLOR)
        line_color.setAlpha(120)
        painter.setPen(line_color)
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        font = painter.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 0.85)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(DIMMED_COLOR))
        painter.drawText(
            rect.adjusted(6, 0, 0, 0),
            QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
            label.upper(),
        )
        painter.restore()


class StateValueDelegate(QtWidgets.QStyledItemDelegate):
    """Paint value with colored change counts on the right side."""
    _spacing = 8
    _margin = 6

    @staticmethod
    def _get_counts(index: QtCore.QModelIndex) -> list[tuple[str, str]]:
        output = []
        anchor = index.siblingAtColumn(ANCHOR_COLUMN)
        for part in anchor.data(STATE_COUNTS_ROLE) or []:
            op, count = part.split(":")
            output.append((op, f"{BADGE_SIGNS[op]}{count}"))
        return output

    def paint(self, painter, option, index):
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget else QtWidgets.QApplication.style()
        style.drawControl(
            QtWidgets.QStyle.CE_ItemViewItem, opt, painter, widget
        )

        is_selected = bool(opt.state & QtWidgets.QStyle.State_Selected)
        painter.save()
        painter.setFont(opt.font)
        metrics = painter.fontMetrics()
        rect = opt.rect
        right = rect.right() - self._margin
        # Counts are painted from the right side
        for op, count_text in reversed(self._get_counts(index)):
            width = metrics.horizontalAdvance(count_text)
            painter.setPen(
                opt.palette.color(QtGui.QPalette.HighlightedText)
                if is_selected
                else QtGui.QColor(BADGE_COLORS[op])
            )
            painter.drawText(
                QtCore.QRect(right - width, rect.y(), width, rect.height()),
                QtCore.Qt.AlignVCenter | QtCore.Qt.AlignRight,
                count_text,
            )
            right -= width + self._spacing

        text_rect = QtCore.QRect(
            rect.x() + self._margin,
            rect.y(),
            max(0, right - rect.x() - self._margin),
            rect.height(),
        )
        role = (
            QtGui.QPalette.HighlightedText
            if is_selected
            else QtGui.QPalette.Text
        )
        painter.setPen(opt.palette.color(role))
        painter.drawText(
            text_rect,
            QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
            metrics.elidedText(text, QtCore.Qt.ElideRight, text_rect.width()),
        )
        painter.restore()


class StateHistoryModel(QtGui.QStandardItemModel):
    """History of a state path, one row per step that touched it."""
    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._reset_header()

    def _reset_header(self) -> None:
        self.setHorizontalHeaderLabels(["Order", "Plugin", "", "Value"])

    def set_history(
        self,
        history: PublishStateHistory | None,
        entries: list[StateHistoryEntry],
        plugin_infos: dict[str, PublishPluginReportInfo],
        instance_labels: dict[str, str],
        selected_steps: set[int],
        selected_path: StatePath = (),
        selected_root: str = "",
    ) -> None:
        """Show history entries of a state path.

        Args:
            history (PublishStateHistory | None): Recorded state history.
            entries (list[StateHistoryEntry]): Entries to show.
            plugin_infos (dict[str, PublishPluginReportInfo]): Plugins
                by id.
            instance_labels (dict[str, str]): Instance labels by id.
            selected_steps (set[int]): Selected step indexes. Plugin of
                selected step is bold and the row is tinted.
            selected_path (StatePath): Path of the shown entries, values
                of the path are resolved from recorded states.
            selected_root (str): Root of the selected path.

        """
        self.removeRows(0, self.rowCount())
        if history is None:
            return
        # Nothing to tell apart if all steps are selected
        tint_selected = len(selected_steps) < len(history.steps)
        changed_steps = set()
        # Reads are hidden if same plugin and instance also changed it
        changing = {
            (
                history.steps[entry.step_index].plugin_id,
                history.steps[entry.step_index].instance_id,
            )
            for entry in entries
            if entry.op != "read"
        }
        phase = None
        for entry in entries:
            step = history.steps[entry.step_index]
            if entry.op == "read" and (
                step.plugin_id, step.instance_id
            ) in changing:
                continue
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

            # Always show whole value of the selected path, even if
            #   the entry changed only a child of it
            old_value = get_path_value(
                history.state_at(entry.step_index - 1),
                selected_root,
                selected_path,
            )
            if entry.op == "read":
                op = "read"
                new_value = old_value
            else:
                if entry.step_index in changed_steps:
                    # Step with more changes of the path is shown once
                    continue
                changed_steps.add(entry.step_index)
                new_value = get_path_value(
                    history.state_at(entry.step_index),
                    selected_root,
                    selected_path,
                )
                if old_value is _MISSING and new_value is _MISSING:
                    continue
                if old_value is _MISSING:
                    op = "added"
                elif new_value is _MISSING:
                    op = "removed"
                else:
                    op = "modified"

            diff_parts = None
            if op == "read":
                value_text = _format_missing(old_value)
                value_tooltip = _format_full_value(old_value)
            elif op == "removed":
                value_text = _format_missing(old_value)
                value_tooltip = "{} → {}".format(
                    _format_full_value(old_value), _format_full_value(_MISSING)
                )
            elif op == "modified":
                value_text = "{} → {}".format(
                    _format_missing(old_value),
                    _format_missing(new_value),
                )
                diff_parts = _diff_segments(old_value, new_value)
                if diff_parts is not None:
                    value_text = "".join(text for text, _ in diff_parts)
                value_tooltip = "{} → {}".format(
                    _format_full_value(old_value),
                    _format_full_value(new_value),
                )
            else:
                value_text = _format_missing(new_value)
                value_tooltip = "{} → {}".format(
                    _format_full_value(_MISSING),
                    _format_full_value(new_value),
                )

            plugin_info = plugin_infos.get(step.plugin_id or "")
            if plugin_info is not None:
                step_phase = _get_phase(plugin_info.order)
                if step_phase != phase:
                    phase = step_phase
                    self._append_divider(phase)

            texts = {
                HISTORY_OP_COLUMN: "",
                HISTORY_ORDER_COLUMN: order_text,
                HISTORY_PLUGIN_COLUMN: plugin_label,
                HISTORY_VALUE_COLUMN: value_text,
            }
            items = []
            for column in range(len(texts)):
                text = texts[column]
                item = QtGui.QStandardItem(text)
                item.setEditable(False)
                item.setToolTip(text)
                item.setData(entry.step_index, HISTORY_STEP_ROLE)
                item.setData(step.plugin_id, HISTORY_PLUGIN_ROLE)
                items.append(item)
            plugin_item = items[HISTORY_PLUGIN_COLUMN]
            op_item = items[HISTORY_OP_COLUMN]
            value_item = items[HISTORY_VALUE_COLUMN]
            if plugin_tooltip:
                plugin_item.setToolTip(plugin_tooltip)
            op_tooltip = BADGE_TOOLTIPS[op]
            if entry.path:
                op_tooltip += f": {_format_path(entry.path)}"
            op_tooltip += f"\n{value_tooltip}"
            op_item.setToolTip(op_tooltip)
            value_item.setToolTip(value_tooltip)
            op_item.setIcon(_get_badge_icon(op))

            if diff_parts is not None:
                value_item.setData([
                    [
                        text,
                        BADGE_COLORS.get(part_op, ""),
                    ]
                    for text, part_op in diff_parts
                ], HISTORY_SEGMENTS_ROLE)
            if tint_selected and entry.step_index in selected_steps:
                for item in items:
                    item.setData(SELECTED_STEP_BG, HISTORY_SELECTED_ROLE)
            if op != "read":
                value_item.setForeground(QtGui.QColor(BADGE_COLORS[op]))
            else:
                value_color = QtGui.QColor(
                    value_item.foreground().color()
                    if value_item.foreground().style()
                    else DEFAULT_VALUE_COLOR
                )
                value_color.setAlphaF(0.5)
                value_item.setForeground(value_color)
            if entry.step_index in selected_steps:
                _set_font_style(plugin_item, bold=True)
            if step.source == STEP_SOURCE_OUTSIDE:
                _set_font_style(plugin_item, italic=True)
            self.appendRow(items)

    def _append_divider(self, label: str) -> None:
        items = []
        for _ in range(self.columnCount()):
            item = QtGui.QStandardItem()
            item.setFlags(QtCore.Qt.NoItemFlags)
            item.setData(label, HISTORY_DIVIDER_ROLE)
            items.append(item)
        self.appendRow(items)

    def set_hovered_plugin(
        self, plugin_id: str | None, prior_ids: set[str] | None = None
    ) -> None:
        """Highlight rows of hovered plugin and of plugins before it."""
        prior_ids = prior_ids or set()
        for row in range(self.rowCount()):
            row_plugin_id = self.item(row, 0).data(HISTORY_PLUGIN_ROLE)
            brush = None
            if plugin_id is not None and row_plugin_id == plugin_id:
                brush = HOVER_PLUGIN_BG
            elif row_plugin_id in prior_ids:
                brush = HOVER_PRIOR_BG
            for column in range(self.columnCount()):
                self.item(row, column).setData(brush, HISTORY_HIGHLIGHT_ROLE)


class StatusLineWidget(QtWidgets.QWidget):
    """Badge counts to toggle visibility by and text filter.

    Checked badges are shown, buttons of badges without any item are
    disabled.

    Signals:
        filter_changed (): Badge or text filter changed.

    """
    filter_changed = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)

        buttons = {}
        size = STATUS_LINE_ICON_SIZE
        for op in STATUS_LINE_ORDER:
            btn = QtWidgets.QPushButton(self)
            btn.setObjectName("StateStatusButton")
            btn.setCheckable(True)
            btn.setChecked(True)
            btn.setFlat(True)
            btn.setIconSize(QtCore.QSize(size, size))
            btn.setCursor(QtCore.Qt.PointingHandCursor)
            btn.clicked.connect(self._on_clicked)
            layout.addWidget(btn, 0)
            buttons[op] = btn

        text_input = QtWidgets.QLineEdit(self)
        text_input.setPlaceholderText("Filter keys and values...")
        text_input.setClearButtonEnabled(True)
        layout.addSpacing(8)
        layout.addWidget(text_input, 1)

        text_timer = QtCore.QTimer(self)
        text_timer.setSingleShot(True)
        text_timer.setInterval(200)

        text_input.textChanged.connect(text_timer.start)
        text_timer.timeout.connect(self.filter_changed)

        self._buttons = buttons
        self._counts: dict[str, int] = {}
        self._text_input = text_input
        self.set_counts({})

    def get_filter(self) -> set[str] | None:
        """Badges to show, 'None' if all badges are shown."""
        enabled = [
            op for op, btn in self._buttons.items() if btn.isEnabled()
        ]
        shown = {op for op in enabled if self._buttons[op].isChecked()}
        if len(shown) == len(enabled):
            return None
        return shown

    def get_text(self) -> str:
        return self._text_input.text()

    def set_counts(self, counts: dict[str, int]) -> None:
        """Show counts, buttons without any count are disabled."""
        self._counts = dict(counts)
        for op in STATUS_LINE_ORDER:
            self._update_button(op)

    def _update_button(self, op: str) -> None:
        count = self._counts.get(op, 0)
        btn = self._buttons[op]
        btn.setEnabled(count > 0)
        btn.setText(str(count))
        is_on = count > 0 and btn.isChecked()
        color = (
            BADGE_COLORS.get(op, DIMMED_COLOR) if is_on else DIMMED_COLOR
        )
        btn.setIcon(_get_status_icon(op, color))
        if is_on:
            qcolor = QtGui.QColor(color)
            background = (
                f"rgba({qcolor.red()}, {qcolor.green()}, {qcolor.blue()}, 45)"
            )
            border = color
        else:
            background = "transparent"
            border = "transparent"
        btn.setStyleSheet(
            "QPushButton {"
            f" color: {color}; border: 1px solid {border};"
            " border-radius: 3px; padding: 1px 5px; text-align: left;"
            f" background: {background}; }}"
            f"QPushButton:hover:enabled {{ border-color: {color}; }}"
        )
        btn.setToolTip(
            f"{count} {STATUS_LINE_LABELS[op]}"
            + ("" if count <= 0 else ", click to show or hide")
        )

    def _on_clicked(self) -> None:
        for op in STATUS_LINE_ORDER:
            self._update_button(op)
        self.filter_changed.emit()


class DeselectableTreeView(QtWidgets.QTreeView):
    """Tree view that clears selection on click in empty space or Escape."""
    def mousePressEvent(self, event):
        if not self.indexAt(event.pos()).isValid():
            self.selectionModel().clear()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Escape:
            self.selectionModel().clear()
            event.accept()
            return
        super().keyPressEvent(event)


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

        status_line = StatusLineWidget(self)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal, self)
        splitter.setChildrenCollapsible(True)

        state_model = StateTreeModel(self)
        state_view = DeselectableTreeView(splitter)
        state_view.setObjectName("PublishStateView")
        state_view.setModel(state_model)
        state_view.setUniformRowHeights(True)
        state_view.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers
        )
        state_view.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows
        )
        value_delegate = StateValueDelegate(state_view)
        state_view.setItemDelegateForColumn(VALUE_COLUMN, value_delegate)
        state_view.header().setStretchLastSection(True)
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
        history_view.setItemDelegate(StateHistoryDelegate(history_view))
        history_view.header().setStretchLastSection(True)
        history_view.viewport().installEventFilter(self)

        splitter.addWidget(state_view)
        splitter.addWidget(history_view)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(empty_label, 1)
        layout.addWidget(status_line, 0)
        layout.addWidget(splitter, 1)

        state_view.expanded.connect(self._on_state_expanded)
        state_view.selectionModel().currentChanged.connect(
            self._on_state_current_changed
        )
        status_line.filter_changed.connect(self._apply_state_view)
        history_view.entered.connect(self._on_history_entered)
        history_view.clicked.connect(self._on_history_clicked)

        self._empty_label = empty_label
        self._status_line = status_line
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
        # Arguments of the state model of current selection
        self._view_args: tuple | None = None

        self._update_visibility()

    def set_active(self, is_active: bool) -> None:
        """Required by tabs of report viewer, nothing to load."""

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

    def set_hovered_plugin(self, plugin_id: str | None) -> None:
        """Show where plugin is in history and which ran before it."""
        prior_ids = set()
        if plugin_id is not None and self._report is not None:
            for plugin_info in self._report.plugins_info:
                if plugin_info.id == plugin_id:
                    break
                prior_ids.add(plugin_info.id)
        self._history_model.set_hovered_plugin(plugin_id, prior_ids)

    def _get_history(self) -> PublishStateHistory | None:
        if self._report is None:
            return None
        return self._report.state

    def _update_visibility(self) -> None:
        has_state = self._get_history() is not None
        self._empty_label.setVisible(not has_state)
        self._status_line.setVisible(has_state)
        self._splitter.setVisible(has_state)

    def _get_root_label(self, root: str) -> str:
        return self._instance_labels.get(root, root)

    def _refresh(self) -> None:
        history = self._get_history()
        self._selected_steps = set()
        if history is None:
            self._view_args = None
            self._status_line.set_counts({})
            self._apply_state_view()
            return

        # No selection is the same as everything selected
        step_indexes = history.get_step_indexes(
            self._plugin_ids or None, self._instance_ids or None
        )
        self._selected_steps = set(step_indexes)
        if step_indexes:
            state = history.state_at(step_indexes[-1])
            badges = history.changes_for(step_indexes)
        else:
            state = history.final_state()
            badges = None

        steps_roots = set()
        for step_index in step_indexes:
            step = history.steps[step_index]
            steps_roots.add(step.instance_id)
            steps_roots |= step.get_roots()

        scope = None
        if self._instance_ids:
            scope = set(self._instance_ids)
            # Keep root of selected path if selected steps touched it
            if (
                self._selected_path is not None
                and self._selected_path[0] in steps_roots
            ):
                scope.add(self._selected_path[0])
            scope.add(CONTEXT_ID)

        root_ids = [CONTEXT_ID]
        known = set(state) | set(history.initial_state)
        if badges is not None:
            known |= {root for root, _ in badges.badges}
        root_ids.extend(
            root
            for root in self._get_root_order(history, state)
            if (scope is None or root in scope)
            and root in known
            and root != CONTEXT_ID
        )

        counts = {}
        if badges is not None:
            for ops in badges.badges.values():
                for op in ops:
                    counts[op] = counts.get(op, 0) + 1
        roots = [(root, self._get_root_label(root)) for root in root_ids]
        counts["unchanged"] = self._count_unchanged(roots, state, badges)
        self._status_line.set_counts(counts)

        self._view_args = (roots, state, badges)
        self._apply_state_view()

    @staticmethod
    def _count_unchanged(
        roots: list[tuple[str, str]],
        state: dict[str, Any],
        badges: StateBadges | None,
    ) -> int:
        """Count items of state that have no change in them or below."""
        touched = set()
        if badges is not None:
            for root, path in badges.badges:
                for idx in range(len(path) + 1):
                    touched.add((root, path[:idx]))
        count = 0
        stack = [
            (root, (), state[root]) for root, _ in roots if root in state
        ]
        while stack:
            root, path, value = stack.pop()
            if (root, path) not in touched:
                count += 1
            if isinstance(value, dict):
                children = value.items()
            elif isinstance(value, list):
                children = enumerate(value)
            else:
                continue
            for key, child in children:
                stack.append((root, path + (key,), child))
        return count

    def _apply_state_view(self) -> None:
        roots, state, badges = self._view_args or ([], {}, None)
        # Removing rows would reset selected path
        selection_model = self._state_view.selectionModel()
        selection_model.blockSignals(True)
        try:
            self._state_model.set_state(
                roots,
                state,
                badges,
                self._status_line.get_filter(),
                self._status_line.get_text(),
            )
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
        root = ""
        path = ()
        if history is not None and self._selected_path is not None:
            root, path = self._selected_path
            entries = history.history_for(root, path)
        self._history_model.set_history(
            history,
            entries,
            self._plugin_infos,
            self._instance_labels,
            self._selected_steps,
            path or (),
            root,
        )
        for column in range(self._history_model.columnCount() - 1):
            self._history_view.resizeColumnToContents(column)
        for row in range(self._history_model.rowCount()):
            if self._history_model.item(row, 0).data(
                HISTORY_DIVIDER_ROLE
            ) is not None:
                self._history_view.setFirstColumnSpanned(
                    row, QtCore.QModelIndex(), True
                )

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
