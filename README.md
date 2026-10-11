# h2h-dev-tools

Shared development tools for H2H repositories. The Python package owns common
implementations; each consumer commits a verified source snapshot so its hooks
run offline against the code present in that repository's Git tree.

## Install and synchronize

Use Python 3.14 or newer. Install the package in a development environment:

```bash
python -m pip install h2h-dev-tools==0.3.1
```

For an unpublished candidate, install an explicitly supplied wheel instead:

```bash
python -m pip install /absolute/path/to/h2h_dev_tools-0.3.1-py3-none-any.whl
```

Declare the components the consumer actually uses in its `pyproject.toml`:

```toml
[tool.h2h-dev-tools]
components = ["change-scope", "dependencies", "documentation", "git-flow"]
```

Then generate or update the snapshot:

```bash
h2h-dev-tools sync --repository /absolute/path/to/consumer
h2h-dev-tools verify --repository /absolute/path/to/consumer
```

Selection is explicit. The generator includes each selected component's complete
dependency closure and removes previously verified files that are no longer
selected. Verification rejects stale selection, modified files and unknown files.
Python modules and shell resources share the same hash-bound manifest.

When upgrading across releases that change a component's dependency closure,
supply the wheel that produced the existing snapshot explicitly:

```bash
h2h-dev-tools sync --repository /absolute/path/to/consumer \
  --previous-wheel /absolute/path/to/h2h_dev_tools-0.2.2-py3-none-any.whl
```

Use a trusted wheel from the exact previous build; matching version strings alone
are insufficient. The updater reads its distribution metadata and source bytes
without executing its code. Every declared existing file must match that wheel
and its manifest hash. Unknown files, local modifications, symlinks and missing
declared files block replacement. The tool rechecks ownership before writing.
The supplied wheel proves which existing files may be replaced or removed; it
does not certify that the old component graph is complete or passed a gate.
An absent file also omitted from the old manifest may be restored by this explicit
repair. The new snapshot must always pass the current complete closure verifier.
Normal `verify`, hooks and `sync` without `--previous-wheel` retain their strict
current-closure checks; none executes a compatibility fallback or downloads a wheel.

| Component | Responsibility |
| --- | --- |
| `atomic-files` | Atomic byte publication with explicit replace or no-replace semantics; callers own JSON and directory policy |
| `change-scope`, `documentation` | Exact Git candidate classification and documentation checks |
| `dependencies`, `dependency-audit` | Declared dependencies, registry candidates and reviewed audit receipts |
| `version-policy` | Task-level release impact, version and audit validation |
| `primary-branch`, `git-flow`, `hooks`, `environment` | Local branch selection, safe merge lifecycle, hook installation, commit-message validation and environment rebuild |
| `publish-version` | Version transition across a complete push |
| `distribution-check` | Installed requirements, RECORD and import-origin validation |
| `offline-wheel` | Temporary candidate-wheel environment with development dependencies and a local smoke callback |
| `process` | Owned process trees, deadlines, signal deferral and Windows Job start gates |
| `git-environment` | Isolate test children from inherited Git hook repository authority |
| `pytest` | Fresh source and assertion caches with explicit import roots for pytest and local xdist workers |
| `formal` | Pinned TLC download and offline host/Docker execution |
| `code-review` | Exact-candidate Codex review and offline evidence verification |

Schema-1 snapshots from 0.1 use a fixed payload and are not accepted by normal
schema-2 verification or sync. After declaring components, convert once:

```bash
h2h-dev-tools migrate-v1 --repository /absolute/path/to/consumer
```

The converter verifies the complete old manifest, file set and hashes before
updating generated files. It preserves unrelated files and rejects edited or
incomplete snapshots. This conversion changes development tools only; application
databases, archives and runtime environments do not need conversion.

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

Python consumer entry points must source-bootstrap before their first shared
import (also before `runpy.run_module`):

