"""Verify distributable runtime resources and the pinned source dependency, then hash artifacts."""

import hashlib
import tarfile
import tomllib
from email.parser import Parser
from pathlib import Path
from zipfile import ZipFile


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    wheels = list((root / "dist").glob(f"ehentai_to_tg-{version}-*.whl"))
    require(len(wheels) == 1, "Build exactly one wheel for the current version first")
    wheel = wheels[0]
    source = root / "dist" / f"ehentai_to_tg-{version}.tar.gz"
    require(source.is_file(), "The source distribution is missing")
    resources = {
        f"ehtg/storage/migrations/{path.name}"
        for path in (root / "src/ehtg/storage/migrations").glob("*.sql")
    } | {"ehtg/notices/THIRD_PARTY_NOTICES.md"}
    expected_dependency = project["project"]["optional-dependencies"]["source"][0]
    with ZipFile(wheel) as archive:
        names = set(archive.namelist())
        require(resources <= names, "The wheel is missing a migration or third-party notice")
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(archive.read(metadata_name).decode("utf-8"))
        dependencies = metadata.get_all("Requires-Dist", [])
        require(
            any(expected_dependency in value for value in dependencies),
            "The wheel lost the pinned gallery-dl URL/hash",
        )
    with tarfile.open(source) as archive:
        tar_names = archive.getnames()
        require(
            any(name.endswith("/THIRD_PARTY_NOTICES.md") for name in tar_names),
            "The source archive is missing third-party notices",
        )
        require(
            all(
                any(name.endswith("/src/" + resource) for name in tar_names)
                for resource in resources
                if "/migrations/" in resource
            ),
            "The source archive is missing SQL migrations",
        )
    manifest = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in (wheel, source)
    )
    (root / "dist/SHA256SUMS").write_text(manifest, encoding="utf-8", newline="\n")
    print(f"Verified wheel/sdist {version}; wrote dist/SHA256SUMS")


if __name__ == "__main__":
    main()
