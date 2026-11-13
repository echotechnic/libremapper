<?php

namespace Libremapper\Plugin;

use App\Models\Device;
use App\Models\Plugin;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Http\Request;
use Illuminate\Support\Arr;
use Illuminate\Support\Facades\Log;
use InvalidArgumentException;
use Mcp\Schema\Content\TextContent;
use Mcp\Schema\Result\CallToolResult;
use RuntimeException;
use Throwable;

class McpTools
{
    public function __construct(private Engine $engine, private Plugin $plugin, private Request $request, private Model $token) {}

    public static function definitions(): array
    {
        $string = ['type' => 'string', 'minLength' => 1];
        $id = ['type' => 'integer', 'minimum' => 1];
        $hash = ['type' => 'string', 'pattern' => '^[0-9a-f]{64}$'];
        $map = ['map_key' => ['type' => 'string', 'pattern' => '^[a-z0-9]+(?:-[a-z0-9]+)*$']];
        $page = ['query' => ['type' => 'string', 'maxLength' => 255], 'cursor' => ['type' => 'string', 'maxLength' => 512], 'limit' => ['type' => 'integer', 'minimum' => 1, 'maximum' => 500, 'default' => 100]];
        $fresh = $map + ['map_hash' => $hash, 'manual_sha256' => $hash];
        $mapSchema = json_decode(file_get_contents(__DIR__.'/../resources/mcp-map-schema.json'), true, flags: JSON_THROW_ON_ERROR);
        $defs = $mapSchema['$defs'];
        unset($mapSchema['$defs']);
        $schema = static fn (array $properties, array $required = []): array => ['type' => 'object', 'properties' => $properties ?: new \stdClass, 'required' => $required, 'additionalProperties' => false];
        $tools = [];
        $add = function (string $name, int $level, string $description, array $properties = [], array $required = []) use (&$tools, $schema): void {
            $tools[$name] = ['level' => $level, 'description' => $description, 'inputSchema' => $schema($properties, $required)];
        };
        $add('list_maps', 1, 'List configured maps, their stable keys, draft state, and last-run summaries.', $page);
        $add('get_map', 1, 'Read a map configuration and map_hash before editing. No raw provider settings are returned.', $map, ['map_key']);
        $createSchema = $mapSchema;
        $createSchema['required'] = ['name', 'sources'];
        $add('create_map', 2, 'Create a map draft with an explicit unique key. Sources reference configured providers; LibreNMS sources require selectors.', $map + ['configuration' => $createSchema], ['map_key', 'configuration']);
        $add('update_map', 2, 'Patch a map draft. Omitted fields are preserved; arrays replace and object fields merge. Read get_map first.', $map + ['map_hash' => $hash, 'patch' => $mapSchema], ['map_key', 'map_hash', 'patch']);
        $add('review_map', 2, 'Read live sources and store an exact review. Does not publish. Fetch every review page before publication.', $map + ['adopt' => ['type' => 'boolean', 'default' => false], 'relayout' => ['type' => 'boolean', 'default' => false]] + $page, ['map_key']);
        $add('get_review', 1, 'Read a saved review page without regenerating it. The plan hash detects replacement between pages.', $map + ['plan_sha256' => $hash, 'section' => ['enum' => ['operations', 'warnings', 'conflicts', 'link_fallbacks']]] + $page, ['map_key', 'plan_sha256']);
        $add('publish_map', 3, 'Publish only the exact reviewed plan authorized by the user. Conflicts and freshness failures block publication.', $map + ['plan_sha256' => $hash, 'confirm_warnings' => ['type' => 'boolean', 'default' => false]], ['map_key', 'plan_sha256']);
        $add('get_manual_catalog', 1, 'Read shared manual records, dependencies, usage, and manual_sha256. Shared edits can affect every map.', ['section' => ['enum' => ['devices', 'links', 'suppress_links']]] + $page);
        $add('save_manual_device', 2, 'Create or edit a shared manual device. Omit source_id to create; retain it on edits. New devices join map_key.', $fresh + ['source_id' => $string, 'hostname' => $string, 'librenms_device_id' => ['type' => ['integer', 'null'], 'minimum' => 1], 'image' => ['type' => 'string', 'maxLength' => 255], 'layer' => ['type' => 'integer'], 'short_hostname_parts' => ['type' => ['integer', 'null'], 'minimum' => 1, 'maximum' => 127]], [...array_keys($fresh), 'hostname']);
        $add('delete_manual_device', 2, 'Delete an unused shared manual device. Links and suppressions block deletion; memberships are cleaned atomically.', $fresh + ['source_id' => $string], [...array_keys($fresh), 'source_id']);
        $endpoint = $schema(['device_id' => ['oneOf' => [$id, ['type' => 'string', 'pattern' => '^manual:.+$']]], 'interface' => ['type' => 'string', 'description' => 'Empty for unspecified; a decimal port ID, file:SOURCE_ID, or custom for a label.'], 'label' => ['type' => 'string', 'maxLength' => 255]], ['device_id']);
        $add('save_manual_link', 2, 'Create or edit a shared connection between inventory or manual devices, including two external nodes. Supply both endpoints.', $fresh + ['source_id' => $string, 'a' => $endpoint, 'b' => $endpoint], [...array_keys($fresh), 'a', 'b']);
        $add('delete_manual_link', 2, 'Remove a shared manual relationship from all relevant maps.', $fresh + ['source_id' => $string], [...array_keys($fresh), 'source_id']);
        foreach (['device', 'link'] as $kind) $add('set_manual_'.$kind.'_membership', 2, 'Set map-specific inclusion or exclusion for an existing shared manual '.$kind.'.', $fresh + ['source_id' => $string, 'included' => ['type' => 'boolean']], [...array_keys($fresh), 'source_id', 'included']);
        $add('set_traffic_interface', 2, 'Choose a real port from either reviewed endpoint, null for Automatic, or remove=true to remove an override. Changes only this map.', $map + ['map_hash' => $hash, 'canonical_id' => $string, 'port_id' => ['type' => ['integer', 'null'], 'minimum' => 1], 'remove' => ['type' => 'boolean', 'default' => false]], ['map_key', 'map_hash', 'canonical_id', 'port_id']);
        $add('list_devices', 1, 'Search inventory with last stored device status and polling timestamps.', $page);
        $add('get_device', 1, 'Read one inventory device by LibreNMS device_id, including stored status.', ['device_id' => $id], ['device_id']);
        $add('list_interfaces', 1, 'Read searchable interface choices with ownership, status, and polling timestamp. Disabled and virtual ports remain visible.', ['device_id' => $id] + $page, ['device_id']);
        $add('list_neighbors', 1, 'Read discovered neighbor records and protocol provenance. Optionally filter either endpoint by device_id.', ['device_id' => $id] + $page);
        $add('list_groups', 1, 'Read inventory device groups.', $page);
        $add('list_locations', 1, 'Read inventory locations.', $page);
        $add('inspect_provider', 1, 'Inspect parsed provider topology or bounded diagnostic summaries. Optional map_key applies that map’s parser selection and scope. Raw device configurations and credentials are never returned.', ['provider' => $string, 'map_key' => $map['map_key'], 'section' => ['enum' => ['devices', 'links', 'diagnostics']]] + $page, ['provider']);
        foreach (['create_map', 'update_map'] as $name) $tools[$name]['inputSchema']['$defs'] = $defs;
        $output = ['collected_at' => $string, 'items' => ['type' => 'array'], 'total' => ['type' => 'integer', 'minimum' => 0],
            'revision' => $hash, 'next_cursor' => ['type' => ['string', 'null']], 'map_key' => $string, 'name' => $string, 'map_name' => $string,
            'map_hash' => $hash, 'draft' => ['type' => 'boolean'], 'published' => ['type' => 'boolean'], 'configuration' => $mapSchema,
            'last_run' => ['type' => 'object'], 'workspace_url' => $string, 'live_map_url' => ['type' => ['string', 'null']],
            'manual_sha256' => $hash, 'affected_maps' => ['type' => 'array', 'items' => $string], 'source_id' => ['type' => ['string', 'null']],
            'section' => $string, 'plan_sha256' => $hash, 'generated_at' => $string, 'changes' => ['type' => 'integer'],
            'device_count' => ['type' => 'integer'], 'link_count' => ['type' => 'integer'], 'warning_count' => ['type' => 'integer'],
            'conflict_count' => ['type' => 'integer'], 'status_refreshed' => ['type' => 'boolean'], 'remaining_changes' => ['type' => ['integer', 'null']],
            'map' => ['type' => 'object'], 'device' => ['type' => 'object'], 'device_id' => $id, 'last_polled' => ['type' => ['string', 'null']],
            'provider' => $string, 'provider_type' => $string, 'blocked' => ['type' => 'boolean']];
        $mapFields = ['map_key', 'name', 'map_hash', 'draft', 'published', 'configuration', 'last_run', 'workspace_url', 'live_map_url'];
        foreach ($tools as $name => &$tool) {
            $fields = match (true) {
                in_array($name, ['get_map', 'create_map', 'update_map'], true) => $mapFields,
                $name === 'publish_map' => ['published', 'status_refreshed', 'remaining_changes', 'changes', 'plan_sha256', 'map'],
                $name === 'get_device' => ['device'],
                $name === 'set_traffic_interface' => ['map_key', 'map_hash', 'draft'],
                $tool['level'] > 1 && $name !== 'review_map' => ['map_key', 'map_hash', 'draft', 'source_id', 'manual_sha256', 'affected_maps'],
                default => ['items', 'total', 'revision', 'next_cursor', 'section', 'manual_sha256', 'affected_maps', 'map_key', 'map_name', 'plan_sha256', 'generated_at', 'changes', 'device_count', 'link_count', 'warning_count', 'conflict_count', 'device_id', 'last_polled', 'provider', 'provider_type', 'blocked'],
            };
            $tool['outputSchema'] = $schema(Arr::only($output, ['collected_at', ...$fields]), ['collected_at']);
            if (in_array('configuration', $fields, true)) $tool['outputSchema']['$defs'] = $defs;
        }
        unset($tool);
        ksort($tools);
        return $tools;
    }

