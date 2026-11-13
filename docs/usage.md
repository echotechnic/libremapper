# Using Libremapper

Workspace inventory and configuration status are cached for up to 15 seconds
to make navigation faster when LibreNMS has a shared cache store. Managed
configuration edits invalidate the cached results immediately; saves, reviews,
publishing, and agent operations continue to use live data.

## Create a map

LibreNMS must already contain the intended devices, ports, groups, locations,
and discovered relationships. Libremapper reads that inventory; it does not
discover or add devices itself. Install or update the matching sidecar and
plugin with `./install-or-update 0.1.0`; the helper upgrades the sidecar first.
See the [deployment guide](deployment.md) for setup, checks, and rollback.

Open **Plugins → Libremapper → Settings** as a LibreNMS administrator. On a
fresh installation, **Create your first map** asks for a map name and device
scope. The map key is generated from the name, adding `-2`, `-3`, and so on
when needed. Existing map keys stay fixed when names change.

Libremapper chooses the configured `librenms` source automatically, or the
sole LibreNMS source if it was renamed. When several sources are ambiguous,
choose one in **Advanced: discovery source**. Fresh and reset configurations
include LibreNMS discovery using the sidecar's existing database credentials.
Creating maps requires a map directory; custom setups can configure it in
**Advanced**.

New maps enable configured Oxidized discovery, allow every protocol, and use
automatic layout with hierarchy inference, canvas growth, and constrained
ordering. Existing maps retain their saved settings.

The map header stays available across **Devices**, **Links**, **Layout**,
**Review**, and **Advanced**. It shows the selected map, reviewed device and
link totals, attention count, draft or published status, and **Review changes**.
Saving edits keeps a draft; the live Custom Map changes when you publish.

Scope labels distinguish **This map**, **Shared across maps**, and **Preview
only**. Shared editors show their usage or affected maps beside the action.
Hover or keyboard-focus an information glyph for a field definition. Agent
access settings explicitly apply immediately, independently of map publication.

## Devices

Add repeatable device-scope rules using an exact group or location dropdown,
or a regular expression against hostname, sysName, location, OS, hardware, or
device type. Rules **match any**: a device enters the scope when any rule
matches. Groups and locations are checked again on save, as are regex syntax
and device exceptions.

Each scope rule has a whole-number **Layer** value. In manual layered layouts,
the first matching scope rule supplies the layer; individual layer overrides
take priority. Automatic layout and hierarchy inference ignore saved layers.

Use **Devices → Scope** to edit rules and **Devices → Matched devices** for
the searchable device table. Inspect locations, matching rules,
discovered-link counts, and placement. Select a column heading to sort;
select it again to reverse the order. **Include** adds an exact device
exception; **Exclude** removes the device even if a scope rule or manual
connection would otherwise add it. Exclusion also removes its attached links.
**Automatic** clears the individual exception and returns to the saved scope.
Contradictory exact inclusions and exclusions are rejected.

Existing canonical or custom attribute selectors remain available as
**Advanced rules**. Saving plain-language rules preserves those selectors.
The reviewed plan is the authoritative device list, including devices brought
in by relevant manual connections.

## Links

**Connections** shows the merged reviewed topology. **Parsers** contains map
parser selection, parsed connections, and diagnostics. **Manual** edits the
shared connection catalog. Source shortcuts keep the selected map and open the
relevant subtab. Open row details for protocols and reconciliation information.

**Devices → Manual** manages external nodes and older references to existing LibreNMS
devices. It does not create LibreNMS devices. Set a name,
image, placement, and short-hostname component count. New devices are included
in the current map. Other maps can include them explicitly or acquire them
through relevant shared connections. Exclusion removes the device and its
connections even when it merges with inventory. A device cannot be deleted
while connections or suppressions reference it; the error lists dependencies.

Both manual catalogs have separate records and add/edit tabs, with compact
**This map** and **Shared across maps** action columns. They edit the configured
shared topology file. **Edit underlying file in Advanced** opens that same file. Advanced edits appear when the catalogs
reload. Saves reject stale file or map versions. Shared edits can affect several
maps, so inspect usage and review each affected map before publication.

To complete missing connectivity, open **Manual → Add missing connection**:

