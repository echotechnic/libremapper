# Synthetic LibreNMS test stack

This stack reproduces the approved topology and Custom Map shape without
retaining production data. The committed fixture contains 43 inert devices,
117 ports with fixed synthetic rates, 13 native links, anonymized IP/MAC/BGP/OSPF
evidence, nine SAOS10 LLDP artifacts with 31 relationships, and the
26-node/31-edge `Reference Core` map. Four static role groups and a read-only
manual topology reproduce that exact node/relationship set. RRD files are
neither required nor used.

The five maps contain two transit nodes, one OOB provider, two border/core
pairs, two POPs, four same-site OOB switches, and customers `cust01` through
`cust13`. Each border, core, and POP has one management link to its site's OOB;
all four OOB switches link to the single provider. Customer links remain on the
data plane. Known physical core-border members are suppressed explicitly, so
only each `Bundle-Ether2` relationship remains.

## Local development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
ruff check .
mypy
pytest
scripts/plugin-smoke
```

The integration tests use SQLite fixtures shaped like the current LibreNMS
Custom Maps schema. Production writes target MariaDB/MySQL through PyMySQL.

## Start from a clean fixture

```bash
scripts/generate-test-fixture --check
scripts/dev-stack reset
scripts/dev-stack status
```

LibreNMS is available at <http://127.0.0.1:18201>. The local `admin` password
is generated in the ignored `.realdata-dev/.env` file. MariaDB and the
Libremapper sidecar use an internal backend network. The sidecar shares the
stock LibreNMS network namespace, and its loopback API is not published.
LibreNMS also joins a web
bridge so Docker can publish its UI to localhost only. That address is for
human access; the browser smoke container joins the web bridge and reaches
LibreNMS through Compose DNS at `http://librenms:8000`.

The stack builds only the sidecar and pins the unmodified LibreNMS 26.10.2 and
MariaDB image digests. CI also runs LibreNMS 26.5.1 to cover its older API-token
backend. Set `LIBRENMS_IMAGE` and `LIBRENMS_VERSION` to override the test image
and recorded version. The PHP client is installed from the mounted source tree
with `lnms plugin:add`. LibreNMS
cron, scheduler, dispatcher, SNMP daemon, polling, and discovery services are
not started. All fixture devices have SNMP disabled and are also marked
disabled, ignored, and notification-disabled. Scheduled reconciliation is
invoked explicitly by the smoke test.

## Compare maps

`Reference Core` is seeded as Custom Map ID 2 with the anonymized manual
coordinates, node images, edge styles, and bandwidth labels. The reset command
copies `tests/fixtures/synthetic-core/libremapper.yaml`, `manual-topology.yaml`,
and any map fragments into the writable `/config` mount. The installed engine
then reads those canonical files directly for diagnostics:

```bash
scripts/dev-run validate
scripts/dev-run provider inspect artifacts --json
for map in \
  generated-core-top generated-core-left generated-core-radial \
  generated-core-reference-layout; do
  scripts/dev-run plan "$map" -o "/data/libremapper/plans/$map.json"
  scripts/dev-run apply "/data/libremapper/plans/$map.json" --yes
  scripts/dev-run plan "$map"
done
```

Each final plan should report no changes. The generated maps use deterministic
constrained placement, short node labels, role-specific images, separated cubic
Bezier direct edges, and fixed synthetic port rates. Transit/provider, border,
OOB, core, POP, and customer nodes use `gtm.svg`, `firewall.svg`,
`switch-l2.svg`, `router.svg`, `switch-l3.svg`, and `router-square.svg`.
Top, left, and radial are auto-grown; the reference-layout comparison uses
seven fixed columns in an `1800x1200` canvas. The seeded reference is
`1800x800`; generated canvases are `3040x1480`, `1480x3040`, `2960x2960`, and
`1800x1200`.

Run the complete stock-image, sidecar, PHP, browser, external scheduler, and 26-node/31-edge
check with:

```bash
scripts/plugin-smoke
```

## Fixture maintenance

The authoritative, reviewable source is
`tests/fixtures/synthetic-core/topology.json`. Regenerate its SQL and raw
Oxidized-style artifacts with:

```bash
scripts/generate-test-fixture
scripts/generate-test-fixture --check
```

The generator uses only the Python standard library and enforces synthetic
hostnames, documentation IP addresses, local MAC addresses, and fixed fixture
counts. Do not add production dumps or raw production artifacts to this repo.

The included Ciena SAOS 10 Oxidized model and guidance for adding sibling model
files are documented in `examples/oxidized/README.md`.
