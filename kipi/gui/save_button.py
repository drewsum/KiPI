"""A Save button that shows when there is something to save."""

UNSAVED_STYLE = "QPushButton { border: 2px solid #ff8c00; border-radius: 3px; padding: 4px 12px; }"


def mark_unsaved(button, unsaved: bool, tip: str) -> None:
    """Outline `button` while the setup has changes not saved; `tip` is its usual tooltip."""
    button.setStyleSheet(UNSAVED_STYLE if unsaved else "")
    button.setToolTip(f"{tip}. There are changes not saved yet." if unsaved else tip)
