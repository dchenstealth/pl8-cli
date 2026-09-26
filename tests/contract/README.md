# pl8-interface contract

`operations.yaml` is normally a verbatim copy of pl8-interface's operation
allow-list, from
[pl8-services](https://github.com/dchenstealth/pl8-services).

## PROVISIONAL: the attachment entries are not a copy of anything

**This file is currently hand-authored, not copied, and must be replaced with
a verbatim copy once pl8-services' attachment branch lands.**

Everything up to and including `get_issue_blocking` is a verbatim copy from:

```
src/pl8-interface/src/pl8_interface/operations.yaml @ 87d45bac7a5818ce73bbce4cc8cdc87d9a02e06e
```

The entries added after it, and the `ascending` property on
`get_issue_comments`, were written here from the agreed operation list while
pl8-services was implementing the same list in parallel. They are:

```
initiate_issue_attachment_upload   resign_issue_attachment_upload
confirm_issue_attachment_uploaded  get_issue_attachment
get_issue_attachments              get_issue_comment_attachments
delete_issue_attachment            get_issue_comments_after
```

Until they are replaced, these tests prove only that the CLI agrees with what
was agreed, not that it agrees with what pl8-interface does. The response
shapes settled while this was being written: pl8-interface unpacks the pairs
pl8-base returns into named keys, which is what the `returns:` key on three of
the new entries lists, and what `cli.initiated` and `cli.attached` read. No
existing entry has a `returns:` key at all, so whether pl8-services keeps it
on these, adds it to the rest, or drops it, is still to be seen here.

Re-copy the file as soon as the pl8-services branch is on `main`, pin the
commit below again, delete this section, and make the CLI pass against the
real thing.

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
