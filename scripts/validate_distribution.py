"""Inspect build contents and write SHA-256 provenance without importing the app."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

FORBIDDEN_PARTS = {"data", "reports", "output", ".local", ".git", ".venv", "site", "infra"}
RESEARCH_FILES = {"__init__.py", "contracts.py", "matching.py", "benchmark.py", "cli.py", "books.py", "execution.py"}


def verify_paths(paths: list[str]) -> None:
    for path in paths:
        parts = PurePosixPath(path).parts
        if PurePosixPath(path).is_absolute() or ".." in parts:
            raise ValueError(f"unsafe archive path: {path}")
        if any(part in FORBIDDEN_PARTS or part.startswith(".env") for part in parts):
            raise ValueError(f"private or operational file included in distribution: {path}")


def validate(dist: Path) -> dict:
    wheels = sorted(dist.glob("*.whl"))
    sources = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("expected exactly one wheel and one source distribution")
    wheel, source = wheels[0], sources[0]
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        verify_paths(names)
        required = {f"prediction_market/research/{name}" for name in RESEARCH_FILES}
        if not required.issubset(names):
            raise ValueError("wheel is missing research modules")
        entrypoints = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        if len(entrypoints) != 1 or "precedge-research = prediction_market.research.cli:main" not in archive.read(entrypoints[0]).decode():
            raise ValueError("wheel is missing the research CLI entry point")
    with tarfile.open(source, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        verify_paths(names)
        if any(member.issym() or member.islnk() for member in members):
            raise ValueError("source archive must not contain links")
        roots = {PurePosixPath(name).parts[0] for name in names}
        if len(roots) != 1:
            raise ValueError("source archive must contain a single project root")
        root = next(iter(roots))
        required = {
            f"{root}/pyproject.toml", f"{root}/README.md",
            f"{root}/docs/research_rollout.md", f"{root}/docs/ci_cd.md",
            f"{root}/scripts/validate_distribution.py",
            f"{root}/tests/fixtures/oddsportal_sample.html",
            *(f"{root}/src/prediction_market/research/{name}" for name in RESEARCH_FILES),
        }
        if not required.issubset(names):
            raise ValueError("source archive is missing source, documentation or test fixtures")
    commit = os.getenv("GITHUB_SHA") or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True,
    ).strip()
    files = [{"filename": path.name, "bytes": path.stat().st_size,
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in (wheel, source)]
    return {"schema_version": 1, "checkout_sha": commit, "files": files}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args(argv)
    report = validate(args.dist)
    (args.dist / "build-manifest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (args.dist / "SHA256SUMS").write_text(
        "".join(f"{row['sha256']}  {row['filename']}\n" for row in report["files"]), encoding="utf-8",
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
