# MCP client setup

`lv24-seo` runs a local MCP server over stdio. This page registers it in Claude Desktop,
Claude Code, OpenCode, Cursor and VS Code, and documents what `lv24-seo install` merges.
The tool surface itself is in [MCP tool surface](../mcp.md).

## Launcher status in the current alpha

`lv24-seo install` writes this entry (verified with `lv24-seo install --client claude --print`):

```json
{
  "mcpServers": {
    "lv24-seo": {
      "command": "uvx",
      "args": ["--from", "lv24-seo", "lv24-seo", "mcp", "serve"]
    }
  }
}
```

It assumes two things that are **not true in this checkout** (verified 2026-11-10, version
`0.0.1`):

| Assumption | Actual result |
|---|---|
| `lv24-seo` is on PyPI | `uvx --from lv24-seo lv24-seo version` → `lv24-seo was not found in the package registry` |
| `lv24-seo mcp serve` is a CLI command | `lv24-seo --help` lists no `mcp`; `lv24-seo mcp serve` → `No such command 'mcp'` |

The server module itself works: `python -m lv24_seo.mcp.server --help` prints the transport
options, and the process answers an MCP `initialize` handshake as `serverInfo.name = "lv24-seo"`.
If your installed version already resolves `uvx --from lv24-seo ...` and lists `mcp` in
`lv24-seo --help`, ignore this status section and use the blocks as written.

The JSON blocks below are the intended shape; `install` writes the same `command`/`args` under the
client-specific key. The VS Code block is the exception: `install` writes the portable `mcpServers`
shape, not the `servers` shape used by `.vscode/mcp.json`. Until the package is published and the
`mcp` subcommand is wired, point the client at a checkout instead:

```json
{
  "mcpServers": {
    "lv24-seo": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/lv24-seo-optimizer",
        "python",
        "-m",
        "lv24_seo.mcp.server"
      ]
    }
  }
}
```

Use the absolute path to your checkout (on Windows, forward slashes work: `C:/path/to/checkout`).
The `uv run --directory` form was verified against this repository. Use the same command array for
the other clients (`command` is an array in OpenCode); everything else on this page — file
locations, merge behavior, environment variables, security — applies to both launchers. Any
`uvx ... mcp serve` command in the client sections inherits the limitation above; substitute the
checkout argv until the launcher lands.

## Before registering: configuration and tokens

The server calls `load_config()` at startup. It looks for `./seo.toml` relative to the process
working directory, then the platform user config file. Desktop clients do not guarantee a working
directory, so pass the site selectors explicitly:

| Variable | Purpose |
|---|---|
| `LV24_SEO_GSC_PROPERTY` | GSC property id, for example `sc-domain:example.com` |
| `LV24_SEO_YANDEX_HOST_ID` | Yandex host id, for example `https:example.com:443` |
| `LV24_SEO_YANDEX_METRIKA_COUNTER` | Metrika counter id |
| `LV24_SEO_DATA_DIR` | Data directory (SQLite, `secrets.json` fallback) |
| `LV24_SEO_REPORT_LOCALE`, `LV24_SEO_REPORT_TIMEZONE` | Report defaults (`ru`/`en`, IANA zone) |

Run `lv24-seo config show --json` to see the file in effect (`data.path` is `null` when no file was
found).

**Do not put provider tokens in these files.** Store them with
`lv24-seo auth set --provider <name>`; the value goes to the OS keychain (or the 0600 file fallback
when `LV24_SEO_ALLOW_FILE_SECRETS=1`) and never appears in the client config. A token supplied
through `LV24_SEO_*` wins over the keychain and makes `auth set` fail with `WRITE_DISABLED` (that is
deliberate; unset the variable to manage it with `auth set`). The current build has no `auth set
--provider metrika`; the Metrika token is environment-only via `LV24_SEO_YANDEX_METRIKA_TOKEN`.

## Claude Desktop

