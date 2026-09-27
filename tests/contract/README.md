# pl8-interface contract

`operations.yaml` is a verbatim copy of pl8-interface's operation allow-list,
from [pl8-services](https://github.com/dchenstealth/pl8-services) at:

```
src/pl8-interface/src/pl8_interface/operations.yaml @ 5115f6d
```

## That commit is not on pl8-services' `main` yet

`5115f6d` is the head of pl8-services' `feature/issue-attachments` branch
([pl8-services#6](https://github.com/dchenstealth/pl8-services/pull/6)), which
adds the attachment operations this repo's `pl8 attachment` and
`pl8 comment wait` commands call. The copy is verbatim, so these tests do check the CLI against
what pl8-interface actually accepts — but against a branch, not a released
interface.

Re-pin once that branch is merged. A squash merge gives the file a new commit
even though its contents do not change, so the hash above will be stale rather
than wrong:

```bash
git -C ../pl8-services show <merge commit>:src/pl8-interface/src/pl8_interface/operations.yaml \
  > tests/contract/operations.yaml
uv run pytest tests/test_contract.py
```

If that diff is not empty, the interface changed during review and the CLI has
to be made to match it again.

## What the test checks

`tests/test_contract.py` checks the CLI against it: every operation is
reachable, from a subcommand or through `pl8 invoke`; every subcommand sends
params that pass the operation's JSON Schema, including each leg of a command
that sends more than one operation; and every property of every schema is
reachable from some flag.

When pl8-interface's operations change, copy the new file here verbatim,
update the commit above, and make the CLI pass again:

```bash
git -C ../pl8-services show <commit>:src/pl8-interface/src/pl8_interface/operations.yaml \
  > tests/contract/operations.yaml
uv run pytest tests/test_contract.py
```
