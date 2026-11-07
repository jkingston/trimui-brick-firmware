#!/usr/bin/env python3
"""Create a deterministic, auditable Brick WPE binary SDK archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


PACKAGE = "brick-wpewebkit"
VERSION = "2.54.1"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_entry(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_file():
        shutil.copy2(source, destination)
    else:
        raise RuntimeError(f"unsupported installed entry: {source}")


def file_record(root: Path, path: Path) -> dict[str, object]:
    relative = path.relative_to(root).as_posix()
    if path.is_symlink():
        return {"path": relative, "symlink": os.readlink(path)}
    return {
        "path": relative,
        "sha256": sha256(path),
        "size": path.stat().st_size,
    }


def command(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="completed Buildroot output directory")
    parser.add_argument("destination", type=Path, help="directory for release archives")
    parser.add_argument("--revision", default="1", help="Brick binary revision")
    args = parser.parse_args()

    firmware = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    staging = output / "staging"
    if not staging.is_dir():
        sysroots = sorted((output / "host").glob("aarch64*/sysroot"))
        if len(sysroots) != 1:
            raise SystemExit(f"expected one AArch64 staging sysroot, found {sysroots}")
        staging = sysroots[0]
    target = output / "target"
    build = output / "build" / f"{PACKAGE}-{VERSION}"
    manifest_path = build / "install_manifest.txt"
    installed_stamp = build / ".stamp_installed"
    if not installed_stamp.exists() or not manifest_path.exists():
        raise SystemExit(f"{build} is not a completed installed package")
    if not staging.is_dir() or not target.is_dir():
        raise SystemExit(f"missing staging or target tree below {output}")

    package_dir = firmware / "package" / PACKAGE
    source_hash = next(
        line.split()[1]
        for line in (package_dir / f"{PACKAGE}.hash").read_text().splitlines()
        if line.startswith("sha256 ") and line.rstrip().endswith(f"wpewebkit-{VERSION}.tar.xz")
    )
    patches = sorted((package_dir / "source-patches").glob("*.patch"))
    source_options = package_dir / "source-cmake-options.txt"
    inputs = [
        package_dir / f"{PACKAGE}.mk",
        package_dir / "Config.in",
        source_options,
        *patches,
        output / ".config",
    ]
    input_records = [
        {"path": path.relative_to(firmware).as_posix() if path.is_relative_to(firmware) else path.name,
         "sha256": sha256(path)}
        for path in inputs
    ]
    fingerprint = hashlib.sha256(
        json.dumps(input_records, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    config_lines = (output / ".config").read_text().splitlines()
    config_value = {
        line.split("=", 1)[0]: line.split("=", 1)[1].strip('"')
        for line in config_lines
        if line.startswith("BR2_EXTERNAL_") and "=" in line
    }
    name = f"{PACKAGE}-{VERSION}-r{args.revision}-aarch64-buildroot-{fingerprint}"
    args.destination.mkdir(parents=True, exist_ok=True)

    compilers = sorted((output / "host" / "bin").glob("aarch64*-gcc"))
    if len(compilers) != 1:
        raise SystemExit(f"expected one AArch64 cross-compiler, found {compilers}")
    compiler = compilers[0]
    epoch = int(command("git", "show", "-s", "--format=%ct", "HEAD", cwd=firmware))
    with tempfile.TemporaryDirectory(prefix=f"{PACKAGE}-") as temporary:
        archive_root = Path(temporary) / name
        entries: list[Path] = []
        target_files: list[str] = []
        for installed in manifest_path.read_text().splitlines():
            relative = installed.lstrip("/")
            staging_source = staging / relative
            target_source = target / relative
            if target_source.exists() or target_source.is_symlink():
                # Buildroot strips target ELFs.  They are still valid link inputs,
                # so store that smaller copy once and use it in both trees.
                source = target_source
                target_files.append(relative)
            else:
                source = staging_source
            if source.exists() or source.is_symlink():
                destination = archive_root / "staging" / relative
                copy_entry(source, destination)
                entries.append(destination)

        if not entries:
            raise SystemExit("install manifest selected no staging or target files")
        licenses = archive_root / "licenses"
        for license_name in ("LICENSE-APPLE", "LICENSE-LGPL-2.1"):
            destination = licenses / license_name
            copy_entry(build / "Source" / "WebCore" / license_name, destination)
            entries.append(destination)
        (archive_root / "target-files.txt").write_text(
            "".join(f"{path}\n" for path in sorted(target_files))
        )
        entries.append(archive_root / "target-files.txt")
        records = sorted(
            (file_record(archive_root, path) for path in entries),
            key=lambda record: str(record["path"]),
        )
        manifest = {
            "schema": 1,
            "package": PACKAGE,
            "version": VERSION,
            "revision": args.revision,
            "architecture": "aarch64",
            "source_sha256": source_hash,
            "packaged_by_firmware_commit": command("git", "rev-parse", "HEAD", cwd=firmware),
            "build_provenance": {
                "brick_version": config_value.get("BR2_EXTERNAL_BRICK_VERSION"),
                "product_version": config_value.get("BR2_EXTERNAL_BRICK_UI_VERSION"),
            },
            "buildroot_config_sha256": sha256(output / ".config"),
            "compiler": {
                "machine": command(str(compiler), "-dumpmachine"),
                "version": command(str(compiler), "-dumpfullversion", "-dumpversion"),
            },
            "input_fingerprint": fingerprint,
            "inputs": input_records,
            "target_files": sorted(target_files),
            "files": records,
        }
        (archive_root / "MANIFEST.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )

        binary_archive = args.destination / f"{name}.tar.gz"
        with tarfile.open(binary_archive, "w:gz", compresslevel=6) as archive:
            for path in sorted(archive_root.rglob("*")):
                info = archive.gettarinfo(path, arcname=path.relative_to(archive_root.parent))
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mtime = epoch
                if path.is_file() and not path.is_symlink():
                    with path.open("rb") as stream:
                        archive.addfile(info, stream)
                else:
                    archive.addfile(info)

    source_name = f"{PACKAGE}-{VERSION}-r{args.revision}-sources"
    source_archive = args.destination / f"{source_name}.tar.gz"
    source_tarball = (
        firmware / "output" / "downloads" / PACKAGE / f"wpewebkit-{VERSION}.tar.xz"
    )
    if not source_tarball.exists() or sha256(source_tarball) != source_hash:
        raise SystemExit(f"missing or invalid upstream source tarball: {source_tarball}")
    with tempfile.TemporaryDirectory(prefix=f"{PACKAGE}-sources-") as temporary:
        source_root = Path(temporary) / source_name
        source_root.mkdir()
        shutil.copy2(source_tarball, source_root / source_tarball.name)
        for path in [package_dir / f"{PACKAGE}.mk", package_dir / f"{PACKAGE}.hash", package_dir / "Config.in", source_options, *patches]:
            shutil.copy2(path, source_root / path.name)
        with tarfile.open(source_archive, "w:gz", compresslevel=6) as archive:
            for path in sorted(source_root.rglob("*")):
                info = archive.gettarinfo(path, arcname=path.relative_to(source_root.parent))
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mtime = epoch
                with path.open("rb") as stream:
                    archive.addfile(info, stream)

    checksums = args.destination / f"{name}.sha256"
    checksums.write_text(
        f"{sha256(binary_archive)}  {binary_archive.name}\n"
        f"{sha256(source_archive)}  {source_archive.name}\n"
    )
    print(binary_archive)
    print(source_archive)
    print(checksums)


if __name__ == "__main__":
    main()
