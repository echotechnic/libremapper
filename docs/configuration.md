# Advanced configuration reference

Start from [`examples/libremapper.yaml`](../examples/libremapper.yaml) and
[`examples/maps/core-network.yaml`](../examples/maps/core-network.yaml). The
Devices, Links, Layout, and Review cover normal operation. Advanced exposes
the complete YAML and JSON configuration. Start with the [operator workflow](usage.md).

## Files and loading order

`/config` is the sole configuration source. Its managed layout is:

```text
/config/libremapper.yaml
/config/maps/<safe-lowercase-key>.yaml
/config/manual-topology.yaml
```

Each map fragment contains one mapping and its filename must match `key`.
Fragments load lexically after any legacy inline `maps`. Map-local
`scheduled: true` makes an already initialized map eligible for an external
`libremapper reconcile` run; it does not alter plan freshness hashes. Optional `example: true` labels a map
as an example in workspace without changing reconciliation or freshness hashes.

workspace saves preserve unknown keys but normalize the selected map and discard
its comments. Files saves preserve the entered text. Both validate a staged
copy of the complete tree and atomically replace one file. A stale edit hash
requires explicit overwrite confirmation. Regular, non-executable `*.bak`
operator backups are ignored by staging, freshness checks, and Files, and are
left untouched. Hidden files, symlinks, executables, and unsupported active files
still block saves.

## LibreNMS connection

Database credentials come from the standard LibreNMS environment:

- `DB_HOST`
- `DB_PORT`
- `DB_NAME` (`DB_DATABASE` remains a compatibility alias)
- `DB_USER` (`DB_USERNAME` remains a compatibility alias)
- `DB_PASSWORD`

Set `librenms.url` only for development or a nonstandard SQLAlchemy
connection. Do not store passwords in map configuration.

## Providers and identity

Providers are evaluated in the order listed under each map. The first
non-empty value wins during federation. `identity.keys` controls automatic
matching, while `identity.aliases` resolves known naming differences.

Set root-level `manual_provider` to one file provider to apply its links and
suppression rules to every map. It cannot also appear in a map's `sources`.
Manual links are included when either endpoint is selected, bringing the other
endpoint in one hop. File-provider links are never suppressed. Per-map
`manual_link_exclusions: []` includes all relevant global manual links by default;
add a manual source ID to this list to exclude that relationship only from the
map. Links → Manual manages inclusion without changing the global file.
New links use a stable `manual:*` ID derived from sorted endpoint identities;
existing `guided:*` IDs continue to work.

Device-level logical manual links are fallbacks. Discovery replaces their
visual edge while their source observations remain in the file. Single edges
combine provenance; parallel edges use pair-level manual coverage. The plan's
`link_fallbacks` reports `active`, `corroborated`, or `parallel-covered` and the
covered canonical edge IDs. Edge operations may include `reconciliation_reason`
and `previous_canonical_id`; ownership metadata retains protocol and provider
sources for later removal explanations. Legacy records without provenance are
identified explicitly when removed.

The file-provider schema is:

```yaml
version: 1
devices:
  - source_id: edge-a
    hostname: edge-a.example.net
    management_ip: 192.0.2.10
    attributes: {map_image: firewall.svg}
    interfaces:
      - source_id: uplink
        device_source_id: edge-a
        name: Ethernet1
links:
  - source_id: carrier-edge-a
    protocol: external
    a: {device: internet}
    b: {device: edge-a, interface: uplink}
suppress_links:
  - a: {device: edge-a}
    b: {device: old-peer}
    protocol: lldp
```

Suppressions match device pairs in either direction. Interfaces and protocol
optionally narrow a match; a pair-only suppression removes all automatic
LibreNMS or regex-artifact relationships for that pair. Manual additions run
last.

Links involving a non-LibreNMS node default to logical. Set
`relationship_kind: direct` to bind a selected LibreNMS interface's traffic
counters. Set a file device's `attributes.map_image` to override the map's
`node_defaults.image` for that device.

### Oxidized Git provider

**Advanced → Oxidized connection** configures shared Git access.
**Links → Parsers → Selection** enables parsers for each map, and
**Advanced → Parser catalog** edits shared definitions. Results from all
selected parsers are combined before applying each map's scope. Existing configurations default to the Ciena SAOS 10 LLDP parser,
which reads `topology/*--lldp-neighbors`. For a remote repository, the tab
accepts the SSH URL and read-only private fetch key. It stores that key with
mode `0600` in the persistent data volume and pins the server identity on the
first successful connection. Only the managed paths appear in YAML:

