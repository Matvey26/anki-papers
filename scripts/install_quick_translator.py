from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

import argostranslate.package as argos_package
import argostranslate.translate as argos_translate


URL = "https://argos-net.com/v1/translate-en_ru-1_9.argosmodel"
SHA256 = (
    "591d743ae103752b88ffc38785c50421320f4eff93c8967e0d3d2e14d4e27811"
)


def is_installed() -> bool:
    languages = argos_translate.get_installed_languages()
    english = next(
        (language for language in languages if language.code == "en"), None
    )
    russian = next(
        (language for language in languages if language.code == "ru"), None
    )
    return english is not None and russian is not None and english.get_translation(russian) is not None


def install(data_dir: Path) -> Path:
    destination = data_dir.resolve() / "models" / "en_ru.argosmodel"
    if is_installed():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        archive = Path(temporary) / "en_ru.argosmodel"
        request = Request(URL, headers={"User-Agent": "Anki-Papers/1.0"})
        with urlopen(request, timeout=300) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != SHA256:
            raise RuntimeError("Argos model checksum mismatch")
        argos_package.install_from_path(archive)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install local Argos Translate English-Russian model"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    print(install(args.data_dir))


if __name__ == "__main__":
    main()
