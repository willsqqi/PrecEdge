# CI and downloadable builds

The CI workflow replaces the previous GCP-oriented pipeline with checks for the
local research package. It runs on every pull request, pushes to `main`, and
manual dispatch. No cloud credentials or repository secrets are needed.

This change is reviewed in a separate draft PR based on the phase 1 research
branch. Merge the foundation PR first, then retarget/rebase this PR onto `main`
before merging it. The existing three-phase rollout still applies.

## What runs

| Job | Check |
| --- | --- |
| Code and workflow checks | Fatal Python errors across source/tests, standard lint on research and distribution-validation code, and actionlint on all workflow files |
| Test matrix | Complete offline suite on Linux/Python 3.11, 3.12, 3.13 and macOS/Python 3.12 |
| Synthetic benchmark | Recover the planted matches with no false positives on each matrix runner; preserve the machine/config/timing evidence |
| Build and verify distributions | Build sdist and wheel, check metadata, inspect their contents, and smoke-test the installed wheel in a separate empty directory |
| Deliver tested builds | Publish a tested build artifact only after code checks, every matrix entry and packaging succeed |
| CI passed | Fail if any required job fails, is cancelled or is skipped |

The `test` extra installs pytest without the notebook, optional cloud or dashboard
extras. Test jobs explicitly disable the three opt-in live tests. Passing this
suite demonstrates offline behavior, not API compatibility or a profitable live
strategy. Benchmark timings are reported, never enforced as latency thresholds.

Python's broad existing dependency ranges remain unchanged. Runtime dependencies
are resolved from those ranges, so this is not a frozen production environment.
The build and lint tools have pinned versions, actions use immutable commit SHAs,
and the downloaded actionlint archive has a fixed SHA-256 checksum. Pip caching,
timeouts and cancellation of superseded runs keep CI bounded.

## Download a build

Open **Actions -> CI -> a successful run -> Artifacts** and download the artifact
named `tested-distributions-<checkout SHA>`. It contains:

- The Python wheel and source archive.
- `SHA256SUMS` and `build-manifest.json`, binding the files to their content hashes
  and the tested checkout commit.
- `installed-benchmark.json`, produced by the wheel-installed CLI with no source
  checkout or runtime dependencies available.
- `validation.json`, recording the successful CI jobs and workflow run.

The tested artifact and per-platform `evidence-*` reports are retained for 30
days. Evidence includes pytest JUnit XML and synthetic benchmark JSON. An
intermediate `candidate-distributions` artifact lasts one day and is not a
successful-CI certificate. Failed tests can still upload diagnostic evidence;
they cannot produce the tested artifact.

Pull-request builds test GitHub's proposed merge checkout. Their manifest SHA
therefore identifies that merge commit, not necessarily the PR's head commit.
A successful `main` build identifies the actual merged commit. Run attempts are
recorded separately in the validation record.

## Install and verify

After extracting the tested artifact:

```bash
# Linux:
sha256sum --check SHA256SUMS
# macOS:
shasum -a 256 --check SHA256SUMS

python3 -m venv .venv
.venv/bin/python -m pip install ./poly_x_kalshi-*.whl
.venv/bin/precedge-research benchmark --output matcher-benchmark.json
```

The wheel smoke test uses `--no-deps` because the research matcher/benchmark needs
only the standard library. Normal installation resolves the project's full data
dependencies, needed for the candidate importer and existing scanner commands.

## Run the checks locally

```bash
python -m pip install ".[test]" build==1.6.1 twine==7.0.0 ruff==0.17.0
python -m pytest -q --junitxml=reports/ci/junit.xml
python -m prediction_market.research.cli benchmark --output reports/ci/matcher-benchmark.json
ruff check --isolated --select E9,F63,F7,F82 src tests scripts/validate_distribution.py
ruff check --isolated --select E4,E7,E9,F src/prediction_market/research tests/test_research_foundation.py scripts/validate_distribution.py tests/test_distribution.py
python -m build
python -m twine check --strict dist/*.whl dist/*.tar.gz
python scripts/validate_distribution.py --dist dist
```

Build from a clean checkout. If `dist/` contains old wheel/archive versions,
remove those generated files before rebuilding; validation deliberately rejects
multiple ambiguous build outputs. Distribution checks reject operational data,
reports, `.env` files, cloud state, unsafe paths and source links. Source packages
include the research/CI documentation and the HTML fixture needed by the tests.

## Merge protection and deployment boundary

After this workflow is merged into `main`, the recommended required check in a
GitHub branch rule is **CI passed**. Branch protection is a separate repository
setting; this PR does not enable it or merge itself. This single aggregate check
keeps the rule stable when the test matrix changes.

CD here means delivery of downloadable, validated build artifacts. The workflow
does not create a GitHub release, publish to PyPI, run a hosted service, provision
cloud resources or place orders. The old OIDC/GCP deployment job is removed;
historical Terraform and Docker files remain as references. Any future hosting
deployment should be a separately reviewed workflow with a selected destination
and its own credentials and approval gate.

References: [GitHub Actions concurrency](https://docs.github.com/en/actions/concepts/workflows-and-actions/concurrency),
[artifact upload](https://github.com/actions/upload-artifact),
[Python packaging guide](https://packaging.python.org/en/latest/tutorials/packaging-projects/).