```python
import runpy
import sys
from pathlib import Path

repository = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repository / "scripts"))
package = repository / "scripts/_shared/h2h_dev_tools"
runpy.run_path(str(package / "source_loader.py"))["load_package"](
    package, "_shared.h2h_dev_tools"
)
```

This entry verifies the complete snapshot and directly compiles the verified
source bytes, including the package initializer and relative imports. Python
bytecode caches may exist but are never executed by this loader; `-B` alone only
prevents new cache writes. Repeated bootstrap of the same namespace, directory
and bytes preserves existing module and class identity. A changed source set,
conflicting directory or package imported before bootstrap is rejected; start a
fresh process after an update. The namespace's parent remains repository-owned.
Toolkit checkout development uses its `scripts/*.py` source entries, including
editable environments. Snapshot synchronization uses a CLI installed from an
explicit immutable release or candidate wheel, not an editable CLI. Ordinary
installed-wheel imports retain Python's standard import/cache behavior.

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

Thin repository entry points supply their own root and policies. Core schema
checks, adapter tests, pytest selectors, timeout budgets, formal targets and
deployment acceptance remain owned by their original repositories. Primary-branch
detection, hook installation and environment rebuild run before a `.venv` exists;
they do not import the Python package.

Commit-message hooks select the `hooks` component and invoke
`bash scripts/_shared/h2h_dev_tools/commit-message.sh ROOT MESSAGE_FILE PATTERN`.
Each repository declares its own Bash regular expression. The shared helper reads
the first line and rejects unreadable messages and invalid or empty patterns.
A subject starting with `Merge` followed by a space is exempt only while Git
verifies `MERGE_HEAD`. Relative message paths resolve against the caller's working
directory; the helper does not change it.

Test launchers select the `git-environment` component and call
`without_local_git_environment(environment, repository=repository_root)` when
building a test child's environment. Shell entry points can execute
`python scripts/_shared/h2h_dev_tools/git_environment.py --repository ROOT -- COMMAND`.
Both use Git's `rev-parse --local-env-vars` inventory, reject failed or malformed
queries and preserve other environment values. On POSIX the CLI replaces itself
with the test command; on Windows it waits for the child and returns its exit code
inside the existing Job. Neither path creates another process owner.
Apply this only to test children:
gate checks before and after pytest must retain the real candidate's Git environment.

Pytest entry points select the `pytest` component, source-bootstrap their package
as above, then call its `pytest_source.main()` before importing pytest or plugins.
The helper preserves pytest arguments and uses a unique empty bytecode-cache
prefix with cache writes disabled. It prepends the verified package's import
root to `sys.path` and `PYTHONPATH`, preserving the existing environment suffix.
An optional xdist gateway hook revalidates and source-loads the same package in
each local worker before its pytest plugins and conftests load. It checks the
controller's original source hashes, captured
before pytest or its plugins are imported. A valid replacement snapshot is still
a different generation and is rejected. Worker bootstrap and snapshot verifier
code are compiled from the exact bytes checked against those hashes; subsequent
module imports recheck their expected hashes before compiling their bytes.
Path ordering alone cannot protect namespace packages from another checkout's
regular package.
The settings are restored and the temporary directory removed when pytest exits.
This covers direct local Python workers; remote or chained
workers need their own explicit source distribution and are not supported by this
entry's source guarantee. Each repository still owns selectors, dependencies,
process ownership and deadlines; the toolkit has no runtime pytest dependency.

