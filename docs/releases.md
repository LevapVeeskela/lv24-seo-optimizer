# Releases

This page is the release runbook for `lv24-seo`. It covers the versioning contract, the tag-driven
PyPI workflow, the manual MCP Registry step, rollback and cost.

The pipeline is **release plumbing only**: it builds the sdist/wheel and publishes them to PyPI with
OIDC Trusted Publishing, and publishes the thin `@lv24/seo` npm launcher when `NPM_TOKEN` is present.
Nothing in this repository publishes automatically to the MCP Registry, GHCR or any paid service. The
first public release is `0.1.0`.

| Artifact | Built by | Published by | Cost |
|---|---|---|---|
| PyPI sdist + wheel (`lv24-seo`) | [`.github/workflows/release.yml`](../.github/workflows/release.yml) | `publish-pypi` job, OIDC | $0 (public repo, standard runner) |
| npm launcher (`@lv24/seo`) | `packages/npm` | `publish-npm` job, `NPM_TOKEN` | $0 (public npm package) |
| `server.json` (MCP Registry) | this repository | `mcp-publisher`, manually | $0 |
| GHCR image (optional) | disabled job in `release.yml` | not enabled | public package storage/egress free; build minutes still count |

## Versioning policy

Versions follow [Semantic Versioning 2.0.0](https://semver.org/) with the usual pre-1.0 relaxation:

* **`0.MINOR.PATCH` (current).** `MINOR` may contain breaking changes; `PATCH` contains only fixes and
  additive, non-breaking changes. The first public release is `0.1.0`.
* **`1.0.0` and later.** Standard SemVer: breaking changes only in `MAJOR`.

The version must agree in six places, and the release workflow refuses a tag that disagrees with the
package metadata:

1. `pyproject.toml` → `[project] version`
2. `src/lv24_seo/__init__.py` → `__version__`
3. `server.json` → root `version`
4. `server.json` → `packages[0].version`
5. `packages/npm/package.json` → `version`
6. The Git tag `vX.Y.Z`

### What counts as breaking

The public contract is what users and agents can observe:

* **CLI.** Removing or renaming a command, subcommand or flag; changing a flag's meaning or a default
  that changes which data is selected; changing documented exit codes; removing `--json` from a
  command.
* **JSON envelope.** Removing or renaming top-level or per-command keys, changing a value's type or
  nullability, making an optional field required, or changing documented `error.code` values.
  Adding new keys is not breaking.
* **MCP surface.** Tool, resource and prompt names; required input fields, enums and types; output
  fields that consumers read. `schemas/tools.json` is the contract and is regenerated for every
  release. Adding a tool or an optional input is not breaking.
* **Configuration.** Removing or renaming a `seo.toml` key or an `LV24_SEO_*` environment variable;
  changing a default so existing configs behave differently; dropping Python 3.11 (the floor in
  `pyproject.toml`).
* **Stored data.** Dropping or rewriting user data without a migration, or making a newer SQLite
  schema unreadable by an older CLI without saying so.

Not breaking: new commands/tools/config keys with safe defaults, new optional flags, additive JSON
fields, report wording and internal refactors. HTML/Markdown report layout is best-effort; the JSON
envelope and `schemas/tools.json` are the stable surfaces.

Deprecate before removing: announce the change in `CHANGELOG.md`, warn at runtime for at least one
`MINOR` release, then remove it in the next `MINOR` (pre-1.0) or `MAJOR` (post-1.0).

## One-time setup

Complete these before the first tag. They are not automated by `release.yml`.

### 1. PyPI Trusted Publisher

The workflow uses OIDC, not a password or API token. On PyPI, open the project (or add a **pending
publisher** if the project does not exist yet) and configure exactly:

| Field | Value |
|---|---|
| PyPI project name | `lv24-seo` |
| Owner | `LevapVeeskela` |
| Repository name | `lv24-seo-optimizer` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

The environment name is part of the trust relationship: the workflow declares `environment: pypi`
and the PyPI form must match. A GitHub environment with the same name is created on first use; adding
required reviewers there is optional and free, but it turns every publish into a manual approval. If
you add deployment branch/tag rules to that environment, allow `v*` tags — a branch-only rule blocks
tag deployments.

Do **not** add a `PYPI_API_TOKEN` secret. The publish job has `id-token: write` and nothing else.

`uv.lock` must be committed (`git ls-files --error-unmatch uv.lock`). CI and the local release checks
rely on it for reproducible installs; without it `uv sync` resolves fresh versions and the tested
dependency set can drift from the tagged commit.

### 2. MCP Registry ownership marker

The MCP Registry verifies PyPI ownership by looking for this exact string in the published package
README (PyPI renders `README.md` as the project description; an HTML comment is preserved):

```markdown
<!-- mcp-name: io.github.LevapVeeskela/lv24-seo -->
```

Check it before building the first release:

```bash
grep -F 'mcp-name: io.github.LevapVeeskela/lv24-seo' README.md
```

If the command prints nothing, add the line to `README.md` **before** `uv build` and before tagging.
It must be part of the published `0.1.0` README; adding it in a later patch means the registry cannot
verify `0.1.0` retroactively.

### 3. `mcp-publisher`

Install the official CLI from the
[MCP Registry quickstart](https://modelcontextprotocol.io/registry/quickstart) (Homebrew or the
release archive). Publishing is intentionally manual: it needs an interactive GitHub device login and
the repository stores no registry token.

GitHub login grants the `io.github.LevapVeeskela/*` namespace. The server name in `server.json` must
keep exactly one slash and match that namespace. The PyPI package entry passes `mcp serve` through
`packageArguments`, so clients launch `uvx lv24-seo mcp serve` rather than the bare CLI help.

### 4. npm (optional, `@lv24/seo`)

The npm package is a **thin launcher** (`packages/npm`): it forwards every argument to
`uvx --from lv24-seo lv24-seo …`. It contains no SEO logic, so publishing it never requires a Python
release to be repeated.

The `@lv24` organisation already exists (it hosts the `lv24-brain` packages). Authentication uses
**npm Trusted Publishing (OIDC)** — no long-lived token is stored in this repository.

One-time setup (interactive, needs npm 2FA):

```bash
npm login --auth-type=web
npm trust github @lv24/seo \
  --file release.yml \
  --repository LevapVeeskela/lv24-seo-optimizer \
  --allow-publish
```

Notes:

* npm requires CLI **≥ 11.5.1** and Node **≥ 22.14**; the release job pins Node 24 for that reason.
* A freshly created trust relationship must complete its **first publish within 2 days**, otherwise it
  expires and must be recreated.
* `package.json` → `repository.url` must match the GitHub repository exactly (it does).
* Provenance is generated automatically by trusted publishing on a public repo.

Fallback if `npm trust` cannot target a package that does not exist yet: create a **Granular Access
Token** (read/write on `@lv24`, 2FA bypass) and store it as the repository secret `NPM_TOKEN`, then
publish the first version locally:

```bash
cd packages/npm
npm login
npm publish --access public
```

After the first publish, configure the trusted publisher for future releases and revoke the token.

npm versions are immutable: publish a new patch version if `0.1.0` already exists. An npm-only fix does
not require a new PyPI release or a new tag — bump `packages/npm/package.json` and publish manually.

## Release checklist

### 0. Preconditions

```bash
git switch main
git pull --ff-only
git ls-files --error-unmatch uv.lock
grep -F 'mcp-name: io.github.LevapVeeskela/lv24-seo' README.md
```

The latest `ci` run on `main` must be green (lint, typecheck, tests, schemas, gitleaks). The release
workflow builds but does **not** run tests, and `ci.yml` runs on branches/PRs, not on tags. Tag the
exact commit whose CI run is green.

### 1. Bump the version and changelog

Update every version field listed in [Versioning policy](#versioning-policy). Then turn the changelog
into a release entry, keeping a fresh empty section on top:

```markdown
## [Unreleased]

## [X.Y.Z] - YYYY-MM-DD

### Added
...
```

Also update `README.md` status/install text if it still says a release is "planned". The npm launcher
(`packages/npm`) is a wrapper only — see the npm section above for its separate publish path.

### 2. Regenerate the MCP schemas

```bash
uv run python -c "import json, pathlib; from lv24_seo.mcp.registry import tools_as_json; pathlib.Path('schemas/tools.json').write_text(json.dumps(tools_as_json(), indent=2, ensure_ascii=False) + '\n', encoding='utf-8')"
git diff -- schemas/tools.json
```

The Python one-liner writes UTF-8 on every OS; a shell `>` redirect in Windows PowerShell 5.1 would
write UTF-16 and corrupt the file. The diff must contain only intended MCP surface changes. CI's
`schemas` job regenerates the same content and fails on drift.

### 3. Quality gate

```bash
uv sync --extra dev
uv run pytest -q -m "not live and not network"
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

`live` and `network` tests require real tokens/internet and are excluded from CI; never run them as
part of a release gate unless you own the provider accounts.

### 4. Build and smoke-test the distributions

```bash
uv build
ls -lh dist/
uvx --from ./dist/lv24_seo-X.Y.Z-py3-none-any.whl lv24-seo version
```

Expected output: `lv24-seo X.Y.Z`. This proves the wheel contains the entry point before anything is
uploaded.

### 5. Commit, wait for CI, tag

```bash
git add pyproject.toml src/lv24_seo/__init__.py server.json schemas/tools.json CHANGELOG.md README.md
git commit -m "[SEO](release) - vX.Y.Z"
git push origin main
# Wait for the ci workflow on this commit to pass.
git tag -a "vX.Y.Z" -m "vX.Y.Z"
git push origin "vX.Y.Z"
```

Pushing the tag triggers `release.yml`. The build job re-checks that the tag, `pyproject.toml`,
`__version__` and `server.json` agree, then builds and uploads an artifact for 7 days. The `publish-pypi` job downloads
it and uploads to PyPI. A manual `workflow_dispatch` is a build-only dry run unless it is run on a
`v*` tag ref.

### 6. Watch the workflow

Actions → **release**, or:

```bash
gh run list --workflow release.yml --limit 1
gh run watch <run-id>
```

If the `pypi` environment has required reviewers, approve the deployment. A successful run ends with
the `publish-pypi` job green and “Successfully published” in the upload step. A failed upload cannot
be fixed by re-running the same tag: PyPI versions are immutable (see [Rollback](#rollback)).

### 7. Verify PyPI and `uvx`

```bash
uvx --from lv24-seo==X.Y.Z lv24-seo version
uvx --from lv24-seo lv24-seo version
curl -fsS https://pypi.org/pypi/lv24-seo/json \
  | python -c "import json,sys; print(json.load(sys.stdin)['info']['version'])"
```

Both `uvx` commands must print `X.Y.Z`; the unpinned one proves the new version is the resolver's
default. If `uvx` has a cached older build, add `--refresh`.

### 8. Publish the MCP Registry manifest (manual)

Only after PyPI has accepted the files — the registry verifies that the package version exists and
that its README contains the ownership marker:

```bash
mcp-publisher validate server.json
mcp-publisher login github
mcp-publisher publish
```

`validate` performs schema and semantic checks and needs network access; `publish` uses the login
token and the `io.github.LevapVeeskela/lv24-seo` name. Verify the entry:

```bash
curl -fsS "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.LevapVeeskela%2Flv24-seo"
```

Confirm the registry shows `version` `X.Y.Z`, `isLatest: true` and the package `lv24-seo` version
`X.Y.Z`. `0.1.0` is already published (`status: active`); every later release must bump `server.json`
and re-run `mcp-publisher publish` **after** PyPI accepts the version, otherwise the registry keeps
pointing at the previous release. If `validate` reports that the `$schema` URL is deprecated, update
`server.json` and this document in the same change; do not publish with a deprecation warning.

### 9. Post-release

* Create GitHub release notes for the tag (optional; PyPI remains the canonical artifact):
  `gh release create vX.Y.Z --title vX.Y.Z --notes-from-tag`.
* Update README install/status text and any docs that still describe the previous version.
* Announce only after PyPI and the registry are verified.

## Rollback

### A bad PyPI release

PyPI files are immutable. You cannot delete and re-upload the same version.

1. **Yank** the release in the PyPI UI: `https://pypi.org/manage/project/lv24-seo/releases/` → select
   the version → **Options → Yank release**, with a reason (for example, “0.1.0: broken MCP stdio
   handshake”). Yanking hides the version from new resolves but keeps it installable by exact pin, so
   existing lockfiles do not break.
2. Fix the defect on `main`, bump **PATCH**, and repeat the checklist. Never reuse the yanked version
   number.
3. If the release is compromised, yank it, rotate any provider credentials that could be affected
   (the pipeline stores no PyPI token), publish a fixed patch and follow `SECURITY.md`.

### Tag rules

**Never move a published tag** (`git tag -f`, `git push --force`). PyPI artifacts, provenance and
users' pins refer to the tagged commit; rewriting the tag makes the release unreproducible.

| Situation | Action |
|---|---|
| Tag pushed; workflow failed before PyPI accepted the upload | Prefer fix-forward with a new PATCH. If the tag was never published and never announced, you may delete the remote tag and re-tag the fixed commit. |
| PyPI accepted the files | Keep the tag. Yank on PyPI and publish a new PATCH. |
| Wrong commit tagged; nothing published | Delete the remote tag and tag the correct commit (no force-move of a published tag). |
| Version already exists on PyPI | Bump PATCH; do not delete or overwrite. |

Deleting a tag that never reached PyPI (use with care, and only before announcement):

```bash
git push origin :refs/tags/vX.Y.Z
```

### MCP Registry

Registry versions are additive: publish a new `server.json` version after the fixed PyPI release;
the previous version remains visible. If a version must be retired, use the publisher's `status`
command (check `mcp-publisher status --help` for the current flags) rather than editing published
metadata in place.

### Data and configuration

The SQLite store under `data/` is user data. Releases must ship forward migrations; there is no
automatic downgrade. Before upgrading, back up `data/seo.sqlite3` (and `-wal`/`-shm` if present). To
roll back a bad CLI, restore that backup and install the previous version explicitly with
`uvx --from lv24-seo==PREVIOUS`. Keep `seo.toml` in version control so config changes are reviewable.

## Cost

The policy is **zero spend**:

* **GitHub Actions:** a public repository with standard `ubuntu-latest` runners is free. This workflow
  uses two short jobs, no matrix, no larger runners, `timeout-minutes: 10` per job and 7-day artifact
  retention. A private repository consumes included minutes and then bills per minute; artifact
  storage beyond the quota is billed too. Check the repository plan before making it private.
* **PyPI:** free. No paid plan, no token secret, OIDC only.
* **MCP Registry:** free. Publishing is manual, so it consumes no Actions minutes.
* **GHCR (disabled):** public container packages are free to store and pull, but building and pushing
  still consumes Actions minutes. The optional job in `release.yml` stays commented until a Dockerfile
  exists and the first PyPI release is green.
* **No paid features** are enabled: no larger runners, no matrix expansion, no marketplace actions
  with paid tiers, no long-lived registry/PyPI tokens.

GitHub's free-tier terms can change; re-check them before enabling a new job or changing repository
visibility.

## Verify the release plumbing locally

```bash
python -c "import json,pathlib; d=json.loads(pathlib.Path('server.json').read_text(encoding='utf-8')); assert d['name'].count('/') == 1; assert d['packages'][0]['registryType'] == 'pypi'; print('server.json ok:', d['name'], d['version'])"
```

```bash
python -c "import yaml,pathlib; yaml.safe_load(pathlib.Path('.github/workflows/release.yml').read_text(encoding='utf-8')); print('release.yml parses')"
```

PyYAML follows YAML 1.1 and reads the `on:` key as boolean `True`; that is a parser quirk, not an
invalid workflow. GitHub reads YAML 1.2. If `actionlint` is installed, it is the better check:

```bash
actionlint .github/workflows/release.yml
```

`mcp-publisher validate server.json` is the authoritative manifest check; a JSON parse only proves
the file is syntactically valid.
