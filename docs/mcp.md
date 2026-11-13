# Agent access through MCP

Libremapper serves MCP at `https://YOUR-LIBRENMS/api/v0/libremapper/mcp`, through
the existing LibreNMS HTTPS host. It adds no listener or separate identity
service. Whole-map deletion is unavailable.

## Enable and connect

1. Open **Advanced → Agent access** as a plugin administrator and enable MCP.
2. Create a token in LibreNMS **API access**, or use an existing token you own.
3. Assign that token an **Inspect**, **Edit drafts and review**, or **Edit and
   publish** profile. Unassigned tokens have no MCP access. You can manage only
   your own tokens, following LibreNMS's existing token-ownership rules.
4. Configure your MCP client for Streamable HTTP at the endpoint above. Set
   `Authorization: Bearer YOUR-TOKEN` or `X-Auth-Token: YOUR-TOKEN` in the client's
   secret/header configuration. Never place tokens in URLs or tool arguments.

MCP starts disabled when first introduced. Disabling MCP, removing a profile, disabling/deleting
a token, or removing its user's plugin-admin permission stops access. Profiles
restrict this MCP endpoint; they do not reduce the token's existing permissions
on other LibreNMS APIs. MCP itself cannot change profile assignments.

Both current LibreNMS Sanctum tokens and the older `api_tokens` backend are
supported. Expired or revoked tokens lose access immediately. If LibreNMS
migrates your tokens to Sanctum, assign their MCP profiles again in Agent access;
native numeric token IDs can change during that migration. Profiles from the
older backend are not automatically applied to Sanctum tokens.

The endpoint uses the configured LibreNMS public `base_url`, including Host and
Origin validation. Keep the existing trusted-proxy settings correct. It accepts
clients without an Origin header, and browsers from the same origin; it does
not enable cross-origin browser access. Native API tokens are used rather than
an OAuth authorization server, so clients that require OAuth-only connection
flows are not supported. Both the `2026-07-28` protocol and older SDK-supported
handshake clients are supported. Legacy sessions belong to a specific token.

## Inspection and editing

Inspect tools cover maps, saved reviews, manual catalogs, devices, interfaces,
neighbors, groups, locations, and parsed provider diagnostics. They return the
last stored status with available observation timestamps, not an on-demand
probe. Provider diagnostics include bounded category counts and parsed topology.
Pass optional `map_key` to `inspect_provider` to use that map's enabled parsers
and scopes; omitting it retains provider-default inspection. Existing map
create/update schemas accept `sources[].parsers`: parser IDs enable those global
definitions and `[]` disables them. Parser-definition editing and previews are
available in the admin UI; no dedicated parser CRUD/preview MCP tools are exposed.
Credentials, raw device configurations, alerts, routing tables, and historical
metrics are outside this interface.

Lists use `query`, `limit` (default 100, maximum 500), and `cursor`. Reuse the
same query when paging. A changed collection revision requires restarting the
list. Results include `collected_at`; missing polling timestamps mean the
observation time is unavailable.

Edit tools create maps with an explicit key and patch current map settings.
For example, after reading `get_map`, use `update_map` with its `map_hash` and
`patch: {"layout": {"width": 2200}}`. Omitted fields remain unchanged, arrays
replace supplied arrays, and object fields merge. A new map needs a name and
sources referencing already-configured providers; LibreNMS sources require
selectors. `sources: []` supports manual-only maps with explicit inclusions.

Manual device/link changes use `manual_sha256` from `get_manual_catalog` plus
the map hash. Existing records retain their source IDs and unexposed fields.
The catalog lists deletion dependencies. New devices join the selected map;
shared record edits can affect multiple maps. Membership and traffic-interface
overrides are map-specific. Link endpoints use an inventory `device_id` or
`manual:SOURCE_ID`; interfaces use a decimal port ID, `file:SOURCE_ID`, `custom`
with a label, or an empty string for unspecified.

Every save remains a draft and invalidates prior reviews. `review_map` stores
the exact proposed changes and returns `plan_sha256`. Page through
`get_review` for operations, warnings, and conflicts. The Publish profile can
call `publish_map` with that exact hash and `confirm_warnings` when applicable.
Changes to configuration, sources, interfaces, or the live map can block
publication and require another review. A newer review also invalidates the
previous hash. The UI uses the same publish checks.

After a timeout, inspect `get_map` and its last-run record before retrying.
`published: true` with `status_refreshed: false` confirms publication succeeded;
refresh the review instead of publishing again. Mutation logs record actor and
token IDs, operation, target, outcome, and supplied plan hash, without secrets.
Requests are limited to 64 KiB and 120 requests per authenticated user per minute.

## Agent skill

Agents can read `libremapper://skill`, load the `libremapper_workflow` prompt,
or use the skill download in **Agent access**. The bundled source of truth is
[`skills/libremapper/SKILL.md`](../skills/libremapper/SKILL.md). It explains ID
resolution, shared versus map-specific edits, freshness, review, authorization,
and recovery after ambiguous outcomes.

For hosts supporting local agent skills, copy the `skills/libremapper` folder
to that host's skill directory. No token is stored in the skill and no automatic
installation changes the agent host. The server's instructions point connected
agents to the resource even when no local skill is installed.