Version paths are declared in `[tool.h2h.version]`; dependency inventory comes
from the project manifests. Adding, changing or removing only the `dev` optional
extra does not require a release; other named extras, including empty extras,
remain release metadata. Shared version evaluation considers only commits that
actually change release surface. Commit type and `!` signals come only from each
commit's subject; quoted subjects in its body do not affect the version.
An explicit `BREAKING CHANGE:` footer remains an independent breaking signal.
Where enabled, `Version-Impact: none` and its
nonempty `Version-Reason` exempt only their own commit; later release changes
still require the task's single version bump, and extra candidate changes cannot
borrow that exemption. Exemptions that cannot be established from a clean
automatic three-way merge are rejected. Hook installation refuses to replace an
existing third-party hook directory. Rebuild validates explicit source overrides before
replacing the environment. Relative `--source PACKAGE=SOURCE` paths are resolved
from the caller's working directory, including when the rebuild script is invoked
from outside its repository. Local wrappers must preserve that working directory
while locating the repository. The shared rebuild captures it before changing to
the repository, then passes it as `--source-base` to the source resolver. The
repository's `.[dev]` installation, npm and after-build callback still run from
the repository root; absolute paths, URLs and index sources keep their meaning.
Repository path changes use command-local `CDPATH=` so an interactive shell's
search paths cannot redirect tooling or contaminate machine-readable output.
The caller's environment remains available to invoked commands and callbacks.
Merge checks dirty worktrees, in-progress Git operations
and stable refs; it runs the local candidate review/gate and aborts on failure.
Primary worktree discovery queries the exact branch with NUL-delimited fields;
paths containing line breaks are preserved. Repository-local merge, gate and
hook entries must likewise preserve path bytes when determining the Git root.
Both merge strategies use the captured task commit, preserving its branch name
in the merge message. Version checks use the actual `MERGE_HEAD`; before Git
creates that file for a direct merge hook, `WORKFLOW_MERGE_TASK_REF` must contain
the full immutable commit OID. Mutable branch names are no longer accepted there.
Already-integrated tasks use checked cleanup without another merge or gate.
Cleanup rechecks that the original task tip remains unchanged and is contained in
primary before removing a linked worktree. Branch deletion uses primary's checked
out HEAD as its ancestry authority, independent of the task's configured upstream.
A temporary `reference-transaction` hook rechecks the actual task tip while Git
holds its deletion lock, forwarding every event and its unchanged input to the
existing hook. This requires Git 2.28 or newer. A task advanced during review or
cleanup is retained, and cleanup failure never rolls back an already successful
merge.
Workflow commands that change Git state use repository-local Python to own their
hook process trees, including checkout, merge, failure recovery and task-branch
deletion. Starting with 0.3.0, consumers of the `git-flow` component must create
their local `.venv` before merging or cleaning up an already-merged task. Run
the repository's `scripts/rebuild-env.sh` once if it does not exist; branch
detection and bootstrap still work without it. Linked-worktree cleanup also
requires the primary checkout's `.venv`, so removing the task worktree cannot
remove the interpreter needed to finish cleanup. `--cleanup-only` remains
available to retry cleanup after
a successful merge without repeating review. Existing application data does not
need conversion.
This contract does not cover custom helpers used by separate read-only Git
queries, such as an external filesystem monitor, or deliberately detached
processes outside the owned POSIX group.
An interruption waits for that cleanup before aborting a pending candidate; a
commit already completed is retained. Unproven process cleanup retains the
candidate and task for inspection instead of letting a surviving hook race an abort.
Normal workflow commands have no automatic timeout and can be interrupted through
the outer wrapper. During failure recovery, each abort or checkout command has
a separate 10-second hard budget including the existing 6-second teardown reserve.
Recovery Git output is captured and replayed after its owner finishes, up to
64 KiB with a truncation notice when the destination accepts it. Recovery owner
startup errors are suppressed; a failed startup has no cleanup receipt and blocks
further mutation. Owner progress, replay and shell failure messages use the shared
best-effort diagnostic writer described below, so a closed or stalled pipe cannot
delay recovery cleanup or receipt publication. Once the owner is prepared,
this also covers ordinary failures before the shell enters its recovery trap.
Preflight messages before an owner exists retain normal stdio behavior; no Git
mutation or owned child has started at that point.
A failed abort prevents the recovery checkout; recovery never rolls back an
already completed merge.