    public function call(string $name, array $input): CallToolResult
    {
        $definition = self::definitions()[$name] ?? null;
        $mutating = ($definition['level'] ?? 1) > 1;
        $code = 'OK';
        try {
            abort_unless($definition && McpAccess::level(McpAccess::token($this->request)) >= $definition['level'], 403);
            $result = ['collected_at' => now()->toIso8601String()] + $this->execute($name, $input);
            return new CallToolResult([new TextContent($result)], structuredContent: $result);
        } catch (Throwable $error) {
            [$code, $message] = self::error($error);
            $result = ['error' => ['code' => $code, 'message' => $message]];
            return new CallToolResult([new TextContent($result)], isError: true, structuredContent: $result);
        } finally {
            if ($mutating) Log::info('Libremapper MCP mutation', ['user_id' => $this->request->user()->user_id, 'token_id' => $this->token->id, 'tool' => $name, 'map_key' => $input['map_key'] ?? null, 'source_id' => $result['source_id'] ?? $input['source_id'] ?? null, 'plan_sha256' => $input['plan_sha256'] ?? null, 'result' => $code]);
        }
    }

    private function execute(string $name, array $input): array
    {
        $map = $input['map_key'] ?? '';
        switch ($name) {
            case 'list_maps':
                $rows = array_map(fn ($key) => Arr::except($this->map($key), ['configuration']), $this->engine->mapKeys());
                return self::page($rows, $input);
            case 'get_map': return $this->map($map);
            case 'create_map':
            case 'update_map':
                $this->engine->saveMap($map, $input['patch'] ?? $input['configuration'], $input['map_hash'] ?? null);
                return $this->map($map);
            case 'review_map':
                $plan = $this->engine->reviewMap($this->plugin, $map, $input['adopt'] ?? false, $input['relayout'] ?? false);
                return $this->review($plan, $input);
            case 'get_review':
                $plan = $this->engine->reviewedPlan($map);
                if (! hash_equals($plan['plan_sha256'], $input['plan_sha256'])) throw new RuntimeException('Reviewed plan changed.');
                return $this->review($plan, $input);
            case 'publish_map':
                $result = $this->engine->publishMap($this->plugin, $map, $input['plan_sha256'], $input['confirm_warnings'] ?? false);
                return Arr::only($result, ['published', 'status_refreshed', 'remaining_changes', 'changes', 'plan_sha256']) + ['map' => Arr::except($this->map($map), ['configuration'])];
            case 'get_manual_catalog': return $this->catalog($input);
            case 'save_manual_device':
                $existing = collect($this->engine->manualCatalog()['devices'])->firstWhere('source_id', $input['source_id'] ?? '') ?? [];
                $input += Arr::only($existing, ['librenms_device_id', 'layer', 'short_hostname_parts']) + ['image' => $existing['attributes']['map_image'] ?? ''];
                if (($input['short_hostname_parts'] ?? null) === null) $input['short_hostname_parts'] = '';
                $sourceId = $this->engine->saveManualDevice($map, $input);
                break;
            case 'delete_manual_device': $this->engine->removeManualDevice($map, $input['source_id'], $input); break;
            case 'save_manual_link':
                foreach (['a', 'b'] as $side) {
                    foreach (['device_id', 'interface', 'label'] as $field) $input[$side.'_'.$field] = $input[$side][$field] ?? '';
                }
                $sourceId = $this->engine->saveManualLink($map, $input);
                break;
            case 'delete_manual_link': $this->engine->removeManualLink($map, $input['source_id'], $input); break;
            case 'set_manual_device_membership': $this->engine->includeManualDevice($map, $input['source_id'], $input['included'], $input); break;
            case 'set_manual_link_membership': $this->engine->includeManualLink($map, $input['source_id'], $input['included'], $input); break;
            case 'set_traffic_interface':
                $input['port_id'] ??= '';
                $this->engine->saveGraphOverride($map, $input);
                return Arr::only($this->map($map), ['map_key', 'map_hash', 'draft']);
            case 'list_devices': return self::page(NetworkInspection::devices($this->engine, $this->request->user()), $input);
            case 'get_device':
                $device = collect(NetworkInspection::devices($this->engine, $this->request->user()))->firstWhere('device_id', $input['device_id']);
                if (! $device) throw new RuntimeException('Unknown device.');
                return ['device' => $device];
            case 'list_interfaces':
                $device = Device::hasAccess($this->request->user())->findOrFail($input['device_id']);
                return self::page(InterfaceOptions::options($input['device_id'], $this->request->user()), $input) + ['device_id' => $input['device_id'], 'last_polled' => $device->last_polled?->toIso8601String()];
            case 'list_neighbors': return self::page(NetworkInspection::neighbors($this->request->user(), $input['device_id'] ?? null), $input);
            case 'list_groups': return self::page($this->engine->inventory()['groups'], $input);
            case 'list_locations': return self::page($this->engine->inventory()['locations'], $input);
            case 'inspect_provider': return $this->provider($input);
            default: throw new InvalidArgumentException('Unknown tool.');
        }
        return Arr::only($this->map($map), ['map_key', 'map_hash', 'draft']) + ['source_id' => $sourceId ?? $input['source_id'] ?? null, 'manual_sha256' => $this->engine->manualCatalog()['sha256'], 'affected_maps' => $this->engine->mapKeys()];
    }

