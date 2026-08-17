from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen


URL = (
    "https://download.freedict.org/dictionaries/eng-rus/2025.11.23/"
    "freedict-eng-rus-2025.11.23.stardict.tar.xz"
)
SHA512 = (
    "ffbfbbe9817dc9a2a84fb8556d99ba73b5bc572eab18f91988029adfc2a7917616dd20d13c94a99871857a047ce2a47a3538bde545f97065eb7e531d7a6ef4b4"
)
REQUIRED_FILES = ("eng-rus.ifo", "eng-rus.idx.gz", "eng-rus.dict", "COPYING")

WORDNET_URL = (
    "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/"
    "packages/corpora/wordnet.zip"
)
WORDNET_SHA512 = (
    "1923a8bcd56fa0b9a9de91f53070dce28c3a7efbab11d2ef55c87134b1bf30de0f40abab59c39eb15dce54aec9491d8a5a259de212ff4cb25cde0ad09317009a"
)
WORDNET_FILES = ("wordnet/data.noun", "wordnet/data.verb", "wordnet/data.adj")


def install(data_dir: Path) -> tuple[Path, Path]:
    dictionary = _install_dictionary(data_dir)
    wordnet = _install_wordnet(data_dir)
    return dictionary, wordnet


def _install_dictionary(data_dir: Path) -> Path:
    destination = data_dir.resolve() / "dictionaries" / "eng-rus"
    if all((destination / name).is_file() for name in REQUIRED_FILES):
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        temporary_path = Path(temporary)
        archive = temporary_path / "eng-rus.tar.xz"
        request = Request(URL, headers={"User-Agent": "Anki-Papers/1.0"})
        with urlopen(request, timeout=30) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        digest = hashlib.sha512(archive.read_bytes()).hexdigest()
        if digest != SHA512:
            raise RuntimeError("FreeDict archive checksum mismatch")

        with tarfile.open(archive, "r:xz") as package:
            package.extractall(temporary_path, filter="data")
        extracted = temporary_path / "eng-rus"
        if not all((extracted / name).is_file() for name in REQUIRED_FILES):
            raise RuntimeError("FreeDict archive is incomplete")
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(extracted, destination)
    return destination


def _install_wordnet(data_dir: Path) -> Path:
    destination = data_dir.resolve() / "nltk_data" / "corpora" / "wordnet"
    if all((destination / name[8:]).is_file() for name in WORDNET_FILES):
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        temporary_path = Path(temporary)
        archive = temporary_path / "wordnet.zip"
        request = Request(WORDNET_URL, headers={"User-Agent": "Anki-Papers/1.0"})
        with urlopen(request, timeout=60) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        digest = hashlib.sha512(archive.read_bytes()).hexdigest()
        if digest != WORDNET_SHA512:
            raise RuntimeError("WordNet archive checksum mismatch")

        with zipfile.ZipFile(archive) as package:
            names = package.namelist()
            if not all(name.startswith("wordnet/") for name in names):
                raise RuntimeError("WordNet archive has unexpected layout")
            package.extractall(temporary_path)
        extracted = temporary_path / "wordnet"
        if not all((extracted / name[8:]).is_file() for name in WORDNET_FILES):
            raise RuntimeError("WordNet archive is incomplete")
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(extracted, destination)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Install local English-Russian dictionary")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    dictionary, wordnet = install(args.data_dir)
    print(dictionary)
    print(wordnet)


if __name__ == "__main__":
    main()