The `code-review` component owns the reviewer implementation; each repository's
`scripts/review-code.py` calls `code_review.main(repository_root)`, and its merge
entry point delegates to the shared `review-merge-candidate.sh` with that root.
Full changes require online review of the staged two-parent candidate before
commit, then offline verification before and after the existing full gate.
Documentation-only changes retain their documentation gate without online review.
Documentation export rejects backslashes and colons in Git path components on
every platform: those otherwise valid POSIX names have Windows separator, drive
or alternate-stream semantics. Export also checks resolved destination containment
and refuses filesystem name collisions instead of overwriting another document.
Review reads the repository's root `AGENTS.md`; findings, incomplete results,
timeouts and invalid output block the merge and preserve the task branch.
Each review invocation sets `notify=[]`: asynchronous completion notifications
can otherwise outlive the CLI and violate its process ownership contract.
This override applies only to that invocation. Model preferences, authentication,
plugins, MCP servers and security hooks retain their existing configuration;
their descendants remain subject to the same cleanup requirements.
The merge launcher first owns classification in a dedicated process group and
proves that group empty. Only a full-profile candidate then requires the local
review entry and starts the reviewer, CLI-version preflight and Codex descendants
in their next owned group. Documentation needs neither the review entry nor Codex.
The owner is exported from the actual staged candidate after the three-way merge,
so an independently updated primary does not require rebasing the task. The owner
holds the candidate review lock through cleanup and publication. Managed reviewers
write only pending evidence; the owner publishes it after successful group exit
and fresh candidate/coverage validation. The managed reviewer validates its direct
parent, group leader and complete owner source closure before sharing ownership.
It does not create a separate Codex session that could survive
the reviewer's unexpected death. The launcher proves group cleanup before Git
aborts; missing cleanup evidence retains the candidate for inspection. Standalone
review instead owns its CLI-version and Codex command groups directly. Even a
successful leader exit requires bounded proof that its group is empty before
standalone review evidence can be published.

For long-running review automation, redirect output to a log file and cancel with
TERM sent to the outer merge wrapper. A terminal executor that forcibly kills the
owner cannot run cleanup handlers. Retain the candidate and inspect the surviving
processes; prove their cleanup before manually aborting that candidate.

The first review covers the complete candidate repository, including unchanged
code, tests and configuration. Only a passing full-repository candidate that
actually becomes a commit on the primary's first-parent history establishes a
baseline. Later reviews use the primary-to-candidate diff only while that baseline
and the intervening review chain remain valid. Documentation transitions are
checked by the exact-tree classifier. Missing evidence, unreviewed code in that
history, or changed review policy/implementation requires a new repository review.
Aborted candidates and old difference-only receipts cannot establish a baseline.

Review evidence in Git common metadata `h2h-review` binds the exact tree, ordered
parents, scope, baseline, root policy, local review entry points and shared reviewer,
process and classification sources. It is separate
from test evidence. Run needs an authenticated `codex` CLI and POSIX process-group
support; offline verify also works on Windows. The default timeout is 900 seconds
(`--timeout-seconds` accepts 1..3600). Set `H2H_CODE_REVIEW_TIMEOUT_SECONDS=3600`
when invoking `scripts/git-flow-merge.sh` to allow a longer first repository
review; the same CLI validation applies. Old Core `h2hdb-review` receipts are not
converted: run the new review to establish evidence for the current implementation.

Process-owner and code-review human diagnostics use nonblocking writes on POSIX
descriptors and Windows pipes. A closed or full pipe can discard diagnostic text;
it cannot change the review result or leave buffered output to fail after receipt
publication. It cannot delay
cancellation, timeout cleanup or completion. Each write restores the original
blocking flags, but inherited descriptors share those flags while the write runs;
this is not isolation from concurrent writers during failure teardown. Child data
streams are not converted into diagnostic output. Standard memory-only text
capture is supported; unknown sinks without a descriptor are skipped. Windows
regular files and consoles retain native text output. This does not promise
nonblocking console or filesystem I/O. Native Windows validation belongs to its
platform CI; mocked branch tests do not substitute for that evidence.

