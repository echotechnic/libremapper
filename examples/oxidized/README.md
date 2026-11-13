# Oxidized model examples

Select parsers independently in **Links → Oxidized**. The NNI configuration
parser reads regular Ciena SAOS 10 main backups and does not require a model
override or alternate command output. The model below supplies the separate
LLDP parser; both parsers can run together.

When LLDP reports a bond and member as `port-desc` (for example,
`ae1/sfp28-1`), the LLDP parser uses the bond if both ports resolve and
LibreNMS identifies it as an aggregate or bridge. With both parsers selected,
Libremapper can also combine an IP point-to-point observation when the SAOS
configuration binds its interface through a forwarding domain to that same
local port and its peer is a subinterface of the bond. Ambiguous bindings
remain separate. Custom overrides of the built-in LLDP parser need a
`remote_port_description` capture to use the bond description.

`models/` is a collection of vendor and platform examples. The included
`models/saos10.rb` is specifically for Ciena SAOS 10; copy it into the Oxidized
model directory when that platform applies. The model is adapted from Oxidized
and is licensed under [Apache 2.0](../../LICENSES/Apache-2.0.txt). Oxidized's Git
output must keep `type_as_directory: true`:

```yaml
output:
  default: git
  git:
    type_as_directory: true
```

Each run then stores `topology/<hostname>--lldp-neighbors`. The `topology`
directory is created automatically on the first successful typed output.
Additional platform models should be sibling files under `models/` and emit
their own typed topology output by setting an appropriate type and name.

Push this Git repository to a central remote such as Gitea, then use the
**Links → Oxidized** source form in Libremapper Settings. The central repository is
the recommended handoff: Libremapper needs only a fetch-only deploy key and no
exported working tree. If Oxidized does not already push this repository, use
its `githubrepo` `post_store` hook with a separate write-enabled key. A
same-host install may instead mount the bare repository at `/oxidized.git`.