1. Choose each endpoint's device from the searchable dropdown. Devices in the
   map appear first; **Search all LibreNMS devices** exposes the wider inventory.
2. Choose an interface after selecting the device. Enabled physical interfaces
   with positive speed appear first. **Show all interfaces** exposes virtual,
   disabled, zero-speed, and duplicate-name ports; labels include ifIndex and
   port ID to distinguish them. **Show all interfaces** starts checked;
   uncheck it to keep only the preferred physical interfaces.
3. Save the connection, then review its effect before publishing.

Selecting a real port stores that exact LibreNMS port ID, including when
interface names are duplicated. **No specific interface** leaves an endpoint
unbound; choosing it at both ends creates a logical fallback without traffic
counters. **Custom label…** documents an endpoint without binding counters or
merging it with a real interface of the same name. Endpoints can combine a real
port, a custom label, or an unspecified interface when only part of the
connection is known. **Unknown remote device…** records a named external
device, which is editable in Devices → Manual. Two external devices may connect.
A real port on the known endpoint can supply counters.

Manual relationships are network-wide truth. The table shows which maps use
each relationship; save **Include** separately for each map to control its
visibility. Edit a relationship to correct its endpoints without changing its
source identity. Removing it removes the shared relationship from every map.
Global additions, edits, and removals invalidate reviewed plans; review again
before publishing. Existing `guided:*` relationships remain editable.
New connections reference LibreNMS devices directly. Existing associated manual
records remain available until migrated. If an external device's name matches
inventory, the manual catalog identifies the match for review.

The table distinguishes **Discovered**, **Manual fallback active**,
**Discovered with manual fallback available**, **Parallel discovered links
covered by one fallback**, and incomplete or unresolved relationships.
A logical fallback supplies connectivity while discovery is absent. One
discovered edge combines provenance; parallel discovered edges retain their
own provenance and show fallback coverage for the pair once. Discovery does
not delete the saved manual relationship. A unique replacement retains the
LibreNMS edge ID and styling; ambiguous replacements require reviewed
remove/create operations.

Existing shared manual-file records, including file-defined interfaces and
external endpoints, are editable. Attributes, protocols, suppressions, and
unexposed interface definitions are preserved. Other advanced topology providers
remain editable through Advanced.

Each reviewed connection has a **Traffic interface** control. Choose **Automatic**
or any searchable interface on either associated LibreNMS device. This choice
belongs to the current map and leaves the shared relationship unchanged.
Automatic keeps the deterministic endpoint preference and uses a sole valid
interface on either side. Logical and external connections can carry counters;
custom labels cannot supply them. Explicit virtual, disabled, or zero-speed
interfaces are supported. Missing, deleted, wrong-device ports and unmatched
choices block publication. Correct them or remove the choice in Review.
Graph swaps preserve the native edge identity, styling, positions, and direction.

## Layout

Choose **Automatic**, **Top-down**, **Left-to-right**, **Radial**, or **Organic**.
Choose **Node labels** here as well. Short hostnames keep the first
**Short hostname components** from the left, defaulting to `2`:
`router1.site1.edge.example.test` becomes `router1.site1`. IP addresses stay intact.
In Devices or Layout, check the devices to change, enter one shared
**Short hostname components** count, then choose **Apply to selected devices**.
**Check all** selects only the visible search results. Selections stay selected
when filtering, and the selected count includes hidden selections. Leave the
field blank to restore each selected device's definition or map default.
The **Hostname labels** lane matches its checkbox column. Its effect summary
shows the requested value and includes devices hidden by the current filters.
**Clear hostname selection** clears that selection without changing saved values.
Automatic uses inferred connection hierarchy for structured topologies and
organic placement for predominantly flat ones. Inference prefers direct
relationships, uses other relationships where direct evidence is absent,
and places isolated devices outside the connected tiers.

In Layout, check devices in the **Device layers** column, enter one
**Layer value**, and choose **Apply layer to selected devices**. **Check all
layers** selects only visible search results and retains hidden selections.
Blank clears only the selected devices' individual overrides. Existing
Advanced placement rules remain intact.
The layer lane has its own independent selection and effect summary, and states
whether the saved layout currently uses or ignores manual layers. **Clear layer
selection** clears all selected layer checkboxes, including hidden ones.

