from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

from scripts.build_release import build_addon, release_addon_files


class ReleaseTests(TestCase):
    def test_packages_manifest_modules_only_and_rejects_published_data(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "RXPQuestGuard"
            copytree(Path(__file__).resolve().parents[1] / "addon" / source.name, source)
            (source / "ExtraExport.lua").write_text("-- synthetic local file", encoding="utf-8")
            with patch("scripts.build_release.addon_source", return_value=source):
                toc, files = release_addon_files()
                with ZipFile(build_addon(root, 16001)) as archive:
                    self.assertEqual(set(archive.namelist()),
                                     {f"{source.name}/{name}" for name in (*files, "LICENSE.txt")})
                    self.assertIn(b"## Interface: 16001", archive.read(f"{source.name}/{toc.name}"))

                with (source / "Data.lua").open("a", encoding="utf-8") as local:
                    local.write("-- synthetic modification\n")
                with self.assertRaisesRegex(ValueError, "empty starter Data.lua"):
                    release_addon_files()
