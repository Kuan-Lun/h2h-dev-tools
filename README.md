# h2h-dev-tools

Shared development tools for H2H repositories. The Python package owns common
implementations; each consumer commits a verified source snapshot so its hooks
run offline against the code present in that repository's Git tree.

## Install and synchronize

Use Python 3.14 or newer. Install the package in a development environment:

```bash
python -m pip install h2h-dev-tools
h2h-dev-tools sync --repository /absolute/path/to/consumer
h2h-dev-tools verify --repository /absolute/path/to/consumer
```

For an unpublished candidate, install an explicitly supplied wheel instead:

```bash
python -m pip install /absolute/path/to/h2h_dev_tools-0.1.0-py3-none-any.whl
```

Review the resulting consumer diff, run that repository's checks and commit the
snapshot update on its task branch. Sync is an explicit source update; it does
not commit, merge, push or publish. Consumers do not need this installed package
to run their committed snapshots, and do not add it to runtime dependencies.

The consumer snapshot lives in `scripts/_shared/h2h_dev_tools/`. Its tracked
`manifest.json` identifies the toolkit version and source payload hashes; these
are payload identities, not the wheel archive hash. Generated snapshots must
never be edited manually. A consumer checks its local snapshot without network
or an installed copy of the tool:

```bash
python scripts/_shared/h2h_dev_tools/bundle_verify.py --repository .
```

The manifest detects changed or incomplete source files; it is not a signature
or proof of the publisher's identity. Review the package source and consumer diff
before adopting an update.

Sync writes individual files before replacing the manifest. An interrupted update
can therefore leave a bundle that verification and later sync deliberately reject.
Inspect and preserve any pending edits first. If this consumer already has a
committed bundle, restore only that generated directory from the intended known
good commit, then run sync again; do not restore the whole repository or overwrite
unrelated changes. For example, after confirming `HEAD` contains the bundle you
intend to recover:

```bash
git diff -- scripts/_shared/h2h_dev_tools
git diff --cached -- scripts/_shared/h2h_dev_tools
git restore --source=HEAD --worktree -- scripts/_shared/h2h_dev_tools
h2h-dev-tools sync --repository .
```

The restore command changes only working files; after sync, review and stage the
complete updated bundle using the consumer's normal workflow.

For an interrupted first sync with no committed bundle, preserve the incomplete
directory outside `scripts/_shared/` after inspecting its contents, then sync into
the vacant destination. The tool never treats unknown files as disposable.

The shared modules cover dependency manifest encoding, Git change classification,
exact-candidate documentation checks, declared CI dependency selection and publish
version transitions. Thin repository entry points supply their own root and
policies. Core schema checks, adapter tests, process deadlines and deployment
acceptance remain owned by their original repositories.

## Develop this repository

```bash
scripts/rebuild-env.sh
scripts/install-git-hooks.sh
scripts/check-fast.sh
scripts/check-full.sh
```

The expected primary branch is `master`. Create a dedicated task branch before
changing tracked files, then finish with `scripts/git-flow-merge.sh`. The merge
hook validates the real staged candidate and its version impact before running
the applicable documentation or full gate. `scripts/format.sh` applies formatters;
the check commands are read-only. Ruff and strict mypy use `pyproject.toml`.

The full gate builds fresh distributions, then installs the sdist-derived wheel
in a new environment with normal, offline pip resolution. Only the installed
`packaging` distribution is copied into that environment as dependency evidence;
no editable project or development-site fallback is used. The smoke verifies
package and metadata locations, runtime requirements, both CLI entry points, and
real snapshot export plus standalone verification.
This is an offline distribution check, not a registry-backed clean-resolution test.

## Releases

`pyproject.toml` is the sole project-version source. The initial version is
`0.1.0`; subsequent compatible releases increment the third component, and
breaking changes increment the second component while it is below 1.0.
The shipped development tools and snapshot format are release surface even
though consumers use them only for development.

Before committing a release version, review dependency candidates and generate
the version-bound audit evidence:

```bash
.venv/bin/python scripts/audit-dependencies.py \
  --review-note "Release notes and compatibility validation results"
```

GitHub's `publish.yml` workflow uses PyPI Trusted Publishing with environment
`pypi`. Normal default-branch pushes publish only when the version increases over
the whole push. Creating the branch does not publish. The first release can use
an explicitly authorized workflow dispatch with the exact project version; it
still requires the default branch and builds and verifies distributions first.
The PyPI publisher must match owner `Kuan-Lun`, repository `h2h-dev-tools`, workflow
`publish.yml` and environment `pypi`. Configuring this workflow does not execute a
release or validate the remote publisher configuration.

Licensed under GPL-3.0-only; see [LICENSE](LICENSE).