External manual devices appear in this list with their shared default layer;
they do not need an inventory scope. The same layer field and filtered checkboxes
set overrides for the current map without changing the shared catalog. Devices
associated with LibreNMS use their inventory row. To change a shared default,
use **Devices → Manual → Edit device → Default layer (all maps)**.

Automatic layout and enabled hierarchy inference calculate layers entirely
from connections, ignoring saved scope, individual, and manual-catalog layers.
Saved values remain available: choose Top-down, Left-to-right, or Radial and
turn inference off to use them. Organic layout ignores layers for positioning.

**Layout → Topology** contains layout defaults and the map preview.
**Layout → Overrides** contains individual hostname and layer overrides.
Independent lower sections use tabs to keep the workspace usable at 1920×1080.

Select **Review changes** after a layout edit. The inline SVG uses the reviewed
coordinates and connections, with zoom, label controls, and a device-table
fallback. The layout summary reports the resolved layout, dimensions, and
whether hierarchy was inferred. Saving another draft invalidates that review.
Each device shows its placement tier below its name, matching the Placement
column; **Show tiers** toggles those annotations independently of device names.

Canvas growth preserves device spacing as the map grows. With growth off,
Libremapper reduces node and layer gaps to fit the configured canvas, which
can crowd large maps. To regenerate saved positions, enable growth in
**Advanced**, then use **Reposition all managed nodes** in Review options.

## Review and publish

1. Save the device, link, layout, or Advanced controls you edited. Saving
   validates the configuration and does not change the Custom Map.
2. Select **Review changes**. Inspect the exact resulting devices and links,
   additions, updates, highlighted removals, warnings, and conflicts in
   **Review**. The same reviewed topology appears in **Layout** and **Links**.
3. Select **Publish to LibreNMS**. Publication writes only that reviewed plan
   and refuses if configuration, topology, or LibreNMS state has changed.

Warnings require explicit confirmation; conflicts block publication. After
publishing, Libremapper generates a fresh review so the header can show
**Published · current**. If that refresh fails, publication still succeeded;
the status message explains the refresh failure. Select **Review changes**
again to refresh status.

## Advanced and existing maps

**Advanced** has subtabs for **Map configuration** (including protocol filtering
and scheduling), **Appearance**, **Oxidized connection**, **Parser catalog**,
**Agent access**, **Files**, and **Maintenance** (reset). Provider and YAML details are in the
[Advanced configuration reference](configuration.md). Existing custom sources,
selectors, manual topology, appearance, and file fragments remain intact
unless you edit the corresponding control. Old Guided, Oxidized, and Files
tab bookmarks lead to Devices, Links → Parsers, or Advanced → Files.

Configure the shared Git connection in **Advanced → Oxidized connection**. Enter a
remote Git SSH URL and read-only fetch key, or choose a mounted local repository,
then **Save and test source**. In **Links → Parsers → Selection**, check the
parsers for the selected map and **Save parser selection**. Unchecking every
parser turns parsing off for that map without fetching Git. Other maps retain their selections. **Report** shows
only the selected map's enabled parsers and scoped topology, with the revision,
per-parser counts, named interfaces without counters, and diagnostics.

**Advanced → Parser catalog** manages the global catalog, independently of a Git
connection. Edit a built-in, reset it to its packaged definition, or clone/create
a custom regex or SAOS 10 NNI parser. Regex formats are ordered fallbacks: the
first format producing valid connections wins for each file. Different enabled
parsers contribute connections together. The NNI engine exposes artifact patterns,
filename and peer hostname matching, interface types, and IP prefix lengths.

In the editor’s **Preview** tab, use **Preview unsaved definition** with pasted
text and a relative filename or a searchable repository artifact. Preview shows captured fields, resolved endpoints,
missing interface bindings, counts, and diagnostics without saving or changing
reviews. Pasted samples are limited to 256 KiB, definitions to 64 KiB, and preview
requests/results to 1 MiB. At most 100 matches are displayed. Parsing stops after
10 seconds for preview or 30 seconds during normal inspection, independently of
Git's fetch timeout. A successful preview is optional before saving.

