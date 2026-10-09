from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "validate_distribution", Path(__file__).parents[1] / "scripts" / "validate_distribution.py",
)
assert spec is not None and spec.loader is not None
distribution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(distribution)


def artifacts(root: Path, *, missing: str = "", extra: str = "", link: bool = False) -> tuple[Path, Path]:
    root.mkdir(exist_ok=True)
    wheel = root / "example.whl"
    source = root / "example.tar.gz"
    wheel_files = {f"prediction_market/research/{name}": "# source\n" for name in distribution.RESEARCH_FILES}
    wheel_files["example.dist-info/entry_points.txt"] = "[console_scripts]\nprecedge-research = prediction_market.research.cli:main\n"
    wheel_files.pop(missing, None)
    if extra:
        wheel_files[extra] = "should never be shipped"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, text in wheel_files.items():
            archive.writestr(name, text)
    source_files = {
        "pyproject.toml", "README.md", "docs/research_rollout.md", "docs/ci_cd.md",
        "scripts/validate_distribution.py",
        "tests/fixtures/oddsportal_sample.html",
        *(f"src/prediction_market/research/{name}" for name in distribution.RESEARCH_FILES),
    }
    source_files.discard(missing)
    with tarfile.open(source, "w:gz") as archive:
        for name in sorted(source_files):
            content = b"fixture\n"
            info = tarfile.TarInfo(f"example/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        if link:
            info = tarfile.TarInfo("example/linked-source")
            info.type = tarfile.SYMTYPE
            info.linkname = "/outside"
            archive.addfile(info)
    return wheel, source


def test_complete_distributions_record_content_hashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wheel, source = artifacts(tmp_path)
    monkeypatch.setenv("GITHUB_SHA", "fixture-commit")
    report = distribution.validate(tmp_path)
    assert report["checkout_sha"] == "fixture-commit"
    assert [row["sha256"] for row in report["files"]] == [
        hashlib.sha256(path.read_bytes()).hexdigest() for path in (wheel, source)
    ]
    assert distribution.main(["--dist", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "build-manifest.json").read_text()) == report
    assert wheel.name in (tmp_path / "SHA256SUMS").read_text()


@pytest.mark.parametrize("missing", [
    "prediction_market/research/cli.py", "example.dist-info/entry_points.txt",
    "docs/ci_cd.md", "tests/fixtures/oddsportal_sample.html",
])
def test_incomplete_distributions_fail_validation(tmp_path: Path, missing: str) -> None:
    artifacts(tmp_path, missing=missing)
    with pytest.raises(ValueError, match="missing"):
        distribution.validate(tmp_path)


@pytest.mark.parametrize("extra", [
    "data/trades.csv", "reports/private.json", ".env", "infra/state.json",
    "../escape.py", "/absolute.py",
])
def test_operational_data_and_unsafe_paths_are_rejected(tmp_path: Path, extra: str) -> None:
    artifacts(tmp_path, extra=extra)
    with pytest.raises(ValueError, match="archive path|operational file"):
        distribution.validate(tmp_path)


def test_source_links_and_ambiguous_build_outputs_are_rejected(tmp_path: Path) -> None:
    artifacts(tmp_path, link=True)
    with pytest.raises(ValueError, match="links"):
        distribution.validate(tmp_path)
    artifacts(tmp_path)
    (tmp_path / "another.whl").write_bytes(b"extra")
    with pytest.raises(ValueError, match="exactly one"):
        distribution.validate(tmp_path)