```yaml
providers:
  oxidized:
    type: oxidized
    settings:
      repository: git@gitea.example.net:network/oxidized.git
      ref: main
      private_key: /data/libremapper/oxidized/id_git_fetch
      known_hosts: /data/libremapper/oxidized/known_hosts
      accept_new_host_key: true
```

A local bare repository or normal checked-out worktree uses
`repository: /oxidized.git` and needs no SSH settings. A read-only Git mirror
and private parser snapshots are cached under `/data/libremapper/git`. Only
files needed by the selected parsers are extracted, with owner-only access.
Every Review and reconciliation reads
the selected commit first; a remote fetch or parse failure blocks planning and
leaves the last successful snapshot intact.

Git still transfers repository objects into that mirror. Treat the
`libremapper-data` volume as sensitive if the same repository also contains
device configurations.

When enabled on a map through workspace, the provider automatically receives the
same selectors and layers as that map's LibreNMS source. Advanced artifact
formats remain available through `regex_artifacts`.

The Ciena SAOS 10 NNI configuration parser reads main backups at the repository
root or under `configs/`. It finds `oc-if:interfaces` point-to-point interface
addresses on IPv4 `/30` or `/31` and IPv6 `/126` or `/127` networks. A unique CAR
interface in LibreNMS address inventory, on the same subnet and in the same context, supplies the remote
endpoint. Both devices must already exist; unknown or ambiguous matches do not
create devices. Ambiguous matches are reported for review.

Missing local inventory interfaces remain named, unbound endpoints. Existing
inventory interfaces bind to their actual port IDs. These records use protocol
`ip-p2p` and configuration evidence; an unbound name never supplies counters.
Dual-stack observations of the same endpoints produce one connection. A
configuration with no applicable CAR connections is valid and produces no links.

Opening **Links → Parsers → Report** inspects the selected map's enabled
parsers and scope. Source and catalog pages do not run parsers. A blocked selected
parser blocks that map's combined result, even if another parser succeeds.
New workspace maps inherit the legacy provider default (LLDP when omitted).
Explicit `sources[].parsers` replaces that default; `[]` disables execution.

```yaml
# Shared full-definition overrides and custom parsers in libremapper.yaml.
oxidized_parsers:
  site-neighbors:
    type: regex
    label: Site neighbor format
    file_globs: ["topology/*--neighbors"]
    path_regex: '^topology/(?P<device_name>.+)--neighbors$'
    protocol: lldp
    zero_output_guard: true
    rules:
      - id: neighbors
        label: Neighbor rows
        regex: '^(?P<local_interface>\S+)\s+(?P<remote_hostname>\S+)\s+(?P<remote_interface>\S+)$'
        flags: [MULTILINE]

# Per-map source selection, usually in maps/<key>.yaml.
maps:
  - name: Site map
    sources:
      - provider: oxidized
        parsers: [saos10-lldp-neighbors, site-neighbors]
        selectors: [{type: hostname, value: '^core'}]
```

Parser and rule IDs are stable lowercase names, up to 64 characters. Labels can
change independently. Regex rules require `remote_hostname`; local device names
come from `device_name` in the filename or `local_hostname` in the extraction.
Optional captures include interface names, management IPs, chassis ID, and site.
Allowed flags are ASCII, DOTALL, IGNORECASE, MULTILINE, and VERBOSE. Rules are
ordered fallback formats, not additive rules within one parser. File globs are
relative and cannot traverse parents or read symlinks.

The built-in IDs are `saos10-lldp-neighbors` and `saos10-nni-config`. Putting a full
definition under one of these IDs overrides its packaged default. Removing that
override resets the built-in; removing a custom definition is allowed only after
all map/default references are gone. Omitted catalog entries retain packaged
defaults, and legacy `providers.<name>.settings.parsers` remains a fallback for
sources that omit `parsers`. It does not auto-enable new catalog entries.

An NNI definition uses `type: saos10_nni`, the same label/glob/path/guard fields,
and these typed settings (shown with packaged defaults):