    private function map(string $key): array
    {
        $map = $this->engine->mapConfiguration($key);
        $settings = (array) $this->plugin->fresh()->settings;
        $schema = json_decode(file_get_contents(__DIR__.'/../resources/mcp-map-schema.json'), true, flags: JSON_THROW_ON_ERROR);
        return ['map_key' => $key, 'name' => $map['name'], 'map_hash' => hash('sha256', json_encode($map, JSON_THROW_ON_ERROR)),
            'draft' => $this->engine->hasDraft($key), 'published' => in_array($key, $settings['initialized_maps'] ?? [], true),
            'configuration' => self::selectFields($map, $schema, $schema['$defs']), 'last_run' => Arr::only($settings['last_runs'][$key] ?? [], ['status', 'at', 'changes', 'warnings', 'conflicts', 'plan_sha256']) ?: new \stdClass,
            'live_map_url' => isset($settings['last_runs'][$key]['librenms_map_id']) ? url('maps/custom/'.$settings['last_runs'][$key]['librenms_map_id']) : null,
            'workspace_url' => route('plugin.settings', ['plugin' => 'libremapper', 'map' => $key])];
    }

    private function review(array $plan, array $input): array
    {
        $section = $input['section'] ?? 'operations';
        $rows = $plan[$section];
        if ($section === 'operations') $rows = array_map(fn ($row) => Arr::only($row, ['action', 'kind', 'canonical_id', 'display_name', 'layer', 'librenms_id', 'values', 'generated_values', 'preserved_fields', 'sources', 'protocol', 'a_interface', 'b_interface', 'graph', 'reconciliation_reason']), $rows);
        return self::page($rows, $input, $plan['plan_sha256']) + Arr::only($plan, ['map_key', 'map_name', 'plan_sha256', 'generated_at', 'changes', 'device_count', 'link_count'])
            + ['section' => $section, 'warning_count' => count($plan['warnings']), 'conflict_count' => count($plan['conflicts'])];
    }

