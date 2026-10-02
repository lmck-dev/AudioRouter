"""The effect chain for one channel: what is in it, in what order, set how.

Every edit is reported the moment it happens, so nothing typed can be lost, and
in two kinds. `changed` means the chain itself changed (an effect added, removed or
moved), which restarts the channel - seamlessly, the new host taking over
before the old one stops. `tuned` means a knob moved or an effect was switched
on or off, which the engine applies to the running channel in place. The window owns the waiting for both.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSlider,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..channels import Channel
from ..effects import (
    Effect,
    EffectError,
    EffectSpec,
    ParamSpec,
    all_specs,
    db_to_linear,
    linear_to_db,
    make_effect,
    plugin_specs,
)
from .theme import Theme

#: Lowest level a gain knob shows before it reads as "off".
DB_FLOOR = -80.0
SLIDER_STEPS = 1000


def format_value(param: ParamSpec, value: float) -> str:
    """A setting as a person reads it: named choice, on/off, or number and unit."""
    if param.choices:
        for label, choice in param.choices:
            if choice == value:
                return label
    if param.toggled:
        return "on" if value >= 0.5 else "off"
    if param.db:
        if value <= 0 or linear_to_db(value) <= DB_FLOOR:
            return "-inf dB"
        value = linear_to_db(value)
    unit = f" {param.unit}" if param.unit else ""
    return f"{value:.3g}{unit}"


class ParamControl(QWidget):
    """One knob: a switch, a list, or a slider with a number box.

    Values in and out are always the plugin's own; only what is *shown* is
    converted (a linear gain appears in dB), so nothing is lost to rounding
    through the display.
    """

    edited = pyqtSignal()

    def __init__(self, param: ParamSpec, value: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.param = param
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.check: QCheckBox | None = None
        self.combo: QComboBox | None = None
        self.slider: QSlider | None = None
        self.spin: QDoubleSpinBox | None = None
        if param.comment:
            self.setToolTip(param.comment)

        if param.choices:
            self.combo = QComboBox(self)
            for label, choice in param.choices:
                self.combo.addItem(label, choice)
            self.combo.currentIndexChanged.connect(self._from_widget)
            layout.addWidget(self.combo)
            layout.addStretch(1)
        elif param.toggled:
            self.check = QCheckBox(self)
            self.check.toggled.connect(self._from_widget)
            layout.addWidget(self.check)
            layout.addStretch(1)
        else:
            low, high = self._display_range()
            self.slider = QSlider(Qt.Orientation.Horizontal, self)
            self.slider.setRange(0, SLIDER_STEPS)
            self.spin = QDoubleSpinBox(self)
            self.spin.setRange(low, high)
            self.spin.setDecimals(self._decimals())
            self.spin.setSingleStep(param.step if not param.db else 0.5)
            self.spin.setKeyboardTracking(False)
            if param.unit:
                self.spin.setSuffix(f" {param.unit}")
            if param.db and param.minimum <= 0:
                self.spin.setSpecialValueText("-inf dB")
            self.spin.setMinimumWidth(110)
            self.slider.valueChanged.connect(self._from_slider)
            self.spin.valueChanged.connect(self._from_widget)
            layout.addWidget(self.slider, 1)
            layout.addWidget(self.spin)
        self.setValue(value)

    # -- display mapping ----------------------------------------------------

    def _display_range(self) -> tuple[float, float]:
        p = self.param
        if p.db:
            low = DB_FLOOR if p.minimum <= 0 else max(linear_to_db(p.minimum), DB_FLOOR)
            return low, linear_to_db(p.maximum)
        return p.minimum, p.maximum

    def _decimals(self) -> int:
        p = self.param
        if p.integer:
            return 0
        if p.db:
            return 1
        return max(0, min(6, math.ceil(-math.log10(p.step)))) if p.step < 1 else 0

    def _to_display(self, value: float) -> float:
        if self.param.db:
            low, _ = self._display_range()
            return max(low, linear_to_db(value)) if value > 0 else low
        return value

    def _from_display(self, shown: float) -> float:
        p = self.param
        if p.db:
            low, _ = self._display_range()
            if shown <= low and p.minimum <= 0:
                return p.minimum
            return p.clamp(db_to_linear(shown))
        return p.clamp(shown)

    def _position(self, shown: float) -> int:
        low, high = self._display_range()
        if high <= low:
            return 0
        if self.param.logarithmic and not self.param.db:
            if low > 0:
                t = math.log(shown / low) / math.log(high / low)
            else:
                t = math.log1p(shown - low) / math.log1p(high - low)
        else:
            t = (shown - low) / (high - low)
        return round(max(0.0, min(1.0, t)) * SLIDER_STEPS)

    def _shown_at(self, position: int) -> float:
        low, high = self._display_range()
        t = position / SLIDER_STEPS
        if self.param.logarithmic and not self.param.db:
            if low > 0:
                return low * (high / low) ** t
            return low + math.expm1(t * math.log1p(high - low))
        return low + (high - low) * t

    # -- value --------------------------------------------------------------

    def value(self) -> float:
        if self.combo is not None:
            return float(self.combo.currentData())
        if self.check is not None:
            return self.param.maximum if self.check.isChecked() else self.param.minimum
        assert self.spin is not None
        return self._from_display(self.spin.value())

    def setValue(self, value: float) -> None:
        widgets = [w for w in (self.combo, self.check, self.slider, self.spin) if w is not None]
        for widget in widgets:
            widget.blockSignals(True)
        try:
            if self.combo is not None:
                index = self.combo.findData(float(value))
                self.combo.setCurrentIndex(max(index, 0))
            elif self.check is not None:
                self.check.setChecked(value >= (self.param.minimum + self.param.maximum) / 2)
            else:
                assert self.spin is not None and self.slider is not None
                shown = self._to_display(value)
                self.spin.setValue(shown)
                self.slider.setValue(self._position(shown))
        finally:
            for widget in widgets:
                widget.blockSignals(False)

    def _from_slider(self, position: int) -> None:
        assert self.spin is not None
        self.spin.blockSignals(True)
        self.spin.setValue(self._shown_at(position))
        self.spin.blockSignals(False)
        self.edited.emit()

    def _from_widget(self) -> None:
        if self.spin is not None and self.slider is not None:
            self.slider.blockSignals(True)
            self.slider.setValue(self._position(self.spin.value()))
            self.slider.blockSignals(False)
        self.edited.emit()


class ParameterForm(QWidget):
    """One row per knob, in the effect's own units."""

    edited = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QFormLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        # Inside a scroll area the form must never be squeezed below the height
        # its rows need: without this a 30-knob plugin was crushed into a
        # stack of overlapping few-pixel rows instead of scrolling.
        self._layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        self._boxes: dict[str, ParamControl] = {}
        self._effect: Effect | None = None

    def show_effect(self, effect: Effect | None) -> None:
        self._effect = None  # stop edits firing while the form is rebuilt
        # removeRow destroys the widgets there and then. deleteLater only
        # schedules it, and the orphaned labels stay painted where they were,
        # so the previous effect's knobs show through underneath the new ones.
        while self._layout.rowCount():
            self._layout.removeRow(0)
        self._boxes.clear()
        if effect is None:
            return
        try:
            spec = effect.spec
            values = effect.resolved()
        except EffectError as exc:
            self._layout.addRow(QLabel(str(exc), self))
            return
        for param in spec.visible_params():
            control = ParamControl(param, values[param.key], self)
            control.edited.connect(self._changed)
            self._boxes[param.key] = control
            label = QLabel(param.label, self)
            if param.comment:
                label.setToolTip(param.comment)
            self._layout.addRow(label, control)
        if not self._boxes:
            self._layout.addRow(QLabel("This effect has no settings.", self))
        self._effect = effect

    def _changed(self) -> None:
        if self._effect is None:
            return
        self._effect.params = self._effect.spec.normalise(
            {key: box.value() for key, box in self._boxes.items()}
        )
        self.edited.emit()