| OS | File |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` (or `$XDG_CONFIG_HOME/Claude/...`) |

```json
{
  "mcpServers": {
    "lv24-seo": {
      "command": "uvx",
      "args": ["--from", "lv24-seo", "lv24-seo", "mcp", "serve"]
    }
  }
}
```

Environment is a per-server object. Use it for selectors, not tokens:

```json
{
  "mcpServers": {
    "lv24-seo": {
      "command": "uvx",
      "args": ["--from", "lv24-seo", "lv24-seo", "mcp", "serve"],
      "env": {
        "LV24_SEO_GSC_PROPERTY": "sc-domain:example.com",
        "LV24_SEO_YANDEX_HOST_ID": "https:example.com:443"
      }
    }
  }
}
```

Quit Claude Desktop completely (tray/menu, not just the window) and reopen it after editing. If
the server shows as failed, replace `uvx` with its absolute path (`where uvx` on Windows, `which
uvx` on macOS/Linux): GUI applications often do not inherit the shell `PATH`.

## Claude Code

Add the server with Claude Code's own CLI:

```bash
claude mcp add lv24-seo -- uvx --from lv24-seo lv24-seo mcp serve
claude mcp add --scope project lv24-seo -- uvx --from lv24-seo lv24-seo mcp serve
claude mcp add lv24-seo --env LV24_SEO_GSC_PROPERTY=sc-domain:example.com -- uvx --from lv24-seo lv24-seo mcp serve
claude mcp list
```

The project-scoped equivalent is `.mcp.json` at the repository root:

```json
{
  "mcpServers": {
    "lv24-seo": {
      "command": "uvx",
      "args": ["--from", "lv24-seo", "lv24-seo", "mcp", "serve"],
      "env": {
        "LV24_SEO_GSC_PROPERTY": "sc-domain:example.com"
      }
    }
  }
}
```

Claude Code asks you to approve a project-scoped server the first time it starts. The `claude` CLI
is installed separately from this package; check `claude mcp add --help` for the flags supported
by your version.

## OpenCode

Project config is `opencode.json` in the workspace; `lv24-seo install --client opencode` writes the
global `~/.config/opencode/opencode.json`. Local servers use a `command` array and an
`environment` object:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "lv24-seo": {
      "type": "local",
      "command": ["uvx", "--from", "lv24-seo", "lv24-seo", "mcp", "serve"],
      "enabled": true,
      "environment": {
        "LV24_SEO_GSC_PROPERTY": "sc-domain:example.com"
      }
    }
  }
}
```

The installed OpenCode build normalizes this flat `mcp.lv24-seo` entry to `mcp.servers.lv24-seo`
when it loads the file (verified with `opencode debug config`); either layout is accepted. Reload
the configuration with `opencode reload`, or restart the session. MCP servers start with the
session, so a config change does not affect an already-running conversation.

Editing the JSON is more reliable than `opencode mcp add` in this build: its argument parser treats
the `--from` flag as an OpenCode flag and stops with `Unrecognized flag: --from`.

## Cursor

Cursor reads `mcpServers` from the global `~/.cursor/mcp.json` (the path `lv24-seo install
--client cursor` writes) or from `.cursor/mcp.json` in the workspace:

```json
{
  "mcpServers": {
    "lv24-seo": {
      "command": "uvx",
      "args": ["--from", "lv24-seo", "lv24-seo", "mcp", "serve"],
      "env": {
        "LV24_SEO_GSC_PROPERTY": "sc-domain:example.com"
      }
    }
  }
}
```

Reload the window after editing. The server entry appears under Cursor Settings → MCP; if it is
listed but no tools appear, open the server output and check the command/path first.

## VS Code (Copilot Chat)

VS Code's own format uses a top-level `servers` object in `.vscode/mcp.json` (workspace) or the
user-profile `mcp.json`. A `type` is required for stdio servers:

```json
{
  "servers": {
    "lv24-seo": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "lv24-seo", "lv24-seo", "mcp", "serve"],
      "env": {
        "LV24_SEO_GSC_PROPERTY": "sc-domain:example.com"
      }
    }
  }
}
```

Two format details matter:

* VS Code's `mcp.json` uses `servers`; the portable Copilot `.mcp.json` (workspace root) uses
  `mcpServers`. Do not copy one into the other without renaming the key.
* `lv24-seo install --client vscode` writes `mcpServers` to the user-profile `mcp.json`
  (`%APPDATA%\Code\User\mcp.json` on Windows). That is the portable shape, not the VS Code shape.
  Use the block above for `.vscode/mcp.json`, or rename the key after `install`.

The CLI alternative writes the VS Code format to the user profile:

```bash
code --add-mcp '{"name":"lv24-seo","type":"stdio","command":"uvx","args":["--from","lv24-seo","lv24-seo","mcp","serve"]}'
```

Use **MCP: List Servers** to start or restart the server, and **MCP: Reset Cached Tools** after a
tool list change.

## `lv24-seo install`

```bash
lv24-seo install --client claude --print   # print the fragment, write nothing
lv24-seo install --client claude           # merge into the host config
lv24-seo install --client cursor --json    # envelope with the resolved path
```