    private function catalog(array $input): array
    {
        $catalog = $this->engine->manualCatalog();
        $section = $input['section'] ?? 'devices';
        $rows = $catalog[$section];
        if ($section === 'devices') $rows = array_map(function ($device) use ($catalog) {
            $safe = Arr::only($device, ['source_id', 'hostname', 'fqdn', 'management_ip', 'librenms_device_id', 'layer', 'short_hostname_parts']);
            $safe['image'] = $device['attributes']['map_image'] ?? '';
            $safe['interfaces'] = array_map(fn ($port) => Arr::only($port, ['source_id', 'name', 'if_index', 'librenms_port_id']), $device['interfaces'] ?? []);
            $safe['dependencies'] = [];
            foreach (['links', 'suppress_links'] as $kind) foreach ($catalog[$kind] as $link) {
                if (($link['a']['device'] ?? '') === $device['source_id'] || ($link['b']['device'] ?? '') === $device['source_id']) $safe['dependencies'][] = ['kind' => $kind, 'source_id' => $link['source_id'] ?? null] + self::safeLink($link);
            }
            return $safe;
        }, $rows);
        else $rows = array_map(self::safeLink(...), $rows);
        usort($rows, fn ($a, $b) => strcmp($a['source_id'] ?? json_encode($a), $b['source_id'] ?? json_encode($b)));
        return self::page($rows, $input, $catalog['sha256']) + ['section' => $section, 'manual_sha256' => $catalog['sha256'], 'affected_maps' => $this->engine->mapKeys()];
    }

