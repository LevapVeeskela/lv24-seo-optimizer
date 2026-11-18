# 0.14.1 - Hidden Windows scheduled jobs

Status: **released** (2026-11-18) on PyPI, npm and the MCP Registry. Desktop `0.9.3` bundles engine
`0.14.0`; desktop `0.9.4` follows with this engine.

Patch release: Windows Task Scheduler jobs no longer open a console window in the signed-in session.

## Fixed

### Console window on every scheduled run

A task registered with `InteractiveToken` and a console executable (`lv24-seo.exe`) opens a console
window for the whole run. `schedule install --target windows` now registers a generated WScript
launcher instead:

* the XML action is `C:\Windows\System32\wscript.exe //B //Nologo "<name>.vbs"`;
* the launcher runs the real command with `WScript.Shell.Run(command, 0, True)`, so no window is
  created, the engine still gets the task working directory and the process is awaited;
* `WScript.Quit code` forwards the engine exit code to Task Scheduler.

The launcher is written next to the XML in `~/.config/lv24-seo/schedule/<name>.vbs` as UTF-16 (with
a BOM) so Windows Script Host reads non-ASCII paths.

### Status and uninstall cover both artefacts

`schedule status --target windows` now lists three artefacts: the registered `task`, the `xml` and
the `vbs` launcher. A missing or changed launcher makes the job `outdated` (or `partial` when the
task is not registered); `schedule uninstall` removes the launcher together with the XML and the
task. Cron and systemd are unchanged.

### Upgrade note

Existing Windows tasks keep their console action until they are reinstalled:

```powershell
lv24-seo schedule install --kind indexing --target windows --allow-write
```

`schedule status` reports the old task as `outdated` until then.

## Not changed

* MCP stays at **12 tools**, SQLite schema at **version 5**, CLI at **25 top-level commands**.
* No new required dependency; no provider call; no change to the write gate or the write secret.
* cron and systemd generators, their status and uninstall behaviour are unchanged.

## Verification

Offline gate on the release commit:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python -m lv24_seo.mcp.registry --check
uv run python -m pytest -q
```

The scheduler tests cover the generated launcher (hidden `Run`, awaited exit code, quoted paths), the
XML action, the three status artefacts, install/uninstall of both files, and the changed-command and
changed-cadence cases. The Windows task was reinstalled and run on a Windows 11 host without a
console window.

## Limitations

* The launcher relies on Windows Script Host (`wscript.exe`), present on supported Windows versions.
  It is a generated file; reinstall the schedule instead of editing it.
* A task installed by an older engine is reported as `outdated` and must be reinstalled once.
* Task Scheduler's own "Last Run Result" now reflects the engine exit code; provider-level failures
  are still recorded in `indexing history`, not in the task result.
