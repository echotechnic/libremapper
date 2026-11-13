# Libremapper

Libremapper turns LibreNMS inventory and discovered topology into maintained
[LibreNMS Custom Maps](https://docs.librenms.org/Extensions/Custom-Map/). It
reviews every change before writing and preserves manual map styling and
positions. Operators work in **Devices**, **Links**, **Layout**, **Review**,
and **Advanced** without writing YAML.

## Build and publish a map

Open **Plugins → Libremapper → Settings** as a LibreNMS administrator.
LibreNMS should already contain the devices, ports, and discovered
relationships you want to map.

1. In **Devices**, choose **Create your first map**, enter a name, and add
   device-scope rules. Rules match any; include or exclude individual devices
   when needed. Libremapper generates the map key.
2. In **Links**, inspect discovery and use **Add missing connection** to choose
   devices and interfaces from searchable dropdowns. Manual connections are
   shared across relevant maps, with visibility saved separately for each map.
3. Assign scope layers in **Devices**, or use the shared layer field and device
   checkboxes in **Layout**. Automatic layout and hierarchy inference ignore
   saved layers; manual layered layouts use them with inference off.
4. Select **Review changes** to inspect the exact devices, links, removals,
   warnings, and conflicts. Select **Publish to LibreNMS** when ready.

Saving keeps edits in the draft configuration. It does not change the live
Custom Map. Publishing rechecks the reviewed plan for freshness, then refreshes
the header's current status. **Advanced** contains discovery connections,
appearance, diagnostics, scheduling, and raw files. Existing tab bookmarks
continue to their corresponding workspace sections.

New maps automatically use LibreNMS discovery, configured Oxidized discovery,
all protocols, and an automatic layout with hierarchy inference and canvas
growth. A sole renamed LibreNMS source also works automatically; ambiguous
source configurations require an Advanced source choice. See the
[operator guide](docs/usage.md) for the complete workflow.

## Add it with Docker Compose

Libremapper has been tested with the stock `librenms/librenms` images for LibreNMS
26.5.1 and 26.10.2 on Linux amd64. Other LibreNMS versions and architectures
have not been verified. Docker Engine with the `docker compose` plugin is
required; no minimum Docker Engine or Compose version has been established.
See [compatibility](docs/deployment.md#compatibility) for the exact test images.

This is the shortest path for an existing Docker Compose installation whose
main service is named `librenms`. Libremapper does not replace or derive that
image: the engine runs beside it and a small native plugin supplies the UI.

Copy [`compose.plugin.yaml`](compose.plugin.yaml) beside the existing Compose
file as `compose.libremapper.yaml`, and copy
[`scripts/install-or-update`](scripts/install-or-update) into that directory.
Add the existing database values and a new API token to `.env`:

```bash
printf 'LIBREMAPPER_API_TOKEN=%s\n' "$(openssl rand -hex 32)" >> .env
```

Install or update both components with one command:

```bash
chmod +x install-or-update
./install-or-update 0.1.0
```

The helper upgrades the sidecar first, verifies its version, installs the
matching native plugin from the image, enables it, and verifies its routes. It
also persists `LIBREMAPPER_VERSION` in `.env`. Compose may recreate the main
container once on first install to attach the shared mounts and environment,
but continues using the image already declared by the existing stack. Run the
same command after LibreNMS is recreated or when changing Libremapper versions.
See the [deployment guide](docs/deployment.md) for initial files, nonstandard
Compose filenames, checks, and rollback.

Do not install Libremapper under `/data/plugins`. That mount supports local,
hook-only plugins; Libremapper's thin Composer/Laravel client provides routes
and views, while the engine and scheduler remain in the sidecar.

Existing same-name maps are left alone unless you explicitly choose **Take
over an existing map**.

To supplement discovery from configuration backups and command output, open
**Advanced → Oxidized connection** to configure Git, then select parsers for each
map in **Links → Parsers**. Edit shared definitions in **Advanced → Parser catalog**.
Ciena SAOS 10 LLDP and NNI configuration parsers can run together. For a central
repository, paste its Git SSH URL and
read-only fetch key—no Compose change or update-script rerun is needed. A
same-host Oxidized bare repository or checked-out worktree can instead be
mounted read-only. Libremapper automatically reuses each map's LibreNMS device
selections and layers. See the
[deployment guide](docs/deployment.md#oxidized-git-neighbors).

## What it does

- Combines LibreNMS discovery with optional YAML, JSON, and Oxidized topology.
- Produces a read-only exact plan before publishing.
- Preserves manual labels, styles, images, positions, and edge midpoints.
- Supports automatic hierarchy, deterministic layered and organic layouts,
  and externally scheduled reconciliation.

## Documentation

- [Docker Compose deployment and upgrades](docs/deployment.md)
- [Operator workflow, troubleshooting, and CLI](docs/usage.md)
- [Advanced configuration, providers, selectors, and layouts](docs/configuration.md)
- [Agent access through MCP and the bundled skill](docs/mcp.md)
- [Contributing and reporting issues](CONTRIBUTING.md)
- [Reporting security vulnerabilities](SECURITY.md)
- [Oxidized model examples and typed output](examples/oxidized/README.md)
- [Official LibreNMS Custom Maps documentation](https://docs.librenms.org/Extensions/Custom-Map/)

Libremapper is the Custom Maps successor to the original `weathermapper`
utility. It writes LibreNMS Custom Map records rather than generating a static
image.

## License

Libremapper is maintained by EchoTechnic and contributors. It is available under
the [GNU General Public License, version 3 or later](LICENSE), except for the
adapted Oxidized model, which is available under [Apache 2.0](LICENSES/Apache-2.0.txt).
See [COPYRIGHT.md](COPYRIGHT.md) for attribution. Contributions are accepted
under the GPL unless another compatible license is identified.