    private function provider(array $input): array
    {
        $inspection = $this->engine->providerInspection($input['provider'], $input['map_key'] ?? null);
        $section = $input['section'] ?? 'diagnostics';
        if ($section === 'diagnostics') {
            $rows = [];
            foreach (['matched_files', 'matched_rules', 'skipped_files', 'resolution_failures', 'warnings'] as $kind) $rows[] = ['kind' => $kind, 'count' => count($inspection[$kind])];
            foreach ($inspection['parser_reports'] ?? [] as $parser) $rows[] = ['kind' => 'parser'] + Arr::only($parser, ['name', 'label', 'matched_files', 'links', 'unbound_interfaces', 'blocked']);
        } else {
            $fields = $section === 'devices' ? ['source_id', 'hostname', 'fqdn', 'management_ip', 'librenms_device_id', 'layer'] : ['source_id', 'a', 'b', 'protocol', 'relationship_kind'];
            $rows = array_map(fn ($row) => $section === 'links' ? self::safeLink($row) : Arr::only($row, $fields), $inspection['topology'][$section]);
            usort($rows, fn ($a, $b) => strcmp($a['source_id'], $b['source_id']));
        }
        return self::page($rows, $input) + Arr::only($inspection, ['provider', 'provider_type', 'blocked']) + ['section' => $section];
    }