class EffectBrowser(QDialog):
    """Pick an effect: the built-in ones first, then every installed plugin."""

    def __init__(
        self,
        parent: QWidget | None = None,
        edit_folders: Callable[[QWidget], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add effect")
        #: Opens the plugin folders; True when they changed and the list must
        #: be read again.
        self.edit_folders = edit_folders
        self.resize(560, 560)
        self.choice: tuple[str, str] | None = None

        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search effects - e.g. compressor, reverb, eq")
        self.search.setClearButtonEnabled(True)
        self.show_unusable = QCheckBox("Also show plugins that cannot be used here", self)
        self.tree = QTreeWidget(self)
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Effect", "Maker"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        # Sized by the header, not by contents: the plugin groups start
        # collapsed, so measuring them then cut every plugin name short.
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.detail = QLabel(self)
        self.detail.setWordWrap(True)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add")
        self.folders_button = QPushButton("Plugin folders...", self)
        self.folders_button.setToolTip("Add folders of LV2 or LADSPA plugins, such as your DAW's")
        self.folders_button.setVisible(edit_folders is not None)
        self.folders_button.clicked.connect(self._edit_folders)

        options = QHBoxLayout()
        options.addWidget(self.show_unusable, 1)
        options.addWidget(self.folders_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.search)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.detail)
        layout.addLayout(options)
        layout.addWidget(self.buttons)

        self.search.textChanged.connect(self._filter)
        self.show_unusable.toggled.connect(self._populate)
        self.tree.currentItemChanged.connect(self._selected)
        self.tree.itemDoubleClicked.connect(lambda item, _: self._accept_item(item))
        self.buttons.accepted.connect(lambda: self._accept_item(self.tree.currentItem()))
        self.buttons.rejected.connect(self.reject)

        self._populate()
        self.search.setFocus()

    def _edit_folders(self) -> None:
        if self.edit_folders is None or not self.edit_folders(self):
            return
        # New folders mean reading every new plugin once, which takes a moment.
        QGuiApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        try:
            self._populate()
        finally:
            QGuiApplication.restoreOverrideCursor()

    def _populate(self) -> None:
        """One group per kind of effect; built-in effects first within each."""
        from collections import Counter

        from ..lv2 import CATEGORIES

        self.tree.clear()
        specs: list[EffectSpec] = list(all_specs())
        try:
            specs += plugin_specs(include_unusable=self.show_unusable.isChecked())
        except Exception as exc:  # noqa: BLE001 - a broken catalogue must not hide the built-ins
            self.detail.setText(f"Could not read the installed plugins: {exc}")
        # Mono and stereo builds often share a name (RNNoise does); say which.
        repeated = Counter(spec.label for spec in specs)
        groups: dict[str, QTreeWidgetItem] = {}
        for category in CATEGORIES:
            group = QTreeWidgetItem(self.tree, [category])
            group.setFlags(Qt.ItemFlag.ItemIsEnabled)
            group.setFirstColumnSpanned(True)
            groups[category] = group
        dim = Theme(self).dim
        for spec in specs:
            group = groups.get(spec.category, groups["Utility"])
            label = spec.label
            if repeated[label] > 1 and spec.plugin:
                layout = "stereo" if "(stereo)" in spec.summary else "mono"
                label = f"{label} ({layout})"
            item = QTreeWidgetItem(group, [label, spec.group])
            item.setData(0, Qt.ItemDataRole.UserRole, (spec.kind, spec.plugin))
            problems = spec.unsatisfied()
            item.setData(0, Qt.ItemDataRole.UserRole + 1, spec.summary)
            if problems:
                reason = "\n".join(p.explain() for p in problems)
                item.setData(0, Qt.ItemDataRole.UserRole + 1, f"{spec.summary}\nCannot be used: {reason}")
                item.setForeground(0, dim)
                item.setForeground(1, dim)
                item.setToolTip(0, reason)
                item.setData(0, Qt.ItemDataRole.UserRole + 2, False)
            else:
                item.setData(0, Qt.ItemDataRole.UserRole + 2, True)
        for group in groups.values():
            group.setText(0, f"{group.text(0)}  ({group.childCount()})")
        self._filter(self.search.text())

    def _items(self):
        for g in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(g)
            for i in range(group.childCount()):
                yield group, group.child(i)

    def _filter(self, text: str) -> None:
        """Show effects matching every word typed.

        An effect's own name, maker and description are searched first; the
        kind of effect only counts when nothing matches by itself. Otherwise
        "noise" matched the whole "Noise & gates" group and buried RNNoise
        among 44 gates and expanders.
        """
        words = text.lower().split()
        items = list(self._items())

        def own(item: QTreeWidgetItem) -> str:
            return " ".join(
                [item.text(0), item.text(1), str(item.data(0, Qt.ItemDataRole.UserRole + 1))]
            ).lower()

        hits = {id(item) for _, item in items if all(w in own(item) for w in words)}
        if words and not hits:
            hits = {
                id(item) for group, item in items
                if all(w in f"{own(item)} {group.text(0).lower()}" for w in words)
            }
        visible_groups: set[int] = set()
        first: QTreeWidgetItem | None = None
        for group, item in items:
            hit = not words or id(item) in hits
            item.setHidden(not hit)
            if hit:
                visible_groups.add(id(group))
                if first is None and item.data(0, Qt.ItemDataRole.UserRole + 2):
                    first = item
        for g in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(g)
            group.setHidden(id(group) not in visible_groups)
            group.setExpanded(bool(words))
        if words and first is not None:
            self.tree.setCurrentItem(first)

    def _selected(self, item: QTreeWidgetItem | None) -> None:
        usable = bool(item is not None and item.data(0, Qt.ItemDataRole.UserRole + 2))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(usable)
        self.detail.setText(str(item.data(0, Qt.ItemDataRole.UserRole + 1) or "") if item else "")

    def _accept_item(self, item: QTreeWidgetItem | None) -> None:
        if item is None or not item.data(0, Qt.ItemDataRole.UserRole + 2):
            return
        kind, plugin = item.data(0, Qt.ItemDataRole.UserRole)
        self.choice = (kind, plugin)
        self.accept()


class PluginFoldersDialog(QDialog):
    """The user's own plugin folders, beside the places plugins usually live."""

    def __init__(self, folders: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Plugin folders")
        self.resize(520, 340)
        intro = QLabel(
            "Audio Router finds LV2 and LADSPA plugins in the usual places on "
            "this computer. Add any other folder that holds them - your music "
            "software's plugin folder, say - and the folders inside it are "
            "searched too. VST plugins cannot be used.",
            self,
        )
        intro.setWordWrap(True)
        self.list = QListWidget(self)
        for folder in folders:
            QListWidgetItem(folder, self.list)
        self.add_button = QPushButton("Add folder...", self)
        self.add_button.clicked.connect(self._add)
        self.remove_button = QPushButton("Remove", self)
        self.remove_button.clicked.connect(self._remove)
        self.list.currentRowChanged.connect(lambda row: self.remove_button.setEnabled(row >= 0))
        self.remove_button.setEnabled(False)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        row = QHBoxLayout()
        row.addWidget(self.add_button)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.list, 1)
        layout.addLayout(row)
        layout.addWidget(buttons)

    def folders(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def add_folder(self, folder: str) -> None:
        if folder and folder not in self.folders():
            QListWidgetItem(folder, self.list)

    def _add(self) -> None:
        from PyQt6.QtWidgets import QFileDialog

        self.add_folder(QFileDialog.getExistingDirectory(self, "Choose a plugin folder"))

    def _remove(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)


class EffectsPanel(QGroupBox):
    """Add, order, switch off and adjust the effects on one channel."""

    #: The chain changed shape: the channel must restart.
    changed = pyqtSignal()
    #: Only settings changed: they can be applied to the running channel.
    tuned = pyqtSignal()
    #: The highlighted effect's position in the chain, or -1 for none.
    highlighted = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Effects", parent)
        self.channel: Channel | None = None
        #: Set by the window: opens the plugin folders, True if they changed.
        self.edit_plugin_folders: Callable[[QWidget], bool] | None = None

        self.list = QListWidget(self)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.currentRowChanged.connect(self._selection_changed)
        self.list.itemChanged.connect(self._item_toggled)

        self.add_button = QPushButton("Add effect...", self)
        self.add_button.clicked.connect(self._add)
        self.remove_button = QPushButton("Remove", self)
        self.remove_button.clicked.connect(self._remove)
        self.up_button = QPushButton("Up", self)
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button = QPushButton("Down", self)
        self.down_button.clicked.connect(lambda: self._move(1))
        self.reset_button = QPushButton("Reset settings", self)
        self.reset_button.clicked.connect(self._reset)

        self.title = QLabel(self)
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        self.form = ParameterForm(self)
        self.form.edited.connect(self._parameter_edited)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidget(self.form)

        buttons = QHBoxLayout()
        buttons.addWidget(self.up_button)
        buttons.addWidget(self.down_button)
        buttons.addWidget(self.remove_button)

        chain = QVBoxLayout()
        chain.addWidget(self.add_button)
        chain.addWidget(self.list, 1)
        chain.addLayout(buttons)

        heading = QHBoxLayout()
        heading.addWidget(self.title, 1)
        heading.addWidget(self.reset_button)

        settings = QVBoxLayout()
        settings.addLayout(heading)
        settings.addWidget(self.summary)
        settings.addWidget(self.scroll, 1)

        layout = QHBoxLayout(self)
        layout.addLayout(chain, 2)
        layout.addLayout(settings, 3)

        self.set_channel(None)

    # -- population --------------------------------------------------------

    def set_channel(self, channel: Channel | None) -> None:
        # The window re-selects the same channel after every refresh, and a
        # live knob change causes a refresh. Rebuilding the form then would
        # destroy the slider under the user's mouse halfway through a drag.
        if channel is self.channel and channel is not None:
            self._refresh_labels()
            return
        self.channel = channel
        self.setEnabled(channel is not None)
        self.refresh()

    def refresh(self) -> None:
        row = self.list.currentRow()
        self.list.blockSignals(True)
        self.list.clear()
        if self.channel is not None:
            for effect in self.channel.effects:
                item = QListWidgetItem(self._describe(effect), self.list)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if effect.enabled else Qt.CheckState.Unchecked
                )
        self.list.blockSignals(False)
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)
        elif self.list.count():
            self.list.setCurrentRow(0)
        else:
            self._selection_changed(-1)
        self._update_buttons()

    def _refresh_labels(self) -> None:
        if self.channel is None or self.list.count() != len(self.channel.effects):
            self.refresh()
            return
        self.list.blockSignals(True)
        for row, effect in enumerate(self.channel.effects):
            self.list.item(row).setText(self._describe(effect))
        self.list.blockSignals(False)

    def _describe(self, effect: Effect) -> str:
        try:
            spec = effect.spec
            values = effect.resolved()
        except EffectError:
            return f"{effect.kind} (cannot be loaded)"
        problems = spec.unsatisfied()
        if problems:
            return f"{spec.label} (unavailable)"
        if spec.plugin:
            return spec.label
        headline = spec.params[0] if spec.params else None
        if headline is None:
            return spec.label
        return f"{spec.label} - {headline.label.lower()} {format_value(headline, values[headline.key])}"

    # -- editing -----------------------------------------------------------

    def _current(self) -> Effect | None:
        row = self.list.currentRow()
        if self.channel is None or not 0 <= row < len(self.channel.effects):
            return None
        return self.channel.effects[row]

    def _selection_changed(self, row: int) -> None:
        effect = self._current()
        self.highlighted.emit(row if effect is not None else -1)
        self.form.show_effect(effect)
        dim = Theme(self).dim
        if effect is None:
            self.title.setText("")
            self.summary.setText("Add an effect to change how this channel sounds."
                                 if self.channel is not None else "")
        else:
            try:
                spec = effect.spec
                problems = spec.unsatisfied()
                self.title.setText(spec.label)
                text = spec.summary
                if problems:
                    text += "\nCannot run here: " + "; ".join(p.explain() for p in problems)
                self.summary.setText(text)
            except EffectError as exc:
                self.title.setText(effect.kind)
                self.summary.setText(str(exc))
        self.summary.setStyleSheet(f"color: {dim.name()};")
        self._update_buttons()

    def _update_buttons(self) -> None:
        row = self.list.currentRow()
        count = self.list.count()
        self.remove_button.setEnabled(row >= 0)
        self.reset_button.setEnabled(row >= 0 and bool(self.form._boxes))
        self.up_button.setEnabled(row > 0)
        self.down_button.setEnabled(0 <= row < count - 1)

    def _add(self) -> None:
        self.choose_effect()

    def choose_effect(self) -> None:
        """Open the effect browser and append the chosen effect (also from the mixer)."""
        if self.channel is None:
            return
        # Reading every plugin description takes ~5 s when the cache is stale
        # (after installing or updating plugins); a tenth of a second otherwise.
        QGuiApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        try:
            browser = EffectBrowser(self, edit_folders=self.edit_plugin_folders)
        finally:
            QGuiApplication.restoreOverrideCursor()
        if browser.exec() == QDialog.DialogCode.Accepted and browser.choice is not None:
            self.add_effect(*browser.choice)

    def add_effect(self, kind: str, plugin: str = "") -> None:
        if self.channel is None:
            return
        self.channel.effects.append(make_effect(kind, plugin=plugin))
        self.refresh()
        self.list.setCurrentRow(len(self.channel.effects) - 1)
        self.changed.emit()

    def _remove(self) -> None:
        effect = self._current()
        if effect is None or self.channel is None:
            return
        self.channel.effects.remove(effect)
        self.refresh()
        self.changed.emit()

    def _move(self, delta: int) -> None:
        row = self.list.currentRow()
        if self.channel is None:
            return
        target = row + delta
        if not (0 <= row < len(self.channel.effects) and 0 <= target < len(self.channel.effects)):
            return
        effects = self.channel.effects
        effects[row], effects[target] = effects[target], effects[row]
        self.refresh()
        self.list.setCurrentRow(target)
        self.changed.emit()

    def _reset(self) -> None:
        effect = self._current()
        if effect is None:
            return
        effect.params = effect.spec.defaults()
        self.form.show_effect(effect)
        self._parameter_edited()

    def _parameter_edited(self) -> None:
        row = self.list.currentRow()
        effect = self._current()
        if effect is not None and 0 <= row < self.list.count():
            # Keep the summary in the list honest while the knob moves.
            self.list.blockSignals(True)
            self.list.item(row).setText(self._describe(effect))
            self.list.blockSignals(False)
        self.tuned.emit()

    def _item_toggled(self, item: QListWidgetItem) -> None:
        if self.channel is None:
            return
        row = self.list.row(item)
        if 0 <= row < len(self.channel.effects):
            effect = self.channel.effects[row]
            effect.enabled = item.checkState() == Qt.CheckState.Checked
            # Every effect sits behind a bypass switch, so on/off is a live
            # change - unless the effect cannot run here, in which case it is
            # left out of the graph and switching it changes the graph's shape.
            if effect.spec.unsatisfied():
                self.changed.emit()
            else:
                self.tuned.emit()
