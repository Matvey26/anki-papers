"""webapp."""
from __future__ import annotations

import argparse

from articles_to_anki.web.application import create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run Anki Papers web application.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    from waitress import serve

    serve(create_app(), host=args.host, port=args.port, threads=4)


# Compatibility for scripts that construct card contexts.
from .cards.text import make_target_context as make_target_context

if __name__ == "__main__":
    main()
