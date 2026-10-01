"""The user guide, in a window of its own.

The guide ships inside the package (`audiorouter/guide/user_guide.md`), so it
works offline and always describes the version that is installed. Qt renders
the Markdown; `style_guide` then sets it like a well-made page rather than
Qt's dense defaults (owner, 1 Oct 2026: "less like an appliance manual"): a
centred reading column, more air between lines and paragraphs, headings in
the theme's accent colour, tables with padding, soft rules and striped rows.
Colours come from the palette, so it reads in light and dark themes alike.

Pictures are scaled to the column as Qt asks for them (`loadResource`): a
Markdown image cannot say how wide to be, and resources registered
beforehand are dropped - `setMarkdown` clears them.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QPalette,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFrameFormat,
    QTextLength,
    QTextTable,
)
from PyQt6.QtWidgets import QDialog, QTextBrowser, QVBoxLayout, QWidget

from .theme import Theme

GUIDE_DIR = Path(__file__).resolve().parent.parent / "guide"
GUIDE = GUIDE_DIR / "user_guide.md"
#: The reading column: lines much longer than this are tiring to follow.
COLUMN_WIDTH = 760
#: Pictures are drawn no wider than the column.
IMAGE_WIDTH = 700
#: Body text, relative to the desktop's font.
TEXT_SCALE = 1.1
LINE_HEIGHT = 150  # percent, for paragraphs
TABLE_LINE_HEIGHT = 125  # tighter in a table cell
PARAGRAPH_GAP = 10
LIST_GAP = 4
#: Space round the column when the window is narrow.
PAGE_MARGIN = 28
HEADING_SIZES = {1: 2.0, 2: 1.45, 3: 1.2}  # relative to the body text
HEADING_GAP_ABOVE = {1: 4, 2: 30, 3: 18}


class GuideBrowser(QTextBrowser):
    """Shows the guide in a centred column; hands Qt each picture scaled to fit."""

    def loadResource(self, kind: int, url: QUrl):
        if kind == QTextDocument.ResourceType.ImageResource.value:
            picture = GUIDE_DIR / Path(url.path()).name
            image = QImage(str(picture))
            if not image.isNull():
                return image.scaledToWidth(IMAGE_WIDTH) if image.width() > IMAGE_WIDTH else image
        return super().loadResource(kind, url)

    def resizeEvent(self, event) -> None:
        # Centre the column with the page's own margins, so a wide window gets
        # more white space, not longer lines (viewport margins showed the
        # window's grey at the sides and a sideways scroll bar).
        super().resizeEvent(event)
        self.fit_column()

    def fit_column(self) -> None:
        side = max(PAGE_MARGIN, (self.viewport().width() - COLUMN_WIDTH) // 2)
        root = self.document().rootFrame()
        fmt = root.frameFormat()
        if (fmt.leftMargin(), fmt.topMargin()) != (side, PAGE_MARGIN):
            fmt.setLeftMargin(side)
            fmt.setRightMargin(side)
            fmt.setTopMargin(PAGE_MARGIN)
            fmt.setBottomMargin(PAGE_MARGIN)
            root.setFrameFormat(fmt)


def style_guide(document: QTextDocument, palette: QPalette, theme: Theme) -> None:
    """Restyle a document Qt built from Markdown: spacing, headings, tables, pictures."""
    base = palette.color(QPalette.ColorRole.Base)
    accent = palette.color(QPalette.ColorRole.Highlight)
    rule = Theme.blend(palette.color(QPalette.ColorRole.Text), base, 0.18)
    header = Theme.blend(accent, base, 0.14)
    stripe = Theme.blend(palette.color(QPalette.ColorRole.Text), base, 0.04)
    body_size = document.defaultFont().pointSizeF()

    block = document.begin()
    while block.isValid():
        cursor = QTextCursor(block)
        fmt = block.blockFormat()
        level = fmt.headingLevel()
        in_table = cursor.currentTable() is not None
        if level:
            fmt.setTopMargin(HEADING_GAP_ABOVE.get(level, 14))
            fmt.setBottomMargin(8)
            cursor.setBlockFormat(fmt)
            heading = QTextCharFormat()
            heading.setFontPointSize(body_size * HEADING_SIZES.get(level, 1.1))
            heading.setFontWeight(QFont.Weight.DemiBold if level > 1 else QFont.Weight.Bold)
            if level > 1:
                heading.setForeground(QBrush(accent))
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            cursor.mergeCharFormat(heading)
        else:
            fmt.setLineHeight(TABLE_LINE_HEIGHT if in_table else LINE_HEIGHT,
                              QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            if not in_table:
                fmt.setBottomMargin(LIST_GAP if block.textList() is not None else PARAGRAPH_GAP)
            if block.text() == "\ufffc":  # a picture on its own line
                # Single spacing: a proportional height adds half the
                # picture's height again as blank space under it.
                fmt.setLineHeight(100, QTextBlockFormat.LineHeightTypes.SingleHeight.value)
                fmt.setAlignment(fmt.alignment().AlignHCenter)
                fmt.setTopMargin(8)
                fmt.setBottomMargin(12)
            cursor.setBlockFormat(fmt)
        block = block.next()

    for frame in document.rootFrame().childFrames():
        if not isinstance(frame, QTextTable):
            continue
        table: QTextTable = frame
        tfmt = table.format()
        tfmt.setCellPadding(9)
        tfmt.setCellSpacing(0)
        tfmt.setBorder(1)
        tfmt.setBorderBrush(QBrush(rule))
        tfmt.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
        tfmt.setBorderCollapse(True)
        tfmt.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
        tfmt.setTopMargin(6)
        tfmt.setBottomMargin(16)
        if table.columns() >= 2:
            first = 30 if table.columns() == 2 else 24
            rest = (100 - first) / (table.columns() - 1)
            tfmt.setColumnWidthConstraints(
                [QTextLength(QTextLength.Type.PercentageLength, first)]
                + [QTextLength(QTextLength.Type.PercentageLength, rest)] * (table.columns() - 1))
        table.setFormat(tfmt)
        for row in range(table.rows()):
            for col in range(table.columns()):
                cell = table.cellAt(row, col)
                cfmt = cell.format().toTableCellFormat()
                if row == 0:
                    cfmt.setBackground(QBrush(header))
                    cfmt.setFontWeight(QFont.Weight.DemiBold)
                elif row % 2 == 0:
                    cfmt.setBackground(QBrush(stripe))
                cell.setFormat(cfmt)


class GuideWindow(QDialog):
    """A read-only, resizable window showing the user guide."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Audio Router — User Guide")
        self.resize(900, 820)
        self.browser = GuideBrowser(self)
        self.browser.setOpenExternalLinks(True)
        self.browser.setFrameShape(QTextBrowser.Shape.NoFrame)
        document = self.browser.document()
        document.setDocumentMargin(0)
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() * TEXT_SCALE)
        document.setDefaultFont(font)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.browser)
        self.load()

    def load(self) -> None:
        try:
            self.browser.setMarkdown(GUIDE.read_text(encoding="utf-8"))
        except OSError:
            self.browser.setPlainText(f"The user guide could not be found at {GUIDE}.")
            return
        style_guide(self.browser.document(), self.browser.palette(), Theme(self))
        self.browser.fit_column()  # loading the Markdown reset the page's margins
