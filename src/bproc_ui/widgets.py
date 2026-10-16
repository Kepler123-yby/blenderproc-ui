"""Small reusable numeric and editor widgets."""

from PySide6.QtCore import Qt, Signal, QRegularExpression
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
                               QLabel, QDoubleSpinBox, QSpinBox, QGroupBox,
                               QScrollArea, QFrame, QLineEdit, QSizePolicy)

from bproc_core.config import Range


def number(value=0.0, minimum=-1e6, maximum=1e6, decimals=4, suffix=""):
    widget = QDoubleSpinBox()
    widget.setRange(minimum, maximum)
    widget.setDecimals(decimals)
    widget.setSingleStep(0.01 if decimals >= 3 else 1.0)
    widget.setValue(value)
    widget.setSuffix(suffix)
    widget.setKeyboardTracking(False)
    widget.setMinimumWidth(76)
    widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
    return widget


def integer(value=0, minimum=0, maximum=10000000):
    widget = QSpinBox()
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    widget.setKeyboardTracking(False)
    return widget


class RangeEditor(QWidget):
    changed = Signal()

    def __init__(self, minimum=-1e6, maximum=1e6, decimals=8, suffix="", parent=None):
        super().__init__(parent)
        self._value = Range(0., 0.)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.minimum = number(minimum=minimum, maximum=maximum, decimals=decimals, suffix=suffix)
        self.maximum = number(minimum=minimum, maximum=maximum, decimals=decimals, suffix=suffix)
        layout.addWidget(self.minimum, 1)
        middle = QLabel("—")
        middle.setObjectName("muted")
        middle.setFixedWidth(12)
        middle.setAlignment(Qt.AlignCenter)
        layout.addWidget(middle)
        layout.addWidget(self.maximum, 1)
        self.minimum.valueChanged.connect(self._minimum_changed)
        self.maximum.valueChanged.connect(self._maximum_changed)

    def value(self):
        return self._value

    def _minimum_changed(self, value):
        self._value = Range(value, self._value.maximum)
        self.changed.emit()

    def _maximum_changed(self, value):
        self._value = Range(self._value.minimum, value)
        self.changed.emit()

    def set_value(self, value):
        self._value = value
        a, b = self.minimum.blockSignals(True), self.maximum.blockSignals(True)
        self.minimum.setValue(value.minimum)
        self.maximum.setValue(value.maximum)
        self.minimum.blockSignals(a)
        self.maximum.blockSignals(b)


class SeedEditor(QLineEdit):
    """A nonnegative Python integer without Qt's 32-bit spin box limit."""
    valueChanged = Signal(object)

    def __init__(self, value=0, parent=None):
        super().__init__(parent)
        self._value = int(value)
        self.setText(str(self._value))
        self.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9]+"), self))
        self.editingFinished.connect(self._commit)

    def value(self):
        return self._value

    def setValue(self, value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("种子必须是非负整数")
        changed = value != self._value
        self._value = value
        self.setText(str(value))
        if changed:
            self.valueChanged.emit(value)

    def _commit(self):
        if self.hasAcceptableInput():
            self.setValue(int(self.text()))
        else:
            self.setText(str(self._value))


def section(title, parent_layout):
    box = QGroupBox(title)
    form = QFormLayout(box)
    form.setContentsMargins(6, 8, 6, 6)
    form.setHorizontalSpacing(8)
    form.setVerticalSpacing(5)
    form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
    form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    parent_layout.addWidget(box)
    return form


def note(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setObjectName("muted")
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


def row(*widgets):
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    for child in widgets:
        if isinstance(child, QLabel) and len(child.text()) <= 2:
            child.setFixedWidth(14)
            child.setAlignment(Qt.AlignCenter)
        layout.addWidget(child)
    return widget


def scrollable(widget):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setWidget(widget)
    return scroll


def editor_header(title, *widgets):
    frame = QFrame()
    frame.setObjectName("editorHeader")
    layout = QHBoxLayout(frame)
    layout.setContentsMargins(9, 4, 6, 4)
    layout.setSpacing(8)
    label = QLabel(title)
    label.setObjectName("editorTitle")
    layout.addWidget(label)
    layout.addStretch()
    for widget in widgets:
        layout.addWidget(widget)
    return frame