| Client | File written | Top-level key |
|---|---|---|
| `claude` | `claude_desktop_config.json` (paths above) | `mcpServers` |
| `cursor` | `~/.cursor/mcp.json` | `mcpServers` |
| `opencode` | `~/.config/opencode/opencode.json` | `mcp` |
| `vscode` | user-profile `mcp.json` (`%APPDATA%\Code\User\mcp.json`, `~/Library/Application Support/Code/User/mcp.json`, `~/.config/Code/User/mcp.json`) | `mcpServers` (portable shape; see the VS Code note) |

`--print` (without `--json`) prints only the JSON fragment and does not touch the file. Without
`--print`:

* it reads the existing file and merges instead of replacing it;
* other top-level keys and other servers are preserved; an existing `lv24-seo` entry is replaced;
* the file is rewritten as 2-space indented JSON with a trailing newline; parent directories are
  created;
* it writes no tokens and makes no backup.

`--config` is accepted for CLI uniformity but ignored (host configuration does not use
`seo.toml`). An unknown client returns `INVALID_INPUT` and exit `2`. A file that is not a JSON
object, or a `mcpServers`/`mcp` key that is not an object, returns `CONFIG_INVALID` and exit `2`.
A UTF-8 BOM is rejected as invalid JSON; rewrite the file without a BOM (see
[troubleshooting](troubleshooting.md#mcp-client-or-install-failures)).

## First-run checklist

If no config exists yet:

```bash
lv24-seo init --property https://example.com
```

Then run the three checks in order:

```bash
lv24-seo doctor
lv24-seo auth status
lv24-seo properties
```

| Command | What to look for |
|---|---|
| `doctor` | Python ≥ 3.11, config found, writable data dir, token presence. It is offline: no DNS, TLS or sitemap checks. A missing config, data dir or token is a **failed check but not a failed command**; only Python and a writable cwd are fatal. |
| `auth status` | Seven rows (`gsc`, `yandex`, `bing`, `crux`, `psi`, `smtp`, `telegram`), each `present`/`missing`, with source `env`, `keyring`, `file` or `missing`. Values are never printed. Metrika is not listed; it is environment-only today. |
| `properties` | Project, each provider's target, token state and `ready`. `psi` is `ready` without a key. Missing tokens produce warnings, not an error. |

Then ask the agent: **“How is SEO doing?”**

If the `seo-weekly` skill is installed in the host, it activates on that question (its description
covers “how organic search is doing”, “why traffic dropped”, “what to fix this week”, and the
Russian “как SEO?”, “почему упал трафик”). It starts with `seo_weekly_report`, then follows up
with indexing triage or opportunities when a metric moves.

| Question | Skill |
|---|---|
| “How is SEO doing?”, “why did traffic drop?”, weekly review | `seo-weekly` |
| “Pages are not indexed”, sitemap errors, sudden exclusions | `seo-indexing-triage` |
| “Check technical SEO”, after a deploy, before a release | `seo-technical-audit` |

`lv24-seo install` registers the MCP server only; it does not install skills. Copy the relevant
`skills/<name>/SKILL.md` into the host's skills directory, or use the repository mirrors described
in [Agent skills](../skills.md). Without a skill, call `seo_weekly_report` directly or use the
`weekly_seo_review` MCP prompt.

## Security notes

* Nine of the eleven tools are read-only (`readOnlyHint: true`). `seo_recrawl_request` and
  `seo_sitemap_submit` are the two write tools (`destructiveHint: true`) and are disabled unless
  the call passes `allow_write=true`.
* Even with `allow_write=true`, the MCP write tools return `WRITE_DISABLED`
  (`recrawl writes are not available in this build` / `sitemap writes are not available in this
  build`) because the MCP tool layer has no wired write methods yet. No MCP call can change a
  provider account today. The CLI has a separate gated write surface (`sitemap submit`, `yandex
  recrawl`, `indexnow submit`) documented in
  [troubleshooting](troubleshooting.md#write_disabled--confirm_required); it does not change the
  MCP tools.
* Never paste a token into chat, a client config, an issue or a screenshot. Use
  `lv24-seo auth set` so the value stays in the OS keychain; the server reads it at tool-call
  time.
* A token placed in `env` wins over the keychain. `auth set` and `auth logout` then fail with
  `WRITE_DISABLED` until the variable is unset.
* Client config files are not secret stores. `install` writes only the launcher and no credentials.
* stdio is local to the machine. The streamable-HTTP transport (`python -m lv24_seo.mcp.server
  --transport http`) has no authentication layer in this build: bind it to localhost or put it
  behind a firewall/reverse proxy; do not expose the port publicly.
