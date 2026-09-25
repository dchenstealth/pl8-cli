# pl8-interface contract

`operations.yaml` is a verbatim copy of pl8-interface's operation
allow-list, from
[pl8-services](https://github.com/dchenstealth/pl8-services) at:

```
src/pl8-interface/src/pl8_interface/operations.yaml @ 463869ee9a2b6a8ddbd815471e626e133d934106
```

`tests/test_contract.py` checks the CLI against it: every operation is
reachable from a subcommand, and every subcommand sends params that pass the
operation's JSON Schema, with every property reachable from some flag.

When pl8-interface's operations change, copy the new file here verbatim,
update the commit above, and make the CLI pass again:

```bash
git -C ../pl8-services show <commit>:src/pl8-interface/src/pl8_interface/operations.yaml \
  > tests/contract/operations.yaml
uv run pytest tests/test_contract.py
```
