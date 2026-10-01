"""The user guide, in a window of its own.

The guide ships inside the package (`audiorouter/guide/user_guide.md`), so it
works offline and always describes the version that is installed. Qt renders
the Markdown, tables included. Pictures are scaled to the page as Qt asks for
them (`GuideBrowser.loadResource`): a Markdown image cannot say how wide to be,
and resources registered beforehand are dropped - `setMarkdown` clears them.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QImage, QTextDocument
from PyQt6.QtWidgets import QDialog, QTextBrowser, QVBoxLayout, QWidget

GUIDE_DIR = Path(__file__).resolve().parent.parent / "guide"
GUIDE = GUIDE_DIR / "user_guide.md"
#: Pictures are drawn no wider than this, so they fit the window as opened.
IMAGE_WIDTH = 720


class GuideBrowser(QTextBrowser):
    """Shows the guide; hands Qt each picture already scaled to the page."""

    def loadResource(self, kind: int, url: QUrl):
        if kind == QTextDocument.ResourceType.ImageResource.value:
            picture = GUIDE_DIR / Path(url.path()).name
            image = QImage(str(picture))
            if not image.isNull():
                return image.scaledToWidth(IMAGE_WIDTH) if image.width() > IMAGE_WIDTH else image
        return super().loadResource(kind, url)


class GuideWindow(QDialog):
    """A read-only, resizable window showing the user guide."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Audio Router — User Guide")
        self.resize(820, 760)
        self.browser = GuideBrowser(self)
        self.browser.setOpenExternalLinks(True)
        self.browser.document().setDocumentMargin(16)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.browser)
        self.load()

    def load(self) -> None:
        try:
            self.browser.setMarkdown(GUIDE.read_text(encoding="utf-8"))
        except OSError:
            self.browser.setPlainText(f"The user guide could not be found at {GUIDE}.")
