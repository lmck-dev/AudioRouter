"""The effect chain for one channel: what is in it, in what order, set how.

Every edit is reported the moment it happens, so nothing typed can be lost.
Changing an effect means restarting the channel's host process, which is far too
heavy to do on every tick of a spin box, but the waiting belongs to whoever owns
the restart - the window - not to two timers in series.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..channels import Channel
from ..effects import Effect, EffectSpec, all_specs, spec_for
from .theme import Theme

class ParameterForm(QWidget):
    """One row per knob, in the effect's own units."""

    edited = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QFormLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._boxes: dict[str, QDoubleSpinBox] = {}
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
        spec = effect.spec
        values = effect.resolved()
        for param in spec.params:
            box = QDoubleSpinBox(self)
            box.setRange(param.minimum, param.maximum)
            box.setSingleStep(param.step)
            box.setDecimals(0 if param.step >= 1 else 2)
            box.setValue(values[param.key])
            if param.unit:
                box.setSuffix(f" {param.unit}")
            box.valueChanged.connect(self._changed)
            self._boxes[param.key] = box
            self._layout.addRow(param.label, box)
        self._effect = effect

    def _changed(self) -> None:
        if self._effect is None:
            return
        self._effect.params = self._effect.spec.normalise(
            {key: box.value() for key, box in self._boxes.items()}
        )
        self.edited.emit()


class EffectsPanel(QGroupBox):
    """Add, order, switch off and adjust the effects on one channel."""

    changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Effects", parent)
        self.channel: Channel | None = None

        self.list = QListWidget(self)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.currentRowChanged.connect(self._selection_changed)
        self.list.itemChanged.connect(self._item_toggled)

        self.picker = QComboBox(self)
        self.add_button = QPushButton("Add", self)
        self.add_button.clicked.connect(self._add)
        self.remove_button = QPushButton("Remove", self)
        self.remove_button.clicked.connect(self._remove)
        self.up_button = QPushButton("Move up", self)
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button = QPushButton("Move down", self)
        self.down_button.clicked.connect(lambda: self._move(1))

        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        self.form = ParameterForm(self)

        self.form.edited.connect(self._parameter_edited)

        add_row = QHBoxLayout()
        add_row.addWidget(self.picker, 1)
        add_row.addWidget(self.add_button)

        buttons = QHBoxLayout()
        buttons.addWidget(self.up_button)
        buttons.addWidget(self.down_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(add_row)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.summary)
        layout.addWidget(self.form)

        self._fill_picker()
        self.set_channel(None)
        self._select_first_available()

    # -- population --------------------------------------------------------

    def _fill_picker(self) -> None:
        self.picker.clear()
        for spec in all_specs():
            label = spec.label if spec.available else f"{spec.label} (needs a plugin)"
            self.picker.addItem(label, spec.kind)
            index = self.picker.count() - 1
            if not spec.available:
                self.picker.setItemData(
                    index, "\n".join(r.explain() for r in spec.unsatisfied()),
                    Qt.ItemDataRole.ToolTipRole,
                )

    def _select_first_available(self) -> None:
        """Default the Add menu to something this machine can actually run."""
        for index in range(self.picker.count()):
            if spec_for(self.picker.itemData(index)).available:
                self.picker.setCurrentIndex(index)
                return

    def set_channel(self, channel: Channel | None) -> None:
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

    def _describe(self, effect: Effect) -> str:
        spec = effect.spec
        values = effect.resolved()
        headline = spec.params[0] if spec.params else None
        if headline is None:
            return spec.label
        value = values[headline.key]
        unit = f" {headline.unit}" if headline.unit else ""
        return f"{spec.label} - {headline.label.lower()} {value:g}{unit}"

    # -- editing -----------------------------------------------------------

    def _current(self) -> Effect | None:
        row = self.list.currentRow()
        if self.channel is None or not 0 <= row < len(self.channel.effects):
            return None
        return self.channel.effects[row]

    def _selection_changed(self, row: int) -> None:
        effect = self._current()
        self.form.show_effect(effect)
        self.summary.setText(effect.spec.summary if effect else "")
        colour = Theme(self).dim
        self.summary.setStyleSheet(f"color: {colour.name()};")
        self._update_buttons()

    def _update_buttons(self) -> None:
        row = self.list.currentRow()
        count = self.list.count()
        self.remove_button.setEnabled(row >= 0)
        self.up_button.setEnabled(row > 0)
        self.down_button.setEnabled(0 <= row < count - 1)

    def _add(self) -> None:
        if self.channel is None:
            return
        kind = self.picker.currentData()
        self.channel.effects.append(Effect(kind=kind, params=spec_for(kind).defaults()))
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

    def _parameter_edited(self) -> None:
        row = self.list.currentRow()
        effect = self._current()
        if effect is not None and 0 <= row < self.list.count():
            # Keep the summary in the list honest while the knob moves.
            self.list.blockSignals(True)
            self.list.item(row).setText(self._describe(effect))
            self.list.blockSignals(False)
        self.changed.emit()

    def _item_toggled(self, item: QListWidgetItem) -> None:
        if self.channel is None:
            return
        row = self.list.row(item)
        if 0 <= row < len(self.channel.effects):
            self.channel.effects[row].enabled = item.checkState() == Qt.CheckState.Checked
            self.changed.emit()
