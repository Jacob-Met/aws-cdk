#!/usr/bin/env python3
"""Archive-level regression tests for Python cloud assembly schema packaging."""

import base64
import csv
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

MODULE = Path(__file__).with_name("normalize-python-schema-packages.py")
spec = importlib.util.spec_from_file_location("schema_packages", MODULE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class TestPythonSchemaPackages(unittest.TestCase):
    def test_wheel_removes_overlap_and_updates_record(self):
        with tempfile.TemporaryDirectory() as d:
            wheel = Path(d) / "aws_cdk_lib-2.0.0-py3-none-any.whl"
            record = "aws_cdk_lib-2.0.0.dist-info/RECORD"
            with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as out:
                for name, contents in {
                    "aws_cdk/__init__.py": b"# generated CDK root\n",
                    "aws_cdk/cloud_assembly_schema/__init__.py": b"# duplicated schema\n",
                    "aws_cdk/cloud_assembly_schema/py.typed": b"",
                    "aws_cdk/core/__init__.py": b"# unrelated module\n",
                    record: b"",
                }.items():
                    out.writestr(name, contents)
            mod.fix_wheel(wheel)
            mod.fix_wheel(wheel)  # safe if the packaging command is repeated
            with zipfile.ZipFile(wheel) as src:
                self.assertNotIn("aws_cdk/cloud_assembly_schema/__init__.py", src.namelist())
                self.assertNotIn("aws_cdk/cloud_assembly_schema/py.typed", src.namelist())
                self.assertIn("aws_cdk/_cdk_cloud_assembly_schema/__init__.py", src.namelist())
                self.assertEqual(
                    src.read("aws_cdk/_cdk_cloud_assembly_schema/__init__.py"),
                    b"# duplicated schema\n",
                )
                root = src.read("aws_cdk/__init__.py")
                self.assertEqual(root.count(b"Register schema types from the independently installed"), 1)
                self.assertEqual(src.read("aws_cdk/core/__init__.py"), b"# unrelated module\n")
                self.assertEqual(
                    src.read(record).decode().count("aws_cdk/cloud_assembly_schema/"), 0
                )
                self.assertIn("aws_cdk/_cdk_cloud_assembly_schema/__init__.py", src.read(record).decode())
                rows = list(csv.reader(io.StringIO(src.read(record).decode())))
                self.assertEqual(len(rows), len(src.namelist()))
                for name, digest, size in rows:
                    if name == record:
                        self.assertEqual((digest, size), ("", ""))
                        continue
                    blob = src.read(name)
                    self.assertEqual(size, str(len(blob)))
                    expected = base64.urlsafe_b64encode(hashlib.sha256(blob).digest()).rstrip(b"=").decode()
                    self.assertEqual(digest, "sha256=" + expected)

    def test_sdist_removes_overlap_and_updates_sources(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "aws_cdk_lib-2.0.0.tar.gz"
            prefix = "aws_cdk_lib-2.0.0/src/"
            entries = {
                prefix + "aws_cdk/__init__.py": b"# generated CDK root\n",
                prefix + "aws_cdk/cloud_assembly_schema/__init__.py": b"# duplicated schema\n",
                prefix + "aws_cdk/cloud_assembly_schema/py.typed": b"",
                prefix + "aws_cdk/core/__init__.py": b"# unrelated module\n",
                "aws_cdk_lib-2.0.0/setup.py": b'packages = ["aws_cdk.cloud_assembly_schema"]\n',
                prefix + "aws_cdk_lib.egg-info/SOURCES.txt": (
                    b"src/aws_cdk/__init__.py\n"
                    b"src/aws_cdk/cloud_assembly_schema/__init__.py\n"
                    b"src/aws_cdk/cloud_assembly_schema/py.typed\n"
                    b"src/aws_cdk/core/__init__.py\n"
                ),
            }
            with tarfile.open(path, "w:gz") as out:
                for name, contents in entries.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(contents)
                    out.addfile(info, io.BytesIO(contents))
            mod.fix_sdist(path)
            with tarfile.open(path) as src:
                names = src.getnames()
                self.assertNotIn(prefix + "aws_cdk/cloud_assembly_schema/__init__.py", names)
                self.assertNotIn(prefix + "aws_cdk/cloud_assembly_schema/py.typed", names)
                self.assertIn(prefix + "aws_cdk/_cdk_cloud_assembly_schema/__init__.py", names)
                self.assertIn(prefix + "aws_cdk/core/__init__.py", names)
                root = src.extractfile(prefix + "aws_cdk/__init__.py").read()
                self.assertIn(b"Register schema types", root)
                setup = src.extractfile("aws_cdk_lib-2.0.0/setup.py").read()
                self.assertIn(b"aws_cdk._cdk_cloud_assembly_schema", setup)
                self.assertNotIn(b"aws_cdk.cloud_assembly_schema", setup)
                sources = src.extractfile(prefix + "aws_cdk_lib.egg-info/SOURCES.txt").read()
                self.assertNotIn(b"aws_cdk/cloud_assembly_schema/", sources)
                self.assertIn(b"aws_cdk/_cdk_cloud_assembly_schema/__init__.py", sources)
                self.assertIn(b"core/__init__.py", sources)


if __name__ == "__main__":
    unittest.main()
