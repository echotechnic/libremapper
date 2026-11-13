<?php

declare(strict_types=1);

use App\Facades\LibrenmsConfig;
use App\Models\ApiToken;
use App\Models\Plugin;
use App\Models\User;
use Illuminate\Contracts\Console\Kernel;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Libremapper\Plugin\Engine;
use Libremapper\Plugin\McpAccess;

require '/opt/librenms/vendor/autoload.php';
$app = require '/opt/librenms/bootstrap/app.php';
$app->make(Kernel::class)->bootstrap();
set_exception_handler(static function (Throwable $error): void { fwrite(STDERR, $error->getMessage()."\n".$error->getTraceAsString()."\n"); exit(1); });
if (getenv('LIBREMAPPER_DISABLE_SERVICES') !== 'true') throw new RuntimeException('MCP checks require the isolated synthetic stack.');

function verify(bool $value, string $message): void { if (! $value) throw new RuntimeException($message); }

$plugin = Plugin::where('plugin_name', Engine::PLUGIN)->firstOrFail();
$originalSettings = (array) $plugin->settings;
$originalBase = LibrenmsConfig::get('base_url');
$admin = User::where('username', 'admin')->firstOrFail();
$tokens = [];
$tokenSecrets = [];
foreach (['inspect', 'edit', 'publish', 'off'] as $profile) {
    if (method_exists($admin, 'createToken')) {
        $createdToken = $admin->createToken('MCP integration '.$profile);
        $tokens[$profile] = $createdToken->accessToken;
        $tokenSecrets[$profile] = $createdToken->plainTextToken;
    } else {
        $tokens[$profile] = ApiToken::generateToken($admin, 'MCP integration '.$profile);
        $tokenSecrets[$profile] = $tokens[$profile]->token_hash;
    }
}
$key = 'mcp-check-'.bin2hex(random_bytes(4));
$engine = app(Engine::class);
$manual = $engine->manualCatalog();
$manualOriginal = collect($engine->files())->firstWhere('path', $manual['path']);
$session = null;
$rpcId = 0;

function rpc(string $method, array $params = [], string $profile = 'publish', string $era = '2026-07-28', array $headers = [], ?string $urlSuffix = null): array
{
    global $tokenSecrets, $session, $rpcId;
    $requestHeaders = ['Accept' => 'application/json, text/event-stream', 'Authorization' => 'Bearer '.$tokenSecrets[$profile]];
    if ($era === '2026-07-28') {
        $requestHeaders += ['MCP-Protocol-Version' => $era, 'Mcp-Method' => $method];
        if (isset($params['name'])) $requestHeaders['Mcp-Name'] = $params['name'];
        if (isset($params['uri'])) $requestHeaders['Mcp-Name'] = $params['uri'];
        $params['_meta'] = ['io.modelcontextprotocol/protocolVersion' => $era, 'io.modelcontextprotocol/clientCapabilities' => new stdClass];
    } elseif ($session) $requestHeaders += ['MCP-Protocol-Version' => $era, 'Mcp-Session-Id' => $session];
    $response = Http::timeout(300)->withHeaders(array_replace($requestHeaders, $headers))->post('http://127.0.0.1:8000/api/v0/libremapper/mcp'.($urlSuffix ?? ''), ['jsonrpc' => '2.0', 'id' => ++$rpcId, 'method' => $method, 'params' => $params ?: new stdClass]);
    $body = $response->body();
    if ($response->header('Mcp-Session-Id')) $session = $response->header('Mcp-Session-Id');
    if (str_starts_with($response->header('Content-Type'), 'text/event-stream')) {
        preg_match_all('/^data: (.+)$/m', $body, $matches);
        $body = end($matches[1]) ?: '{}';
    }
    $decoded = json_decode($body, true);
    return ['status' => $response->status(), 'body' => $decoded ?? $body, 'raw' => $body];
}

