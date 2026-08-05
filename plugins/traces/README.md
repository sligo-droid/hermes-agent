# Hermes Traces

This opt-in bundled plugin publishes completed Discord session lineages through
the pinned Traces CLI and contributes an `Agent Trace` link to the standard
Discord feature-summary embed.

Enable it with `plugins.enabled: [traces]`. The Traces CLI defaults to
`/home/droid/.local/share/hermes-traces-cli/node_modules/.bin/traces`; override
that with `HERMES_TRACES_EXECUTABLE`. Credentials remain in `$HERMES_HOME/.env`.

The protected resolver can run without an editable Python installation:

```bash
cd /path/to/hermes-agent
.venv/bin/python -m plugins.traces.hermes_traces_plugin.resolver \
  --host 127.0.0.1 --port 8788
```

Trace publication and artifact lookup are fail-open so they cannot block the
Discord response or its completion lifecycle.
