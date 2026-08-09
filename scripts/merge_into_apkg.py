from __future__ import annotations

import argparse
from pathlib import Path

from articles_to_anki.apkg import merge


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("combined_csv", type=Path)
    parser.add_argument("csv", type=Path, nargs="+")
    args = parser.parse_args()
    merge(args.source, args.destination, args.csv, args.combined_csv)


if __name__ == "__main__":
    main()