    public static function page(array $rows, array $input, ?string $revision = null): array
    {
        if (($input['query'] ?? '') !== '') $rows = array_values(array_filter($rows, fn ($row) => str_contains(strtolower(json_encode($row)), strtolower($input['query']))));
        $revision ??= hash('sha256', json_encode($rows, JSON_THROW_ON_ERROR));
        $offset = 0;
        if (isset($input['cursor'])) {
            $cursor = json_decode(base64_decode($input['cursor'], true) ?: '', true);
            if (! is_array($cursor) || ! is_int($cursor['offset'] ?? null) || $cursor['offset'] < 0 || ! is_string($cursor['revision'] ?? null)) throw new InvalidArgumentException('Invalid cursor.');
            if (! hash_equals($revision, $cursor['revision'])) throw new RuntimeException('Collection changed.');
            $offset = $cursor['offset'];
        }
        $limit = min(500, max(1, $input['limit'] ?? 100));
        $next = $offset + $limit;
        return ['items' => array_slice($rows, $offset, $limit), 'total' => count($rows), 'revision' => $revision,
            'next_cursor' => $next < count($rows) ? base64_encode(json_encode(['offset' => $next, 'revision' => $revision])) : null];
    }

    private static function selectFields(mixed $value, array $schema, array $definitions): mixed
    {
        if (isset($schema['$ref'])) $schema = $definitions[basename($schema['$ref'])];
        if (is_array($value) && isset($schema['properties'])) {
            $result = [];
            foreach ($schema['properties'] as $key => $child) if (array_key_exists($key, $value)) $result[$key] = self::selectFields($value[$key], $child, $definitions);
            return $result ?: new \stdClass;
        }
        if (is_array($value) && isset($schema['items'])) return array_map(fn ($item) => self::selectFields($item, $schema['items'], $definitions), $value);
        if (is_array($value) && ($schema['type'] ?? '') === 'object') return (object) $value;
        return $value;
    }

    private static function safeLink(array $link): array
    {
        $result = Arr::only($link, ['source_id', 'protocol', 'relationship_kind']);
        foreach (['a', 'b'] as $side) {
            $result[$side] = Arr::only($link[$side], ['device', 'interface']);
            if (isset($link[$side]['interface_definition'])) {
                $definition = $link[$side]['interface_definition'];
                $result[$side]['interface_definition'] = Arr::only($definition, ['source_id', 'name', 'if_index', 'librenms_port_id']);
                $result[$side]['interface_definition']['manual_label_only'] = (bool) ($definition['attributes']['manual_label_only'] ?? false);
            }
        }
        return $result;
    }

    private static function error(Throwable $error): array
    {
        if ($error instanceof \Symfony\Component\HttpKernel\Exception\HttpExceptionInterface && $error->getStatusCode() === 403) return ['FORBIDDEN', 'This token cannot perform this operation.'];
        if ($error instanceof InvalidArgumentException) return ['VALIDATION', 'Invalid arguments or cursor.'];
        $message = $error->getMessage();
        if (preg_match('/changed|no reviewed plan/i', $message)) return ['STALE_REVISION', 'State changed or the review is missing. Reload, reconcile your edits, and review again.'];
        if (str_contains($message, 'already running')) return ['BUSY', 'Another Libremapper action is running. Retry after it finishes.'];
        if (str_contains($message, 'referenced by')) return ['DEPENDENCY', 'The manual device has dependencies. Read its catalog entry before removing references.'];
        if (preg_match('/conflicts|already exists|Confirm the reviewed warnings/i', $message)) return ['CONFLICT', 'Resolve review conflicts, duplicate identities, or unacknowledged warnings before continuing.'];
        if ($error instanceof RuntimeException) return ['VALIDATION', 'The configuration or selected records are invalid. Check the current inventory, catalog, and typed inputs.'];
        return ['OPERATION_FAILED', 'Operation failed. Inspect current status before retrying a mutation.'];
    }
}
