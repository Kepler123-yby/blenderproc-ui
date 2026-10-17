from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from bproc_ui.widgets import RangeEditor, integer, number


def test_numeric_editors_ignore_the_wheel_until_focused(qtbot):
    range_editor = RangeEditor()
    editors = [number(), integer(), range_editor.minimum]
    for editor in editors:
        qtbot.addWidget(editor)
        # StrongFocus keeps wheel events away until the editor is clicked or tabbed into.
        assert editor.focusPolicy() == Qt.StrongFocus


def test_wheel_over_an_unfocused_editor_leaves_the_value_alone(qtbot):
    editor = number(0.3436, 0.0, 5.0, decimals=4)
    qtbot.addWidget(editor)
    editor.show()
    before = editor.value()
    event = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, 120),
                        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    assert not editor.hasFocus()
    # Dispatch through QApplication so Qt's focus-policy filter is exercised, not bypassed.
    QApplication.sendEvent(editor, event)
    assert editor.value() == before