```yaml
peer_hostname_regex: '^car\d+(?:\.|$)'
interface_types: [point-to-point]
ipv4_prefix_lengths: [30, 31]
ipv6_prefix_lengths: [126, 127]
```

NNI filename regexes require `device_name`. At least one address family must be
enabled. SAOS grammar, same-context matching, unique inventory peers, and named
missing interfaces are retained. Definitions are data; executable parser code is
not accepted. Structural saves do not require Git access or a successful match.
Review and Publish still check live selected topology and full map freshness.

Parser-only main-file edits invalidate only maps enabling changed definitions.
Unrelated saved reviews and schedules remain usable. Mixed configuration edits
retain the existing broader invalidation. Preview does not mark or prune
successful production snapshots. See [usage](usage.md#advanced-and-existing-maps)
for request, display, and execution limits.

## Selectors, layers, and protocols

Selectors are unioned and can assign initial layers:

```yaml
selectors:
  - {type: hostname, value: "^core-", layer: 0}
  - {type: group, value: Core, layer: 0}
  - {type: location, value: "^Branch", layer: 10}
```

Supported selector types are `hostname`, `location`, `group`, `device_id`,
`canonical`, and `attribute`. Hostname, location, and attribute values support
regular-expression matching. Group, device ID, and canonical selectors are
exact matches. Every selector can add devices; the first matching selector
supplies the initial layer.

The device-scope editor exposes these layer values. In manual layouts, explicitly
assigned LibreNMS scope layers also apply after federation, so shared manual
device defaults do not override a map's scope assignment. First match wins.

Map-level `layer_overrides` uses the same selector forms after automatic and
manual topology are merged. In manual layouts, the first match changes only the
device layer and takes priority over scope layers. The Layout bulk editor uses
exact `device_id` rules for LibreNMS devices and `canonical` rules containing
`MANUAL_PROVIDER:SOURCE_ID` for external manual devices. Canonical selectors
also match provider-qualified source identities after federation, so a manual
record's override survives merging with inventory. The editor preserves unrelated
overrides and never changes the shared manual default.

An empty protocol list allows every discovered protocol name. Common names are
`lldp`, `cdp`, `ip-p2p`, `mac`, `bgp`, and `ospf`, but configurations should
not assume LibreNMS is limited to that set.

## Layout and labels

Libremapper supports force-directed, top-to-bottom, left-to-right, and radial
layouts. Layered layouts default to packed placement in the configured canvas.
Set `layout.auto_grow: true` to treat width and height as minima while
preserving margins and spacing. New maps created in workspace enable this setting
so large device groups are not compressed into the initial `1600×800` canvas.

`layout.center_layer: 0` places signed layers at equal ranks around the canvas
center. `layout.separation: constrained` uses deterministic relationship
ordering from the center layer while keeping branch groups together on rows,
columns, or rings. Without a center layer it retains seeded spring ordering.
Radial rings choose the lowest-crossing rotation against inner rings.

For `top` and `left`, `layout.layer_margins` can assign each layer non-negative
start and end cross-axis margins. Singleton same-layer edges receive a curve
lane so their labels remain separated.

Map and layout canvases default to `1600×800`. Explicit dimensions remain
authoritative and opt-in auto-grow is not capped by that default. Set
`node_label: sys_name` to use LibreNMS's discovered device name, falling back
to a short hostname when it is unavailable. `node_label: short_hostname`
keeps `short_hostname_parts` components from the left (default `2`), so
`router1.site1.edge.example.test` becomes `router1.site1`. Set the count to `1`
for `router1` or `3` for `router1.site1.edge`. IP addresses and single-component
names remain unchanged. The same count applies to sysName's hostname fallback.
Choose the label format and count in **Layout**. New maps use `sys_name`.
For device-specific exceptions, check devices in Devices or Layout and apply
the shared **Short hostname components** field. **Check all** respects the search
filter; existing selections persist when filtering. Blank clears the selected
exceptions. A map stores these as
`short_hostname_overrides: [{device_id: 42, parts: 3}]`. File topology device
definitions may set `short_hostname_parts: 3` directly on the device. Priority
is map device override, device definition, then map default. These counts
affect short hostnames and sysName fallback; explicit sysNames and full
hostnames keep their existing meaning.
Maps using short hostnames previously removed the final two components;
review label changes before publishing after this update.

## File safety, migration, and reset

Only regular UTF-8 `.yaml`, `.yml`, and `.json` files up to 5 MiB are editable
in the Advanced tab. Traversal, hidden paths, executable files, nested map paths,
and symlinks are rejected. Invalid files written externally disable Preview,
Apply, and scheduling while Files and Reset remain usable.

Automation should write a temporary sibling file, `fsync` it when durability
matters, and rename it over the destination so readers never observe a partial
document.

On first use, an empty writable `/config` is populated from legacy database
YAML. Anchors are expanded, maps are split into standalone files, schedule
choices move into those files, and runtime paths are normalized to
`/data/libremapper/state.sqlite3`, `/oxidized`, and the relative manual
topology path. A non-empty mount is left unchanged except when its complete
tree byte-for-byte matches the prior untouched defaults; that tree gains the
default `librenms` provider. Any custom file, map, provider, or manual topology
prevents this repair.

**Reset to defaults** requires typing `RESET`. It backs up the complete tree
under `/data/libremapper/backups/<timestamp>` before installing an empty map
configuration with the default `librenms` provider.

## Operator workspace fields

`device_exclusions: [42]` removes devices and attached links after automatic
and manual federation. Exact `device_id` inclusions must not contradict these
exclusions. Rules use match-any semantics. Regex syntax is validated on load.
Unsupported canonical and attribute rules remain available in Advanced.

New maps use `layout: {type: auto, infer_layers: true, auto_grow: true, separation: constrained}`.
Existing maps default to `infer_layers: false`. Inference uses direct relationships
within connected components, falling back to other relationships only where no
direct evidence exists. Highest-core, highest-degree roots define shortest-path
tiers; isolated nodes follow connected tiers. Automatic chooses constrained
top-down placement for hierarchical graphs and organic placement for flat ones.
Automatic always derives layers from connections. Automatic and enabled
`infer_layers` ignore all saved scope layers, `layer_overrides`, and manual
device layers. These values remain saved and take effect again with inference
off in a manual layered layout. Explicit Organic uses force placement.
Reviewed plans expose requested/resolved layout, dimensions,
and `hierarchy_inferred` under `layout_summary`.

The connection builder stores exact `librenms_port_id` values for real ports.
Custom interfaces use `attributes: {manual_label_only: true}` and cannot match
or inherit a discovered interface's counters. Endpoints without interfaces
form device-level logical fallbacks. Editing keeps `source_id`; relationship
changes invalidate reviewed plans before the staged configuration is written.
The shared manual provider's existing external nodes and file-defined interfaces
are editable in the manual catalogs; unexposed fields are retained.

Manual connections may reference an inventory device directly with
`device: librenms:42`. A selected interface can be stored on the endpoint as
`interface_definition` with its `source_id`, `device_source_id`, and any port ID
or custom label. Optional `inventory_overrides` keyed by `librenms:42` retain
manual layers, short hostname settings, attributes, and shared interface
definitions. Existing manual-device records continue to work; migration requires
review of every affected map and its saved settings.

`manual_device_inclusions: [manual-device:example]` seeds standalone devices even
without a matching discovery rule. `manual_device_exclusions` removes matching
manual source IDs and their attached connections after federation, including
inventory aliases. Deleting an unused manual device removes membership entries
across maps in the same validated batch.
An explicit manual device `layer` supplies its default in manual layouts.
Explicit LibreNMS scope layers take priority over that default, and per-map
`layer_overrides` take priority over both. Automatic layout and hierarchy
inference ignore these assignments without removing them from configuration.

`link_graph_overrides: [{canonical_id: CONNECTION_ID, port_id: 42}]` binds that
connection to a live interface belonging to either associated endpoint device.
`port_id: null` explicitly returns to Automatic; an absent entry retains automatic
behavior. The graph binding does not change endpoint identity or relationship
classification. Invalid ports and unmatched targets appear as publishing
conflicts. Only the current map is changed.

Workspace saves create per-map pending markers under the shared runtime directory
(`LIBREMAPPER_DATA_ROOT`, or the state directory for standalone CLI use)
(`drafts/<sha256-map-key>.pending`). Scheduled reconciliation skips these maps.
Only a successful, freshness-checked manual publication clears its marker,
including a no-change publication. Workspace configuration edits conservatively
mark all maps pending; direct CLI/file automation retains its explicit controls.
