# pl8-cli

Agent-friendly command line for [PL8](https://github.com/dchenstealth/pl8-docs),
a lightweight issue tracker backed by DynamoDB. It invokes the
`pl8-interface` Lambda from
[pl8-services](https://github.com/dchenstealth/pl8-services) through the
Lambda `Invoke` API, authenticated with your AWS credentials.

```bash
uvx pl8-cli --env dev invoke get_spaces
```

Every command prints one JSON envelope on stdout,
`{"ok": true, "data": ...}` or `{"ok": false, "error": {"type", "message"}}`,
and exits 0 (ok), 1 (rejected by PL8), 2 (usage error) or 3 (transport or
server fault).
