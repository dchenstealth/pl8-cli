# pl8-cli

Agent-friendly command line for [PL8](https://github.com/dchenstealth/pl8-docs),
a lightweight issue tracker backed by DynamoDB. It invokes the
`pl8-interface` Lambda from
[pl8-services](https://github.com/dchenstealth/pl8-services) through the
Lambda `Invoke` API, authenticated with your AWS credentials. PL8 has no UI;
this is how people and agents use it.

## Getting started

Run it without installing, with [uv](https://docs.astral.sh/uv/):

```bash
uvx pl8-cli --env dev space list
```

or install the shorter `pl8` command:

```bash
uv tool install pl8-cli
pl8 --env dev space list
```

To run an unreleased revision, point uvx at the repo:
`uvx --from git+https://github.com/dchenstealth/pl8-cli pl8 ...`.

### Configuration

| Setting | Flag | Environment variable |
| --- | --- | --- |
| Function to invoke | `--env ENV` (invokes `<ENV>-pl8-interface`) or `--function-name NAME` | `PL8_ENV` or `PL8_FUNCTION_NAME` |
| Default space for bare issue ids | `--space SPACE` | `PL8_SPACE` |
| Creator recorded on what you create | `--creator WHO` | `PL8_CREATOR` |
| AWS credentials and region | `--profile`, `--region` | the standard AWS chain (`AWS_PROFILE`, `~/.aws/config`, ...) |

Flags beat environment variables, and a function name beats an env.
Connection flags may go before or after the command.

Your credentials need `lambda:InvokeFunction` on the function. pl8-services
tags it `Type=PL8Interface` for granting that; the grant itself is managed
outside these repos. `aws login` sessions work.

## Output contract

Every command prints exactly one JSON document on stdout, pl8-interface's
response envelope:

```json
{"ok": true, "data": {"issue_id": "abc123", "status": "TODO", ...}}
{"ok": false, "error": {"type": "DDBStillBlockedError", "message": "..."}}
```

Failures on the CLI side use the same shape. `--pretty` indents it. The exit
status says what kind of failure it was:

| Exit | Meaning | `error.type` |
| --- | --- | --- |
| 0 | Success | |
| 1 | PL8 rejected the request. Fix it; retrying unchanged won't help. | other `DDB*` errors, `InvalidParams`, `UnknownOperation`, `InvalidRequest` |
| 2 | The command line was wrong, or `--profile` names no profile; nothing was sent. | `UsageError` |
| 3 | No trustworthy answer: transport failure or server fault. A write may or may not have been applied, so re-read before retrying it. | `InvokeError`, `FunctionError`, `DDBInternalError`, `DDBCorruptedError` |
| 4 | Transient contention, already retried by PL8. Nothing was applied; retry the same request. | `DDBTransactionConflictError`, `DDBIdCollisionError` |

Writes are never retried automatically, since a write that timed out may
already have been applied. Reads are retried on transient errors.

See pl8-interface's
[contract](https://github.com/dchenstealth/pl8-services/blob/main/src/pl8-interface/README.md)
and pl8-base's
[errors](https://github.com/dchenstealth/pl8-base/blob/main/src/pl8_base/errors.py)
for what each `DDB*` error means.

## Commands

Every command has `--help`, which spells out the rules it enforces.

```
pl8 space create SPACE_ID --name NAME --description TEXT
pl8 space get SPACE_ID
pl8 space list
pl8 space update SPACE_ID --name NAME --description TEXT [--if-version N]
pl8 space delete SPACE_ID

pl8 issue create --title TITLE --description TEXT [--status STATUS]
pl8 issue get ISSUE
pl8 issue list --status STATUS
pl8 issue update ISSUE --title TITLE --description TEXT [--if-version N]
pl8 issue transition ISSUE --status STATUS [--if-version N]
pl8 issue delete ISSUE

pl8 comment add ISSUE --body TEXT
pl8 comment get ISSUE COMMENT_ID
pl8 comment list ISSUE
pl8 comment update ISSUE COMMENT_ID --body TEXT [--if-version N]
pl8 comment delete ISSUE COMMENT_ID

pl8 blocker add --blocking ISSUE --blocked ISSUE
pl8 blocker remove --blocking ISSUE --blocked ISSUE
pl8 blocker list (--blocked ISSUE | --blocking ISSUE)

pl8 invoke OPERATION [--params JSON | --params-file PATH]
```

- **Spaces** must exist before you create Issues in them, and must have no
  Issues left before you delete them.
- **Issues** are named `SPACE/ISSUE_ID`, e.g. `ENG/abc123`. A bare
  `ISSUE_ID` takes its space from `--space` or `PL8_SPACE`; a space in the
  reference always wins, so cross-space blockers need nothing extra.
- **Comments** are named by their Issue and then their own `COMMENT_ID`, as
  two arguments: `pl8 comment delete ENG/abc123 0199f3a1-...`. They are
  listed oldest first, can be added to an Issue in any status including
  `DONE`, and go away with the Issue they are on.
- **Creators** are recorded by `space create`, `issue create` and
  `comment add`. Set `PL8_CREATOR` once, or pass `--creator WHO`. It is a
  label, not a login: PL8 never checks it against your AWS identity and no
  command is allowed or refused on the basis of it, so give each agent its
  own and a thread says which agent wrote what. Updates leave it alone.
- **Long text** can come from a file with `--description-file PATH` (or
  `--body-file` for a comment), or from stdin with `-`, which avoids shell
  quoting.
- **Lists** return `{"items": [...], "cursor": ...}`. Pass `--cursor` back
  for the next page, set the page size with `--limit` (1-100), or use
  `--all` to fetch every page at once.
- **Updates** replace both fields. `--if-version N` makes the write fail
  with `DDBVersionConflictError` if the item changed since you read version
  `N`.
- **`invoke`** sends any operation and params unchanged, for operations
  that don't have a subcommand yet.

### Example

```bash
export PL8_ENV=dev PL8_SPACE=ENG PL8_CREATOR=alice

id=$(pl8 issue create --title "Fix login" --description-file notes.md | jq -r .data.issue_id)
pl8 blocker add --blocking OPS/k8s123 --blocked "$id"
pl8 issue list --status BLOCKED --all
pl8 comment add "$id" --body "Waiting on the cluster upgrade."
pl8 comment list "$id"
```

An Issue that gains a blocker moves to BLOCKED. It returns to TODO in the
background once every Issue blocking it is DONE or deleted, or its blockers
are removed.

## Development

```bash
uv sync
uv run ruff check .
uv run pytest
```

`tests/test_contract.py` checks every subcommand against a pinned copy of
pl8-interface's `operations.yaml`; see
[`tests/contract/README.md`](tests/contract/README.md) to refresh it.

Releases publish to PyPI when a `vX.Y.Z` tag matching `pyproject.toml`'s
version is pushed.
