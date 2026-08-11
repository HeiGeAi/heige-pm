# Agent compatibility and packaging

The package follows the common Agent Skills layout with root [Skill instructions](../SKILL.md), [VERSION](../VERSION), the [boardctl runtime](../scripts/boardctl.py), assets, references, tests, and evals. Packaging compatibility is not proof that a client discovered, executed, or completed an end-to-end dashboard workflow.

## Runtime boundary

`boardctl.py` requires Python 3.9 or newer and uses only Python standard-library modules. The POSIX `fcntl` lock path has been exercised end to end on macOS. The Windows `msvcrt` lock branch is unit-simulated, but there is no current Windows end-to-end claim. Linux and other compatible runtime claims also remain unverified until exercised there.

If Python or both supported lock backends are unavailable, do not claim deterministic merge, render, package, or install. Preserve a private canonical draft when safe, report the exact runtime error, and move processing to a compatible environment.

Always run the current help before use:

```bash
python3 scripts/boardctl.py --help
```

## Claude Code

The generic personal Skill root is `$HOME/.claude/skills`. From the Skill directory, local installation is:

```bash
python3 scripts/boardctl.py install --skill-dir . --target claude
```

This copies to a child directory named `heige-pm`. It refuses an existing target by default. Do not use `--force` unless the user explicitly approves replacement and the backup behavior.

The install command verifies the copied file set and hashes. It does not prove that a running Claude Code session refreshed discovery or executed the Skill. Start or refresh the appropriate session and perform a real trigger test before claiming client verification.

## Codex

The current-user Codex Skill root is `$HOME/.agents/skills`. From the Skill directory:

```bash
python3 scripts/boardctl.py install --skill-dir . --target codex
```

The same conflict refusal and verification boundary apply. The installer deliberately ignores `CODEX_HOME` and installs beneath `$HOME/.agents/skills` unless `--destination` is supplied. A copied directory is local installation evidence, not proof of automatic discovery or end-to-end use in Codex.

Some legacy or local setups may use `$CODEX_HOME/skills`. Treat that as an explicitly selected compatibility destination, not the default:

```bash
python3 scripts/boardctl.py install --skill-dir . --target codex --destination "$CODEX_HOME/skills"
```

Use that command only when the user confirms the variable points to the intended Skill root.

For either client, `--destination` can point to a user-approved alternate Skill root. Do not hardcode a specific person's home directory or silently choose a shared or production location.

## Standard package

The standard ZIP contains one top-level `heige-pm` directory:

```bash
python3 scripts/boardctl.py package --skill-dir . --target standard --output ../heige-pm-standard.zip
```

The output must be outside the source directory. The package builder rejects unsafe paths and validates Markdown references from `SKILL.md`.

## WorkBuddy

WorkBuddy UI import expects the ZIP root itself to contain `SKILL.md` and `VERSION`, so use:

```bash
python3 scripts/boardctl.py package --skill-dir . --target workbuddy --output ../heige-pm-workbuddy.zip
```

Import through the WorkBuddy UI only when the user explicitly requests that external action. Package creation does not prove UI import, discovery, permissions, or a completed workflow.

## OpenClaw

Create the OpenClaw-format compatibility archive with:

```bash
python3 scripts/boardctl.py package --skill-dir . --target openclaw --output ../heige-pm-openclaw.zip
```

The archive uses a top-level Skill directory. This is a format package only. No current OpenClaw installation, discovery, script execution, or end-to-end compatibility claim is made.

## Package and install safety

- Packaging is deterministic: fixed ZIP timestamps, sorted entries, path checks, and no symlinks.
- Generated caches, hidden paths, backups, temporary files, and `dist` are excluded.
- Package output is replaced if it already exists, so preflight the path and require explicit overwrite approval.
- Installation refuses an existing target unless `--force` is supplied. Forced replacement creates a sibling backup, but it still requires explicit approval.
- Neither packaging nor installation publishes a dashboard, sends a message, imports through a UI, or grants credentials.

## Claim levels

Use precise language:

- `package created`: archive structure validated locally.
- `installed locally`: copied tree and hashes verified at the selected root.
- `client discovered`: the target client showed or loaded the Skill.
- `workflow verified`: the client followed the Skill and produced valid local artifacts.
- `end-to-end verified`: the requested user path completed in the target environment.

Do not collapse these levels into a single "works everywhere" claim.
