# pl8-interface contract

`operations.yaml` is a verbatim copy of pl8-interface's operation
allow-list, from
[pl8-services](https://github.com/dchenstealth/pl8-services) at:

```
src/pl8-interface/src/pl8_interface/operations.yaml @ c1cdab2e66049d43507447f730c1d4136251360c
```

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
