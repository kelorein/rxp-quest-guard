"""Build the addon ZIP and the standalone Windows desktop ZIP."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from questguard import __version__
from questguard.installation import ADDON_NAME, addon_source, inspect_installation, installed_toc


STARTER_DATA = (
    b"-- Replaced by RXP Quest Guard Desktop when publishing local quest or route data.\n"
    b"local _, QG = ...\n"
    b"QG.data = {schemaVersion = 1, quests = {}, route = {}}\n"
)


def release_addon_files() -> tuple[Path, dict[str, bytes]]:
    source = addon_source()
    manifest = source / f"{ADDON_NAME}.toc"
    if manifest.is_symlink() or manifest.resolve().parent != source.resolve():
        raise ValueError("The addon manifest must be a regular source file.")
    data = manifest.read_bytes()
    files = {manifest.name: data}
    lines = data.decode("utf-8").splitlines()
    if f"## Version: {__version__}" not in lines:
        raise ValueError("Addon and desktop versions must match before packaging.")
    for row in lines:
        name = row.strip()
        if not name or name.startswith("#"):
            continue
        module = source / name
        if (not name.isascii() or not name.endswith(".lua") or not name[:-4].isidentifier()
                or module.is_symlink() or module.resolve().parent != source.resolve() or not module.is_file()):
            raise ValueError(f"Invalid addon manifest entry: {name}")
        files[name] = module.read_bytes()
    if files.get("Data.lua", b"").replace(b"\r\n", b"\n") != STARTER_DATA:
        raise ValueError("The release requires the empty starter Data.lua. Keep published quest data outside the source tree.")
    return manifest, files


def build_addon(output: Path, interface: int) -> Path:
    destination = output / f"RXP-Quest-Guard-Addon-{__version__}.zip"
    manifest, files = release_addon_files()
    files[manifest.name] = installed_toc(files[manifest.name], interface)
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(f"{ADDON_NAME}/{name}", data)
        archive.write(ROOT / "LICENSE", f"{ADDON_NAME}/LICENSE.txt")
    return destination


def build_desktop(output: Path) -> Path:
    if os.name != "nt" or sys.maxsize <= 2**32:
        raise ValueError("A 64-bit Windows Python installation with Tk is required for this build.")
    work = ROOT / ".build" / "release"
    work.mkdir(parents=True, exist_ok=True)
    _, files = release_addon_files()
    name = "RXP Quest Guard Desktop"
    dist = work / "bundles"
    env = {**os.environ, "PYINSTALLER_CONFIG_DIR": str(work / "cache")}
    with TemporaryDirectory(prefix="addon-source-", dir=work) as directory:
        for filename, data in files.items():
            (Path(directory) / filename).write_bytes(data)
        subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed",
                        "--noupx", "--name", name, "--paths", str(ROOT), "--distpath", str(dist),
                        "--workpath", str(work / "work"), "--specpath", str(work),
                        "--add-data", f"{directory}:addon/{ADDON_NAME}",
                        str(ROOT / "scripts" / "desktop_entry.py")], cwd=ROOT, env=env, check=True)
    bundle = dist / name
    for relative in ("README.md", "LICENSE", "CONTRIBUTING.md", "SECURITY.md", "THIRD-PARTY-NOTICES.md", "docs/DATA-AND-PRIVACY.md",
                     "docs/DEMO.md", "docs/VERIFICATION.md"):
        destination = bundle / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    runtime_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not runtime_license.is_file():
        raise ValueError("The Python distribution's LICENSE.txt could not be found.")
    shutil.copyfile(runtime_license, bundle / "PYTHON-RUNTIME-LICENSE.txt")
    destination = output / f"RXP-Quest-Guard-Desktop-{__version__}-win-x64.zip"
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, (Path(name) / path.relative_to(bundle)).as_posix())
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    evidence = parser.add_mutually_exclusive_group(required=True)
    evidence.add_argument("--wow", type=Path, help="Local WoW Forever folder for interface evidence")
    evidence.add_argument("--interface", type=int, help="Fourth GetBuildInfo() value from the target WoW client")
    parser.add_argument("--addon-only", action="store_true")
    args = parser.parse_args()
    interface = args.interface if args.wow is None else inspect_installation(args.wow).interface
    if interface is None:
        parser.error("Use --interface with the numeric value reported by GetBuildInfo().")
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    artifacts = [build_addon(output, interface)]
    if not args.addon_only:
        artifacts.append(build_desktop(output))
    rows = []
    for path in artifacts:
        with path.open("rb") as file:
            sha = hashlib.file_digest(file, "sha256").hexdigest()
        rows.append(f"{sha}  {path.name}")
        print(path)
    sums = "\n".join(rows) + "\n"
    (output / f"SHA256SUMS-{__version__}.txt").write_text(sums, encoding="ascii")
    (output / "SHA256SUMS.txt").write_text(sums, encoding="ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