The low-level process launch API is silent and accepts file-backed stdout/stderr.
Its caller must defer signals until it owns the returned process. `run_command`
provides that lifecycle, reserves cleanup time, rejects surviving descendants and
verifies the final absolute deadline. Local callers select their budgets and
signals. POSIX children that deliberately detach and Docker-daemon-owned resources
are outside the owned-tree guarantee. Canonical nested commands are registered
before their separate POSIX sessions start. Closing admission prevents a new
phase from racing cleanup; an outer owner cannot send SIGKILL to its inner owner
or claim an empty tree until that owner's recursive cleanup obligations are released.
Scope locks share the caller's deadline. A killed inner owner, stale capability,
damaged ledger or exhausted deadline fails closed and retains the scope path for
inspection; a merge then retains its candidate and task instead of aborting while
unproven children may remain. The private inherited scope capability is local
coordination state, not a persistent receipt or protection against malicious
same-user modification. Windows behavior also runs in the dedicated
Windows workflow; a POSIX test run does not substitute for it.

The `offline-wheel` component owns temporary build/install directories for
consumer wheel smoke checks. A consumer supplies its own validation script:

```bash
.venv/bin/python scripts/_shared/h2h_dev_tools/offline_wheel.py \
  --repository . --smoke-script scripts/check-installed-wheel.py
```

Arguments following `--` are forwarded unchanged to that script. The helper
builds fresh distributions with the repository's development Python and build
backend, requires exactly one wheel, then installs only that wheel in a temporary
venv with `uv` in offline mode. It explicitly adds the development site for the
callback's dependencies and runs the callback with `python -I` from the temporary
directory. The callback owns package import/metadata checks, CLI checks and domain
oracles; arguments naming repository files must therefore be absolute paths.
Success and ordinary failure both remove the temporary environment. A surrounding
runner still owns the aggregate deadline and process-tree termination policy.
This checks the candidate artifact with development dependencies; it does **not**
prove clean dependency resolution or compatibility of an index-selected cohort.
The toolkit's own isolated resolver smoke and hbrowser's target-directory smoke
have different contracts and do not use this component.

## Develop this repository

```bash
scripts/rebuild-env.sh
scripts/install-git-hooks.sh
scripts/check-fast.sh
scripts/check-full.sh
```

For focused tests, use `.venv/bin/python scripts/run-pytest.py <pytest arguments>`.
The full gate and Windows CI use this same source entry. It loads the checkout's
toolkit source before pytest, plugins or conftest can import the package, so a
matching timestamp/size `.pyc` cannot replace the candidate implementation.
The shared `pytest` component also isolates pytest assertion caches and local
xdist worker imports for the full invocation.
Python's direct `-m pytest` entry does not provide this source guarantee; `-B`
only prevents cache writes. Installed wheel smoke keeps ordinary isolated imports.
The full gate still removes Git-local authority only from its pytest child;
the surrounding checks retain the candidate's Git environment.

The expected primary branch is `master`. Create a dedicated task branch before
changing tracked files, then finish with `scripts/git-flow-merge.sh`. The merge
flow reviews full candidates before the hook verifies review evidence, version
impact and the applicable documentation or full gate. `scripts/format.sh` applies formatters;
the check commands are read-only. Ruff and strict mypy use `pyproject.toml`.

The full gate builds fresh distributions, then installs the sdist-derived wheel
in a new environment with normal, offline pip resolution. Only the installed
`packaging` distribution is copied into that environment as dependency evidence;
no editable project or development-site fallback is used. The smoke verifies
package and metadata locations, runtime requirements, both CLI entry points, and
real snapshot export plus standalone verification.
Console smoke isolates Python environment overrides and compares every exported
snapshot file with the package bytes installed in the smoke environment.
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