function tool(string $name, array $arguments = [], string $profile = 'publish', bool $expectError = false): array
{
    fwrite(STDOUT, 'MCP: '.$name."\n");
    $response = rpc('tools/call', ['name' => $name, 'arguments' => $arguments ?: new stdClass], $profile);
    verify($response['status'] === 200, 'Tool '.$name.' HTTP status: '.json_encode($response['body']));
    $result = $response['body']['result'] ?? [];
    verify(($result['isError'] ?? false) === $expectError, 'Tool '.$name.' unexpected result: '.json_encode($result));
    verify(isset($result['structuredContent']), 'Tool '.$name.' must return structured output: '.$response['raw']);
    return $result['structuredContent'];
}

function mapFresh(string $key): array { return ['map_key' => $key, 'map_hash' => tool('get_map', ['map_key' => $key])['map_hash']]; }
function manualFresh(string $key): array { return mapFresh($key) + ['manual_sha256' => tool('get_manual_catalog')['manual_sha256']]; }

try {
    LibrenmsConfig::persist('base_url', 'http://127.0.0.1:8000');
    verify(rpc('tools/list')['status'] === 404, 'MCP must be disabled by default.');
    $settings = $originalSettings + ['mcp_token_profiles' => []];
    $settings['mcp_enabled'] = true;
    foreach ($tokens as $profile => $token) if ($profile !== 'off') $settings['mcp_token_profiles'][McpAccess::profileKey($token)] = $profile;
    $plugin->settings = $settings;
    $plugin->saveOrFail();
    foreach (['inspect' => 12, 'edit' => 22, 'publish' => 23] as $profile => $minimum) {
        $response = rpc('tools/list', [], $profile);
        verify($response['status'] === 200 && isset($response['body']['result']['tools']), 'Tool discovery failed: '.$response['raw']);
        $names = array_column($response['body']['result']['tools'], 'name');
        verify(count($names) >= $minimum - 2, 'Profile tool catalog is incomplete.');
        verify(in_array('publish_map', $names) === ($profile === 'publish'), 'Publish must be independently gated.');
        verify(in_array('create_map', $names) === ($profile !== 'inspect'), 'Draft editing must be gated.');
        verify(! in_array('delete_map', $names), 'Whole-map deletion is deferred.');
    }
    verify(rpc('tools/list', [], 'off')['status'] === 403, 'Unassigned tokens must be denied.');
    verify(rpc('tools/list', headers: ['Authorization' => '', 'X-Auth-Token' => $tokenSecrets['publish']])['status'] === 200, 'Native token headers must work on both authentication backends.');
    verify(rpc('tools/list', headers: ['X-Auth-Token' => $tokenSecrets['inspect']])['status'] === 400, 'Conflicting token headers must be denied.');
    if (method_exists($admin, 'createToken')) {
        $settings['mcp_token_profiles'][(string) $tokens['off']->id] = 'publish';
        $plugin->settings = $settings; $plugin->saveOrFail();
        verify(rpc('tools/list', [], 'off')['status'] === 403, 'A numeric profile from the legacy backend must not authorize a remapped Sanctum token.');
        unset($settings['mcp_token_profiles'][(string) $tokens['off']->id]);
        $tokenSecrets['migrated'] = bin2hex(random_bytes(16));
        $tokens['migrated'] = $admin->tokens()->create(['name' => 'Migrated token', 'token' => hash('sha256', $tokenSecrets['migrated']), 'abilities' => ['*']]);
        $settings['mcp_token_profiles'][McpAccess::profileKey($tokens['migrated'])] = 'inspect';
        $plugin->settings = $settings; $plugin->saveOrFail();
        verify(rpc('tools/list', [], 'migrated')['status'] === 200, 'Migrated native tokens without an ID prefix must authenticate.');
        $createdAt = $tokens['inspect']->created_at;
        $expiration = config('sanctum.expiration');
        try {
            $tokens['inspect']->created_at = now()->subMinutes(2); $tokens['inspect']->saveOrFail();
            config(['sanctum.expiration' => 1]);
            verify(McpAccess::level($tokens['inspect']) === 0, 'The native global token lifetime must be enforced.');
        } finally {
            config(['sanctum.expiration' => $expiration]);
            $tokens['inspect']->created_at = $createdAt; $tokens['inspect']->saveOrFail();
        }
    }
    verify(rpc('tools/list', [], 'publish', headers: ['Origin' => 'https://attacker.invalid'])['status'] === 403, 'Foreign origins must be denied.');
    verify(rpc('tools/list', [], 'publish', headers: ['Host' => 'attacker.invalid'])['status'] === 403, 'Foreign hosts must be denied.');
    verify(in_array(rpc('tools/list', [], 'publish', urlSuffix: '?api_token=hidden')['status'], [400, 401], true), 'URL credentials must be denied.');
    verify(rpc('tools/list', [], 'publish', headers: ['Authorization' => 'Bearer invalid'])['status'] === 401, 'Invalid tokens must be denied.');
    $roles = $admin->getRoleNames()->all();
    try {
        $admin->syncRoles([]);
        verify(rpc('tools/list')['status'] === 403, 'An existing token cannot bypass the plugin-admin gate.');
    } finally { $admin->syncRoles($roles); }
    $invalid = rpc('tools/call', ['name' => 'create_map', 'arguments' => ['map_key' => $key, 'configuration' => ['name' => 'invalid', 'sources' => [], 'providers' => new stdClass]]]);
    verify(isset($invalid['body']['error']) || ($invalid['body']['result']['isError'] ?? false), 'Unknown privileged fields must be rejected by schema validation.');
    tool('create_map', ['map_key' => $key, 'configuration' => ['name' => '  ', 'sources' => []]], expectError: true);
    verify(! in_array($key, $engine->mapKeys(), true), 'Rejected empty names must not create a map draft.');
    $denied = rpc('tools/call', ['name' => 'publish_map', 'arguments' => ['map_key' => $key, 'plan_sha256' => str_repeat('0', 64)]], 'edit');
    verify(isset($denied['body']['error']) || ($denied['body']['result']['isError'] ?? false), 'Hidden tools must reject direct execution.');
    $skill = rpc('resources/read', ['uri' => 'libremapper://skill']);
    verify(str_contains($skill['raw'], 'manual_sha256'), 'Skill resource must be discoverable.');
    verify(isset(rpc('prompts/get', ['name' => 'libremapper_workflow'])['body']['result']['messages']), 'Workflow prompt must load.');
    $devices = tool('list_devices', ['limit' => 1]);
    verify($devices['total'] > 1 && $devices['next_cursor'], 'Device inspection must paginate.');
    verify(array_key_exists('last_polled', $devices['items'][0]), 'Device observation timestamps must be returned.');
    $deviceId = $devices['items'][0]['device_id'];
    verify(tool('get_device', ['device_id' => $deviceId])['device']['device_id'] === $deviceId, 'Device lookup must use stable inventory IDs.');
    tool('list_devices', ['limit' => 1, 'cursor' => $devices['next_cursor']]);
    $lastPolled = DB::table('devices')->where('device_id', $deviceId)->value('last_polled');
    try {
        DB::table('devices')->where('device_id', $deviceId)->update(['last_polled' => now()->toDateTimeString()]);
        verify(is_string(tool('list_interfaces', ['device_id' => $deviceId])['last_polled']), 'Observed interface timestamps must satisfy the JSON output schema.');
    } finally { DB::table('devices')->where('device_id', $deviceId)->update(['last_polled' => $lastPolled]); }
    tool('list_neighbors', ['device_id' => $deviceId]);
    tool('list_groups'); tool('list_locations'); tool('inspect_provider', ['provider' => 'artifacts']);
    $created = tool('create_map', ['map_key' => $key, 'configuration' => ['name' => 'MCP integration '.$key, 'sources' => [['provider' => 'native', 'selectors' => [['type' => 'device_id', 'value' => $deviceId], ['type' => 'group', 'value' => 'Reference Core']]]]]]);
    verify($created['draft'] && ! $created['published'], 'Map creation must remain a draft.');
    $scopedProvider = tool('inspect_provider', ['provider' => 'artifacts', 'map_key' => $key, 'section' => 'links']);
    verify($scopedProvider['total'] === 0, 'Map-aware inspection must exclude providers absent from that map.');
    verify(tool('list_maps', ['query' => $key])['items'][0]['map_key'] === $key, 'New maps must appear in inspection.');
    $file = collect($engine->files())->firstWhere('path', 'maps/'.$key.'.yaml');
    $document = Symfony\Component\Yaml\Yaml::parse($file['content']);
    $document['sources'][0]['private_setting'] = 'SYNTHETIC-SECRET-MARKER';
    $document['node_defaults']['private_setting'] = 'SYNTHETIC-SECRET-MARKER';
    $engine->saveFile($file['path'], json_encode($document, JSON_PRETTY_PRINT | JSON_THROW_ON_ERROR), $file['sha256']);
    verify(! str_contains(json_encode(tool('get_map', ['map_key' => $key])), 'SYNTHETIC-SECRET-MARKER'), 'Nested unexposed fields must not leak through map inspection.');
    tool('update_map', ['map_key' => $key, 'map_hash' => str_repeat('0', 64), 'patch' => ['name' => 'Stale']], expectError: true);
    $updated = tool('update_map', mapFresh($key) + ['patch' => ['layout' => ['width' => 2200]]]);
    verify($updated['configuration']['layout']['width'] === 2200 && $updated['configuration']['layout']['type'] === 'auto', 'Nested patch must preserve unrelated fields.');
    $review = tool('review_map', ['map_key' => $key, 'limit' => 1]);
    $oldPlan = $review['plan_sha256'];
    verify($review['next_cursor'] !== null, 'Large reviews must provide paginated operations.');
    tool('get_review', ['map_key' => $key, 'plan_sha256' => $oldPlan, 'cursor' => $review['next_cursor'], 'limit' => 1]);
    tool('get_review', ['map_key' => $key, 'plan_sha256' => $oldPlan, 'section' => 'warnings']);
    tool('update_map', mapFresh($key) + ['patch' => ['node_defaults' => ['size' => 35]]]);
    tool('publish_map', ['map_key' => $key, 'plan_sha256' => $oldPlan, 'confirm_warnings' => true], expectError: true);
    $review = tool('review_map', ['map_key' => $key]);
    $replacedHash = $review['plan_sha256'];
    $review = tool('review_map', ['map_key' => $key]);
    verify($review['plan_sha256'] !== $replacedHash, 'Regeneration must identify the exact new review.');
    tool('publish_map', ['map_key' => $key, 'plan_sha256' => $replacedHash, 'confirm_warnings' => true], expectError: true);
    $published = tool('publish_map', ['map_key' => $key, 'plan_sha256' => $review['plan_sha256'], 'confirm_warnings' => true]);
    verify($published['published'] && $published['map']['published'], 'Authorized reviewed publication must succeed.');
    verify($published['map']['live_map_url'] !== null, 'Published maps must return a live map link.');
    $review = tool('review_map', ['map_key' => $key]);
    $graphChecked = false;
    foreach ($review['items'] as $operation) if (($operation['kind'] ?? '') === 'edge') {
        tool('set_traffic_interface', mapFresh($key) + ['canonical_id' => $operation['canonical_id'], 'port_id' => 2147483647], expectError: true);
        tool('set_traffic_interface', mapFresh($key) + ['canonical_id' => $operation['canonical_id'], 'port_id' => null]);
        $graphChecked = true;
        break;
    }
    verify($graphChecked, 'A discovered connection must exercise the traffic-interface tool.');
    tool('save_manual_device', manualFresh($key) + ['hostname' => 'mcp-external-'.$key, 'layer' => 4]);
    $catalog = tool('get_manual_catalog', ['query' => 'mcp-external-'.$key]);
    $source = $catalog['items'][0]['source_id'];
    $edited = tool('save_manual_device', manualFresh($key) + ['source_id' => $source, 'hostname' => 'mcp-renamed-'.$key]);
    verify($edited['source_id'] === $source && tool('get_manual_catalog', ['query' => 'mcp-renamed-'.$key])['items'][0]['layer'] === 4, 'Device edits must retain source identity and omitted placement.');
    tool('save_manual_device', mapFresh($key) + ['manual_sha256' => str_repeat('0', 64), 'source_id' => $source, 'hostname' => 'stale'], expectError: true);
    tool('save_manual_link', manualFresh($key) + ['a' => ['device_id' => $deviceId], 'b' => ['device_id' => 'manual:'.$source, 'interface' => 'custom', 'label' => 'external']]);
    $dependency = tool('delete_manual_device', manualFresh($key) + ['source_id' => $source], expectError: true);
    verify($dependency['error']['code'] === 'DEPENDENCY', 'Device dependencies must block deletion.');
    $catalog = tool('get_manual_catalog', ['section' => 'links', 'query' => $source]);
    $link = $catalog['items'][0]['source_id'];
    $edited = tool('save_manual_link', manualFresh($key) + ['source_id' => $link, 'a' => ['device_id' => $deviceId], 'b' => ['device_id' => 'manual:'.$source, 'interface' => 'custom', 'label' => 'renamed label']]);
    verify($edited['source_id'] === $link, 'Link edits must retain source identity.');
    tool('set_manual_link_membership', manualFresh($key) + ['source_id' => $link, 'included' => false]);
    tool('set_manual_device_membership', manualFresh($key) + ['source_id' => $source, 'included' => false]);
    tool('delete_manual_link', manualFresh($key) + ['source_id' => $link]);
    tool('delete_manual_device', manualFresh($key) + ['source_id' => $source]);
    tool('update_map', mapFresh($key) + ['patch' => ['sources' => [], 'link_graph_overrides' => [], 'manual_device_inclusions' => [], 'manual_device_exclusions' => []]]);
    $externalA = tool('save_manual_device', manualFresh($key) + ['hostname' => 'standalone-a-'.$key])['source_id'];
    $externalB = tool('save_manual_device', manualFresh($key) + ['hostname' => 'standalone-b-'.$key])['source_id'];
    $externalLink = tool('save_manual_link', manualFresh($key) + ['a' => ['device_id' => 'manual:'.$externalA], 'b' => ['device_id' => 'manual:'.$externalB]])['source_id'];
    $externalReview = tool('review_map', ['map_key' => $key]);
    verify($externalReview['device_count'] === 2 && $externalReview['link_count'] === 1, 'Manual-only maps must include standalone external devices and their connection.');
    tool('delete_manual_link', manualFresh($key) + ['source_id' => $externalLink]);
    tool('delete_manual_device', manualFresh($key) + ['source_id' => $externalA]);
    tool('delete_manual_device', manualFresh($key) + ['source_id' => $externalB]);
    $initialize = rpc('initialize', ['protocolVersion' => '2025-11-25', 'capabilities' => new stdClass, 'clientInfo' => ['name' => 'integration', 'version' => '1']], era: '2025-11-25');
    verify($initialize['status'] === 200 && $session, 'Older clients must initialize with a session.');
    verify(isset(rpc('tools/list', era: '2025-11-25')['body']['result']['tools']), 'Legacy session must work across requests.');
    verify(rpc('tools/list', profile: 'inspect', era: '2025-11-25')['status'] === 404, 'Sessions cannot cross token identities.');
    if (method_exists($admin, 'createToken')) $tokens['publish']->expires_at = now()->subDay();
    else $tokens['publish']->disabled = true;
    $tokens['publish']->saveOrFail();
    verify(rpc('tools/list')['status'] === 401, 'Revoked tokens must stop working immediately.');
    $tokens['inspect']->delete();
    verify(rpc('tools/list', profile: 'inspect')['status'] === 401, 'Deleted tokens must stop working immediately.');
    echo "MCP HTTP integration checks passed.\n";
} finally {
    $ids = DB::table('custom_maps')->where('name', 'MCP integration '.$key)->pluck('custom_map_id');
    DB::table('custom_map_edges')->whereIn('custom_map_id', $ids)->delete();
    DB::table('custom_map_nodes')->whereIn('custom_map_id', $ids)->delete();
    DB::table('custom_maps')->whereIn('custom_map_id', $ids)->delete();
    $file = collect($engine->files())->firstWhere('path', 'maps/'.$key.'.yaml');
    if ($file) $engine->deleteFile($file['path'], $file['sha256']);
    if ($manualOriginal) $engine->saveFile($manualOriginal['path'], $manualOriginal['content'], $engine->manualCatalog()['sha256']);
    $plugin->settings = $originalSettings; $plugin->saveOrFail();
    LibrenmsConfig::persist('base_url', $originalBase);
    foreach ($tokens as $token) $token->delete();
}