Saving a shared definition makes only maps enabling that parser drafts, clears
their saved reviews, and pauses their schedules until publication. A new parser
is disabled on existing maps. A custom parser cannot be deleted while a map or
legacy source default references it; the catalog shows those dependencies.
Built-in customizations persist across upgrades. Both the editor and
**Edit underlying file in Advanced** use `oxidized_parsers` in the same main file;
reload the catalog after file edits. Source disconnection preserves this catalog
and requires every map's parser selection for that source to be disabled first.

A same-name Custom Map that Libremapper does not own is left untouched.
Choose **Take over an existing map** deliberately in Review options, or use
`--adopt`. Adoption matches nodes by LibreNMS device ID or label and edges by
endpoints; unmatched objects remain unmanaged.

Device association, edge endpoints, and port bindings remain authoritative.
Other managed fields update only while they still equal their last generated
value. This preserves editor changes to labels, images, colors, styles,
positions, and edge midpoints. **Reposition all managed nodes**, or `--relayout`,
deliberately replaces saved positions and midpoints.

Generated map labels use a light outline for readability against dark and light
backgrounds. Default text is dark blue; explicitly configured or manually edited
text colors remain in place. Review and publish changes to update existing maps.

In Links → Connections, review the discovered and manual connections and select
which to include in this map. A discovered connection with an interface at only
one endpoint is excluded by default; select it explicitly to include it. Saving
the selection creates a draft, and the next review shows the resulting changes.
Selections identify the federated connection, so a changed endpoint may produce
a new connection that needs a fresh selection.

Only objects recorded in `/data/libremapper/state.sqlite3` can be removed as
stale. Deleting that database discards ownership knowledge; use adoption to
recover. Deleting a map configuration stops future reconciliation but leaves
the LibreNMS map and ownership records intact.

## Troubleshooting

For a missing device, check its scope rules, exact exceptions, and current
group or location membership. Inspect the complete reviewed list to
distinguish selection from rendering problems. Advanced selectors can also
match canonical IDs and custom attributes.

For a missing link, check that both endpoints are selected or brought in by
manual topology, neither device is excluded, the protocol is allowed, and
LibreNMS has discovered the relationship and relevant ports. Review the link
table and warnings, or add a missing connection.

An empty protocol list in Advanced allows every discovered protocol name.
Common names include `lldp`, `cdp`, `ip-p2p`, `mac`, `bgp`, and `ospf`; LibreNMS
may supply others. For connection, credential, mount, and upgrade issues, see
the [deployment guide](deployment.md).

## Scheduling

Scheduling is unavailable until a map has completed one successful manual
publication. Enable scheduling in **Advanced** or set `scheduled: true` in the
map file. Workspace saves pause scheduling until the operator reviews and
publishes the draft. Workspace configuration and manual-link edits conservatively
pause all maps until each is published. Run the sidecar command from cron or systemd:

```bash
docker compose -f compose.yml -f compose.libremapper.yaml exec -T \
  libremapper libremapper reconcile
```

The command checks every eligible map. `libremapper reconcile MAP` limits a run
to one map. Warnings and conflicts require manual review; the command returns
nonzero if any map fails. Leave scheduling disabled for a manual publish-only
workflow.

## Command line

The engine is available inside the sidecar:

```text
libremapper validate
libremapper plan MAP [--relayout] [--adopt] [--output PLAN.json]
libremapper apply MAP [--dry-run] [--relayout] [--adopt] [--yes]
libremapper apply PLAN.json [--yes]
libremapper reconcile [MAP]
libremapper provider inspect NAME --json
libremapper links plan PROVIDER [--output PLAN.json]
libremapper links sync PROVIDER_OR_PLAN [--yes]
libremapper state show MAP
```

The CLI retains `plan` and `apply`; the workspace labels these actions Review
and Publish. Applying a saved plan retains configuration, source, topology,
and database freshness checks. `--relayout` and `--adopt` are explicit plan
controls. A shared action lock prevents external reconciliation from
overlapping review or publication.

For viewer, editor, image, and style behavior, see the official
[LibreNMS Custom Maps documentation](https://docs.librenms.org/Extensions/Custom-Map/).
