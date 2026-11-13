#!/usr/bin/env python3
"""Make aws-cdk-lib's Python wheel share schema types without sharing files.

The standalone aws-cdk.cloud-assembly-schema distribution owns the public
aws_cdk.cloud_assembly_schema module and the jsii assembly it loads. jsii-pacmak
also generates that module for the aws-cdk-lib re-export, with different jsii
type identifiers. If both distributions write the same path, installation
order changes runtime behavior (aws/aws-cdk#38482).
"""

import base64
import copy
import csv
import hashlib
import io
import os
from pathlib import Path
import tarfile
import tempfile
import zipfile

MODULE = "aws_cdk/cloud_assembly_schema/"
PRIVATE = "aws_cdk/_cdk_cloud_assembly_schema/"

# The jsii kernel returns FQNs from aws-cdk-lib's re-export. Python classes
# come from the standalone distribution, so register equivalent FQN aliases.
# This is intentionally in aws_cdk/__init__.py: the standalone distribution
# owns the schema submodule and must never be overwritten by aws-cdk-lib.
ALIASES = '''
# Register schema types from the independently installed schema distribution
# under the FQNs emitted by aws-cdk-lib's re-export.
import aws_cdk.cloud_assembly_schema as _cdk_schema
if not hasattr(_cdk_schema, "MetadataEntry"):
    # An upgrade from the old overlapping wheels can remove the shared module
    # when the old aws-cdk-lib wheel is uninstalled. Use its private copy
    # without claiming the standalone distribution's public module path.
    import aws_cdk._cdk_cloud_assembly_schema as _cdk_fallback
    _cdk_schema.__dict__.update(
        (name, value) for name, value in vars(_cdk_fallback).items()
        if not name.startswith("__") or name == "__all__"
    )
    del _cdk_fallback
from jsii import _reference_map as _cdk_refs
for _cdk_kind in ("_types", "_data_types", "_enums", "_interfaces"):
    _cdk_registry = getattr(_cdk_refs, _cdk_kind)
    for _cdk_fqn, _cdk_type in tuple(_cdk_registry.items()):
        if _cdk_fqn.startswith("@aws-cdk/cloud-assembly-schema."):
            _cdk_registry.setdefault(
                "aws-cdk-lib.cloud_assembly_schema." + _cdk_fqn.split(".", 1)[1],
                _cdk_type,
            )
del _cdk_schema, _cdk_refs, _cdk_kind, _cdk_registry, _cdk_fqn, _cdk_type
'''.encode("utf-8")


def patched_root(contents):
    if b'# Register schema types from the independently installed schema distribution' in contents:
        return contents
    return contents.rstrip() + b"\n\n" + ALIASES


def _replace_atomically(path, write):
    path = Path(path)
    with tempfile.NamedTemporaryFile(prefix=path.name + ".", dir=path.parent, delete=False) as target:
        temp_path = Path(target.name)
    try:
        write(temp_path)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def fix_wheel(path):
    path = Path(path)

    def write(target):
        with zipfile.ZipFile(path) as original:
            names = original.namelist()
            record_paths = [name for name in names if name.endswith(".dist-info/RECORD")]
            if len(record_paths) != 1 or "aws_cdk/__init__.py" not in names:
                raise ValueError(f"Unexpected aws-cdk-lib wheel layout: {path}")
            record_path = record_paths[0]
            with zipfile.ZipFile(target, "w") as output:
                records = []
                for info in original.infolist():
                    name = info.filename
                    if name == record_path:
                        continue
                    content = original.read(name)
                    if name == "aws_cdk/__init__.py":
                        content = patched_root(content)
                    output_name = (
                        PRIVATE + name[len(MODULE):]
                        if name.startswith(MODULE) else name
                    )
                    output_info = copy.copy(info)
                    output_info.filename = output_name
                    output.writestr(output_info, content)
                    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
                    records.append((output_name, "sha256=" + digest, str(len(content))))
                record_data = io.StringIO(newline="")
                writer = csv.writer(record_data, lineterminator="\n")
                writer.writerows(records + [(record_path, "", "")])
                output.writestr(original.getinfo(record_path), record_data.getvalue().encode("utf-8"))

    _replace_atomically(path, write)


def fix_sdist(path):
    path = Path(path)

    def write(target):
        with tarfile.open(path, "r:gz") as original, tarfile.open(target, "w:gz") as output:
            members = original.getmembers()
            if not any(m.name.endswith("/aws_cdk/__init__.py") for m in members):
                raise ValueError(f"Unexpected aws-cdk-lib sdist layout: {path}")
            for member in members:
                if not member.isfile():
                    output.addfile(member)
                    continue
                data = original.extractfile(member).read()
                if member.name.endswith("/aws_cdk/__init__.py"):
                    data = patched_root(data)
                elif member.name.endswith("/SOURCES.txt"):
                    data = data.replace(
                        MODULE.encode("utf-8"), PRIVATE.encode("utf-8")
                    )
                elif member.name.endswith("/setup.py"):
                    data = data.replace(
                        b"aws_cdk.cloud_assembly_schema",
                        b"aws_cdk._cdk_cloud_assembly_schema",
                    )
                if ("/" + MODULE) in member.name:
                    member.name = member.name.replace("/" + MODULE, "/" + PRIVATE, 1)
                member.size = len(data)
                output.addfile(member, io.BytesIO(data))

    _replace_atomically(path, write)


def main():
    root = Path("dist/python")
    wheels = sorted(root.rglob("*.whl"))
    sdists = sorted(root.rglob("*.tar.gz"))
    for path in wheels:
        fix_wheel(path)
    for path in sdists:
        fix_sdist(path)
    print(f"Normalized schema module ownership in {len(wheels)} Python wheels and {len(sdists)} sdists")


if __name__ == "__main__":
    main()
