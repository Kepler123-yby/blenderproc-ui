"""Blender-inspired visual system, reviewed against the approved desktop brief.

The scene, object tree, properties and sample strip are editors, not cards.
Graphite chrome (#292929), panel grey (#3d3d3d), numeric fields (#545454),
readable foreground (#dedede), orange selection (#f99c33) and blue activation
(#477ba8) follow Blender's visual vocabulary. Noto Sans CJK SC at 11 px keeps
Chinese labels legible in a compact workspace. The one visual focus is the real
mesh viewport; typography and dividers quietly explain its controls.

Review: an initial generic tab-and-card dashboard would waste space and obscure
the relationship between an edited object and the camera anchor. Split editors,
an explicit anchor badge and a floating camera view directly serve this brief.
"""

from PySide6.QtGui import QColor, QFont, QPalette


BASE = "#292929"
PANEL = "#3d3d3d"
FIELD = "#545454"
TEXT = "#dedede"
ORANGE = "#f99c33"
BLUE = "#477ba8"

STYLESHEET = """
QWidget { color: #dedede; font-size: 11px; }
QMainWindow, QDialog { background: #303030; }
QMenuBar, QMenu, QStatusBar { background: #292929; }
QMenuBar::item { padding: 5px 9px; }
QMenuBar::item:selected, QMenu::item:selected { background: #477ba8; }
QMenu::item { padding: 6px 22px; }
QFrame#editorHeader { background: #303030; border-bottom: 1px solid #242424; }
QLabel#editorTitle { color: #ededed; font-weight: 600; }
QLabel#muted, QLabel[muted="true"] { color: #adadad; }
QLabel#anchorBadge { color: #f99c33; }
QLabel#errorLabel { color: #f6b18c; }
QLabel#successLabel { color: #a6cda5; }
QLabel#emptyHint { color: #a7a7a7; font-size: 13px; }
QPushButton, QToolButton { background: #505050; border: 1px solid #272727;
  border-radius: 3px; padding: 5px 8px; min-height: 16px; }
QPushButton:hover, QToolButton:hover { background: #606060; }
QPushButton:pressed, QToolButton:pressed { background: #3d3d3d; }
QPushButton:disabled, QToolButton:disabled { color: #888888; background: #414141; }
QPushButton#primaryButton { background: #477ba8; border-color: #355e83; }
QPushButton#primaryButton:hover { background: #568bb9; }
QPushButton:focus, QToolButton:focus, QComboBox:focus, QLineEdit:focus,
QAbstractSpinBox:focus { border: 1px solid #709cc1; }
QToolButton[workspace="true"] { border: none; border-radius: 3px 3px 0 0;
  background: #292929; padding: 7px 18px; }
QToolButton[workspace="true"]:checked { background: #454545; color: white; }
QLineEdit, QAbstractSpinBox, QComboBox { background: #545454; border: 1px solid #2d2d2d;
  border-radius: 3px; padding: 4px 5px; min-height: 16px; selection-background-color: #477ba8; }
QLineEdit:disabled, QAbstractSpinBox:disabled, QComboBox:disabled { color: #909090; background: #424242; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView { background: #414141; selection-background-color: #477ba8; }
QSpinBox::up-button, QDoubleSpinBox::up-button { width: 12px; border: none; }
QSpinBox::down-button, QDoubleSpinBox::down-button { width: 12px; border: none; }
QTreeWidget, QListWidget { background: #353535; border: none; alternate-background-color: #393939;
  selection-background-color: #477ba8; outline: none; }
QTreeWidget::item, QListWidget::item { min-height: 23px; padding: 1px 3px; }
QTreeWidget::item:selected, QListWidget::item:selected { background: #477ba8; }
QHeaderView::section { background: #303030; border: none; border-bottom: 1px solid #252525;
  padding: 5px; color: #bcbcbc; }
QTabWidget::pane { border: none; background: #3d3d3d; }
QTabBar::tab { background: #303030; padding: 8px 12px; border: none; }
QTabBar::tab:selected { background: #3d3d3d; border-top: 2px solid #477ba8; }
QScrollArea { border: none; background: #3d3d3d; }
QScrollArea > QWidget > QWidget { background: #3d3d3d; }
QScrollBar:vertical { background: #333333; width: 9px; margin: 0; }
QScrollBar::handle:vertical { background: #646464; border-radius: 4px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { height: 9px; background: #333333; }
QScrollBar::handle:horizontal { background: #646464; min-width: 24px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QSplitter::handle { background: #242424; }
QSplitter::handle:horizontal { width: 4px; }
QSplitter::handle:vertical { height: 4px; }
QPlainTextEdit { background: #292929; border: 1px solid #242424; color: #c5d7bb;
  font-family: 'DejaVu Sans Mono'; font-size: 10px; padding: 7px; }
QCheckBox { spacing: 6px; }
QCheckBox::indicator { width: 13px; height: 13px; }
QGroupBox { border: none; border-top: 1px solid #303030; margin-top: 15px; padding-top: 12px;
  font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 3px; }
QToolTip { background: #202020; color: #ededed; border: 1px solid #606060; padding: 6px; }
QSlider::groove:horizontal { height: 4px; background: #252525; }
QSlider::sub-page:horizontal { background: #477ba8; }
QSlider::handle:horizontal { background: #b1b1b1; width: 10px; margin: -4px 0; border-radius: 3px; }
"""


def apply_theme(app):
    app.setStyle("Fusion")
    app.setFont(QFont("Noto Sans CJK SC", 9))
    palette = QPalette()
    for role, color in ((QPalette.Window, PANEL), (QPalette.Base, BASE),
                        (QPalette.Button, FIELD), (QPalette.Text, TEXT),
                        (QPalette.WindowText, TEXT), (QPalette.ButtonText, TEXT),
                        (QPalette.Highlight, BLUE), (QPalette.HighlightedText, "#ffffff")):
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)
