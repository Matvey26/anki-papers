from __future__ import annotations

from articles_to_anki.extract import (
    Token,
)


def token(text: str, index: int, *, x0: float | None = None) -> Token:
    left = float(index * 20 if x0 is None else x0)
    return Token(
        text=text,
        page_index=0,
        x0=left,
        x1=left + 15,
        top=100,
        bottom=111,
        local_index=index,
        global_index=index,
        selected=True,
        coverage=0.8,
    )
