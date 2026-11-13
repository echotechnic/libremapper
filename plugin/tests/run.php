<?php

declare(strict_types=1);

use App\Models\Plugin;
use App\Models\User;
use Illuminate\Contracts\Console\Kernel;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Route;
use Libremapper\Plugin\Engine;
use Libremapper\Plugin\Hooks\MenuEntry;
use Libremapper\Plugin\Hooks\Settings;
use Libremapper\Plugin\Http\LibremapperController;
use Libremapper\Plugin\InterfaceOptions;
use Symfony\Component\Yaml\Yaml;

require '/opt/librenms/vendor/autoload.php';
$app = require '/opt/librenms/bootstrap/app.php';
$app->make(Kernel::class)->bootstrap();
set_exception_handler(static function (Throwable $error): void {
    fwrite(STDERR, $error->getMessage()."\n".$error->getTraceAsString()."\n");
    exit(1);
});

function check(bool $condition, string $message): void
{
    if (! $condition) {
        throw new RuntimeException($message);
    }
}

function fails(callable $callback, string $message): void
{
    try {
        $callback();
    } catch (Throwable $error) {
        check(str_contains($error->getMessage(), $message), "Unexpected error: {$error->getMessage()}");

        return;
    }

    throw new RuntimeException("Expected failure containing: $message");
}

function cleanupPhpFreshnessMaps(): void
{
    $mapIds = DB::table('custom_maps')->where('name', 'like', 'PHP Freshness %')
        ->pluck('custom_map_id');
    DB::table('custom_map_edges')->whereIn('custom_map_id', $mapIds)->delete();
    DB::table('custom_map_nodes')->whereIn('custom_map_id', $mapIds)->delete();
    DB::table('custom_maps')->whereIn('custom_map_id', $mapIds)->delete();
}

function removeTestTree(string $root): void
{
    if (! is_dir($root)) {
        return;
    }
    foreach (scandir($root) ?: [] as $name) {
        if ($name === '.' || $name === '..') {
            continue;
        }
        $path = $root.'/'.$name;
        if (is_dir($path) && ! is_link($path)) {
            removeTestTree($path);
        } else {
            unlink($path);
        }
    }
    rmdir($root);
}

$plugin = Plugin::query()->where('plugin_name', Engine::PLUGIN)->where('version', 2)->firstOrFail();
$admin = User::query()->where('username', 'admin')->firstOrFail();
$viewer = new User;
check((new MenuEntry)->authorize($admin), 'Admin must see the menu hook.');
check(! (new MenuEntry)->authorize($viewer), 'Non-admin must not see the menu hook.');
check((new Settings)->authorize($admin), 'Admin must see the settings hook.');
$hook = (new Settings)->handle(Engine::PLUGIN, (array) $plugin->settings);
check($hook['content_view'] === 'libremapper::settings', 'Settings hook must render the native view.');
check(isset($hook['settings']['files']), 'Settings hook must supply the complete admin UI data.');

$routes = collect(Route::getRoutes()->getRoutes())->filter(
    fn ($route) => str_starts_with($route->uri(), 'plugin/libremapper')
);
check($routes->count() === 27, 'All twenty-seven workspace routes must load.');
check(
    $routes->every(fn ($route) => in_array('can:plugin.admin', $route->middleware(), true)),
    'Every plugin route must require plugin admin authorization.'
);
$redirect = (new LibremapperController(app(Engine::class)))->index(
    Request::create('/plugin/libremapper?map=generated-core-top')
);
check(str_contains($redirect->getTargetUrl(), '/plugin/settings/libremapper'), 'Compatibility route must redirect to native Settings.');
foreach (['guided' => 'devices', 'files' => 'advanced', 'oxidized' => 'links', 'links' => 'links'] as $legacyTab => $workspaceTab) {
    $legacyRedirect = (new LibremapperController(app(Engine::class)))->index(
        Request::create('/plugin/libremapper?map=generated-core-top&tab='.$legacyTab)
    );
    check(str_contains($legacyRedirect->getTargetUrl(), 'tab='.$workspaceTab), "Legacy $legacyTab bookmarks must open $workspaceTab.");
    if ($legacyTab === 'files' || $legacyTab === 'oxidized') {
        check(str_contains($legacyRedirect->getTargetUrl(), 'subtab='.($legacyTab === 'files' ? 'files' : 'parsers')),
            "Legacy $legacyTab bookmarks must retain their destination subtab.");
    }
}
$legacyFileRedirect = (new LibremapperController(app(Engine::class)))->index(
    Request::create('/plugin/libremapper?map=generated-core-top&tab=files&file=manual-topology.yaml')
);
check(str_contains($legacyFileRedirect->getTargetUrl(), 'file=manual-topology.yaml'), 'Legacy file bookmarks must retain the selected Advanced file.');
$portDefaults = [
    'ifName' => 'Ethernet1', 'ifDescr' => 'Physical port', 'ifAlias' => 'uplink',
    'ifType' => 'ethernetCsmacd', 'ifSpeed' => 1000000000,
    'ifAdminStatus' => 'up', 'ifOperStatus' => 'down', 'ifIndex' => 1,
    'disabled' => 0, 'deleted' => 0,
];
$classified = InterfaceOptions::classify([
    ['port_id' => 8, 'ifName' => 'loopback0', 'ifType' => 'softwareLoopback'] + $portDefaults,
    ['port_id' => 7, 'ifName' => 'Ethernet7', 'disabled' => 1] + $portDefaults,
    ['port_id' => 6, 'ifName' => 'Ethernet6', 'ifSpeed' => 0] + $portDefaults,
    ['port_id' => 5, 'ifName' => 'Ethernet5', 'deleted' => 1] + $portDefaults,
    ['port_id' => 4, 'ifName' => 'Ethernet4', 'ifAdminStatus' => 'down'] + $portDefaults,
    ['port_id' => 3, 'ifName' => 'DUPLICATE'] + $portDefaults,
    ['port_id' => 2, 'ifName' => 'duplicate'] + $portDefaults,
    ['port_id' => 1] + $portDefaults,
]);
check($classified[0]['port_id'] === 1 && count(array_filter($classified, fn ($port) => $port['usable'])) === 1, 'Only enabled positive-speed physical interfaces with unique names belong first.');
check($classified[0]['alias'] === 'uplink' && $classified[0]['oper_status'] === 'down'
    && str_contains($classified[0]['text'], 'ifIndex 1; port ID 1'), 'Interface choices must expose status, aliases, and unambiguous IDs.');

function dumpTestConfig(array $value, int $inline = 8, int $indent = 2, int $flags = 0): string
{
    return preg_replace('/^(\s*)(maps|parsers|link_inclusions|link_exclusions|manual_device_inclusions|manual_device_exclusions|link_graph_overrides): \{\s*\}$/m', '$1$2: []', Yaml::dump($value, $inline, $indent, $flags));
}

function manualInput(Engine $engine, string $map, array $input = []): array
{
    $config = collect($engine->configuration()['maps'])->firstWhere('key', $map);
    return $input + ['manual_sha256' => $engine->manualCatalog()['sha256'],
        'map_hash' => hash('sha256', json_encode($config, JSON_THROW_ON_ERROR))];
}

$testRoot = '/data/libremapper/runtime/candidate-'.bin2hex(random_bytes(8));
$mapName = 'PHP Freshness '.bin2hex(random_bytes(4));
cleanupPhpFreshnessMaps();
$cleanupPaths = [$testRoot];
$cleanup = static function () use (&$cleanupPaths): void {
    try {
        cleanupPhpFreshnessMaps();
    } catch (Throwable $error) {
        fwrite(STDERR, "PHP freshness database cleanup failed: {$error->getMessage()}\n");
    } finally {
        foreach ($cleanupPaths as $path) {
            removeTestTree($path);
        }
        foreach (glob('/data/libremapper/state.sqlite3*') ?: [] as $stateFile) {
            @unlink($stateFile);
        }
        foreach (['php-freshness', 'php-other', 'php-inline', 'guided-created', 'php-freshness-2'] as $testMapKey) {
            @unlink('/data/libremapper/drafts/'.hash('sha256', $testMapKey).'.pending');
        }
    }
};
register_shutdown_function($cleanup);
$configRoot = $testRoot;
mkdir($configRoot.'/maps', 0770, true);
$main = <<<YAML
version: 1
state_path: /data/libremapper/state.sqlite3
map_directory: maps
librenms: {}
providers:
  native: {type: librenms, settings: {}}
  external: {type: file, settings: {path: manual-topology.yaml}}
maps: []
YAML;
$manual = "version: 1\ndevices: []\nlinks: []\nsuppress_links: []\n";
file_put_contents($configRoot.'/libremapper.yaml', $main);
file_put_contents($configRoot.'/manual-topology.yaml', $manual);
$engine = new Engine(null, '/data/libremapper', $configRoot);
$engine->validateCurrent();
fails(
    fn () => (new Engine(null, '/data/libremapper', $configRoot, 'wrong-token'))->validateCurrent(),
    'unauthorized'
);

$map = <<<YAML
key: php-freshness
name: {$mapName}
example: true
scheduled: false
sources:
  - provider: native
    selectors: [{type: device_id, value: 1001}]
YAML;
$engine->createFile('maps/php-freshness.yaml', $map);
check($engine->mapKeys() === ['php-freshness'], 'Created map fragments must load.');
$controller = new LibremapperController($engine);
view()->share('errors', new \Illuminate\Support\ViewErrorBag);
$guidedData = $controller->viewData(Request::create(
    '/plugin/settings/libremapper?map=php-freshness'
));
$guidedHtml = view('libremapper::settings', $guidedData)->render();
check($guidedData['selectedKey'] === 'php-freshness', 'The requested Guided map must be selected.');
check(str_contains($guidedHtml, 'id="map" name="map"'), 'Guided must render a map dropdown.');
check(
    preg_match('/<option value="php-freshness"[^>]*selected[^>]*>\s*'.preg_quote($mapName, '/').'\s*<\/option>/', $guidedHtml) === 1,
    'The selected map must show its operator-facing name.'
);
check(str_contains($guidedHtml, '<option value="__new__"'), 'Guided must offer new-map mode.');
$fallbackData = $controller->viewData(Request::create(
    '/plugin/settings/libremapper?map=does-not-exist'
));
$arrayFallbackData = $controller->viewData(Request::create(
    '/plugin/settings/libremapper?map[]=does-not-exist'
));
check(
    $fallbackData['selectedKey'] === 'php-freshness'
        && $fallbackData['map']['name'] === $mapName
        && $arrayFallbackData['selectedKey'] === 'php-freshness',
    'Unknown Guided map selections must fall back to the first configured map.'
);
$newMapData = $controller->viewData(Request::create(
    '/plugin/settings/libremapper?map=__new__'
));
$newMapHtml = view('libremapper::settings', $newMapData)->render();
check($newMapData['newMap'] && $newMapData['selectedKey'] === '__new__', 'New Guided map mode must be selected.');
check(
    str_contains($newMapHtml, 'id="name" name="name"')
        && ! str_contains($newMapHtml, 'name="key"')
        && ! str_contains($newMapHtml, 'name="provider"'),
    'New maps must ask for a name and infer the sole renamed LibreNMS provider and safe key.'
);
check(
    str_contains($newMapHtml, 'Create map')
        && ! str_contains($newMapHtml, 'Publish to LibreNMS')
        && ! str_contains($newMapHtml, 'name="selectors_yaml"')
        && ! str_contains($newMapHtml, 'name="layout_type"'),
    'Creation must need no YAML, layout choice, or publishing step.'
);
check(
    count($newMapData['inventoryGroups']) === 4
        && collect($newMapData['inventoryGroups'])->firstWhere('name', 'Reference Core')['device_count'] === 2
        && collect($newMapData['inventoryGroups'])->every(fn ($group) => $group['device_count'] > 0),
    'Device scope must use populated inventory groups with device counts.'
);
foreach (['Devices', 'Links', 'Layout', 'Review', 'Advanced', 'Device scope', 'Match any rule',
    'Exact group', 'Exact location', 'Hostname matches regex', 'sysName matches regex',
    'Hardware matches regex', 'Device type matches regex', 'Reference Core', 'Add rule', 'Remove rule'] as $copy) {
    check(str_contains($newMapHtml, $copy), "Map creation must render: $copy");
}

$oxidizedDataRoot = '/data/libremapper/runtime/oxidized-'.bin2hex(random_bytes(8));
$oxidizedConfigRoot = '/data/libremapper/runtime/oxidized-config-'.bin2hex(random_bytes(8));
$failedOxidizedRoot = '/data/libremapper/runtime/oxidized-'.bin2hex(random_bytes(8));
array_push($cleanupPaths, $oxidizedDataRoot, $oxidizedConfigRoot, $failedOxidizedRoot);
mkdir($oxidizedConfigRoot.'/maps', 0770, true);
file_put_contents($oxidizedConfigRoot.'/libremapper.yaml', $main);
file_put_contents($oxidizedConfigRoot.'/manual-topology.yaml', $manual);
$configurationEngine = new Engine(null, $oxidizedDataRoot, $oxidizedConfigRoot, 'synthetic-token');
$originalHttp = Http::getFacadeRoot();
$connectionFailure = false;
$sourceTestCalls = 0;
Http::swap(new \Illuminate\Http\Client\Factory);
Http::fake(function ($request) use (&$connectionFailure, &$sourceTestCalls) {
    $operation = $request['operation'];
    if ($operation === 'test-source') {
        $sourceTestCalls++;
        if ($connectionFailure) return Http::response(['exit_code' => 1, 'stdout' => '', 'stderr' => 'connection failed']);
    }
    $stdout = $operation === 'catalog' ? json_encode(['items' => [
        ['id' => 'saos10-lldp-neighbors'], ['id' => 'saos10-nni-config'],
    ], 'selections' => []], JSON_THROW_ON_ERROR) : 'verified';
    return Http::response(['exit_code' => 0, 'stdout' => $stdout, 'stderr' => '']);
});
try {
    $configurationEngine->configureOxidized(
        'gitea', 'git@gitea.example.net:network/oxidized.git', 'main',
        "-----BEGIN OPENSSH PRIVATE KEY-----\ntest-fetch-key\n-----END OPENSSH PRIVATE KEY-----"
    );
    $savedOxidizedContent = file_get_contents($oxidizedConfigRoot.'/libremapper.yaml');
    $configuredOxidized = Yaml::parse($savedOxidizedContent)['providers']['oxidized'];
    $storedKey = $oxidizedDataRoot.'/oxidized/id_git_fetch';
    $storedKnownHosts = $oxidizedDataRoot.'/oxidized/known_hosts';
    check(
        $configuredOxidized['type'] === 'oxidized'
            && $configuredOxidized['settings']['repository'] === 'git@gitea.example.net:network/oxidized.git'
            && $configuredOxidized['settings']['private_key'] === $storedKey
            && $configuredOxidized['settings']['known_hosts'] === $storedKnownHosts
            && $configuredOxidized['settings']['accept_new_host_key'] === true
            && fileperms($storedKey) % 01000 === 0600
            && fileperms($storedKnownHosts) % 01000 === 0600
            && ! str_contains($savedOxidizedContent, 'test-fetch-key'),
        'Remote Git setup must protect the key and store only managed credential paths.'
    );
    $configurationEngine->configureOxidized('local', '', 'main');
    $configuredLocalOxidized = Yaml::parseFile($oxidizedConfigRoot.'/libremapper.yaml')['providers']['oxidized'];
    check($configuredLocalOxidized['settings'] === ['repository' => '/oxidized.git', 'ref' => 'main'],
        'Local Oxidized setup must use the read-only repository mount without credentials.');
    $selectedParsers = ['saos10-lldp-neighbors', 'saos10-nni-config'];
    $configurationEngine->configureOxidized('local', '', 'main', '', $selectedParsers);
    $customOxidized = Yaml::parseFile($oxidizedConfigRoot.'/libremapper.yaml');
    check($customOxidized['providers']['oxidized']['settings']['parsers'] === $selectedParsers,
        'Legacy Oxidized setup must preserve multiple selected default parsers.');
    $customOxidized['providers']['oxidized']['settings']['fetch_timeout'] = 90;
    file_put_contents($oxidizedConfigRoot.'/libremapper.yaml', Yaml::dump($customOxidized, 12, 2));
    $configurationEngine->configureOxidized('local', '', 'other-ref');
    $savedSettings = Yaml::parseFile($oxidizedConfigRoot.'/libremapper.yaml')['providers']['oxidized']['settings'];
    check($savedSettings['parsers'] === $selectedParsers && $savedSettings['fetch_timeout'] === 90,
        'Connection edits must preserve legacy parser defaults and other advanced settings.');
    foreach ([[], ['unknown'], ['saos10-lldp-neighbors', 'saos10-lldp-neighbors']] as $invalidParsers) {
        fails(fn () => $configurationEngine->configureOxidized('local', '', 'main', '', $invalidParsers), 'supported Oxidized parser');
    }
    $beforeFailure = file_get_contents($oxidizedConfigRoot.'/libremapper.yaml');
    $connectionFailure = true;
    $failedConfigurationEngine = new Engine(null, $failedOxidizedRoot, $oxidizedConfigRoot, 'synthetic-token');
    fails(fn () => $failedConfigurationEngine->configureOxidized(
        'gitea', 'git@gitea.example.net:network/oxidized.git', 'main',
        "-----BEGIN OPENSSH PRIVATE KEY-----\ntest-fetch-key\n-----END OPENSSH PRIVATE KEY-----"
    ), 'connection failed');
    check(! file_exists($failedOxidizedRoot.'/oxidized/id_git_fetch')
        && ! file_exists($failedOxidizedRoot.'/oxidized/known_hosts')
        && file_get_contents($oxidizedConfigRoot.'/libremapper.yaml') === $beforeFailure,
        'A failed connection test must roll back new credentials and leave configuration unchanged.');
    check($sourceTestCalls === 5, 'Every saved connection must test only Git connectivity before committing.');
    check(Http::recorded(fn ($request) => $request['operation'] === 'test-source')->isNotEmpty(), 'Connection setup must test the source.');
    check(Http::recorded(fn ($request) => str_contains($request->body(), 'test-fetch-key'))->isEmpty(), 'Connection setup must not send private key contents to the sidecar.');
} finally {
    Http::swap($originalHttp);
}
$emptyInventoryEngine = new class(null, '/data/libremapper', $configRoot) extends Engine
{
    public function inventory(): array
    {
        return ['groups' => [], 'locations' => []];
    }
};
$emptyInventoryData = (new LibremapperController($emptyInventoryEngine))->viewData(Request::create(
    '/plugin/settings/libremapper?map=__new__'
));
check(
    $emptyInventoryData['inventoryGroups'] === [] && $emptyInventoryData['inventoryLocations'] === []
        && str_contains(view('libremapper::settings', $emptyInventoryData)->render(), 'Hostname matches regex'),
    'Empty group inventory must retain regex-based device scope.'
);
$inlineRoot = '/data/libremapper/runtime/candidate-'.bin2hex(random_bytes(8));
$cleanupPaths[] = $inlineRoot;
mkdir($inlineRoot);
file_put_contents($inlineRoot.'/libremapper.yaml', <<<YAML
version: 1
providers:
  native: {type: librenms, settings: {}}
maps: []
YAML);
$inlineEngine = new Engine(null, '/data/libremapper', $inlineRoot);
$inlineData = (new LibremapperController($inlineEngine))->viewData(Request::create(
    '/plugin/settings/libremapper?map=__new__'
));
check(
    ! $inlineData['canCreateGuidedMap']
        && str_contains(view('libremapper::settings', $inlineData)->render(), 'Configure a map directory in Advanced'),
    'Guided creation without map_directory must point users to Files for recovery.'
);
$keylessMap = ['name' => 'Keyless Inline', 'sources' => [
    ['provider' => 'native', 'selectors' => [['type' => 'device_id', 'value' => 1001]]],
]];
file_put_contents($inlineRoot.'/libremapper.yaml', dumpTestConfig([
    'version' => 1, 'state_path' => '/data/libremapper/state.sqlite3',
    'providers' => ['native' => ['type' => 'librenms', 'settings' => []]], 'maps' => [$keylessMap],
], 12, 2));
$keylessData = (new LibremapperController($inlineEngine))->viewData(Request::create(
    '/plugin/settings/libremapper?map=keyless-inline'
));
$keylessDraft = $inlineEngine->guidedFile('keyless-inline', [
    'map_hash' => hash('sha256', json_encode($keylessData['map'], JSON_THROW_ON_ERROR)),
    'name' => 'Renamed Inline',
]);
$keylessSaved = Yaml::parse($keylessDraft['content'])['maps'][0];
check($keylessSaved['key'] === 'keyless-inline' && $keylessSaved['name'] === 'Renamed Inline',
    'Keyless legacy inline maps must accept their displayed draft hash and keep identity on rename.');
$advancedUiData = $controller->viewData(Request::create('/plugin/settings/libremapper?map=php-freshness&tab=advanced'));
$configurationHtml = view('libremapper::settings', $advancedUiData)->render();
$appearanceUiData = array_replace($advancedUiData, ['activeSubtab' => 'appearance']);
$appearanceHtml = view('libremapper::settings', $appearanceUiData)->render();
$guidedHtml = $configurationHtml.$appearanceHtml;
check(! str_contains($configurationHtml, 'id="files"') && ! str_contains($configurationHtml, 'id="agent-access"')
    && ! str_contains($configurationHtml, 'id="layout_type"') && ! str_contains($appearanceHtml, 'id="selectors_yaml"')
    && ! str_contains($configurationHtml, 'name="layout_auto_grow"') && ! str_contains($appearanceHtml, 'name="scheduled"'),
    'Advanced subtabs must show only their own settings.');
foreach (['Devices to include (YAML)', 'Adjust layers after discovery (YAML)', 'type: canonical',
    'type: attribute', 'Advanced map display', 'Advanced layout tuning', 'Advanced appearance',
    'Save configuration', 'list="node-image-options"'] as $copy) {
    check(str_contains($guidedHtml, $copy), "Advanced must retain configuration controls: $copy");
}
check(in_array('router.svg', $guidedData['nodeImages'], true), 'Current Custom Map icons must populate image suggestions.');
$layoutData = $controller->viewData(Request::create('/plugin/settings/libremapper?map=php-freshness&tab=layout'));
$topologyHtml = view('libremapper::settings', $layoutData)->render();
$overridesData = $controller->viewData(Request::create('/plugin/settings/libremapper?map=php-freshness&tab=layout&subtab=overrides'));
$overridesHtml = view('libremapper::settings', $overridesData)->render();
$layoutHtml = $topologyHtml.$overridesHtml;
check(! str_contains($topologyHtml, 'id="device-list"') && ! str_contains($overridesHtml, 'id="map-layout-form"'),
    'Layout must separate topology controls from individual overrides.');
$scopeHtml = view('libremapper::settings', $guidedData)->render();
$matchedHtml = view('libremapper::settings', array_replace($guidedData, ['activeSubtab' => 'matched']))->render();
check(! str_contains($scopeHtml, 'id="device-list"') && str_contains($matchedHtml, 'id="device-list"')
    && ! str_contains($matchedHtml, 'id="device-scope-form"'), 'Matched devices must have a separate subtab from scope rules.');
check(str_contains($layoutHtml, 'name="node_label"') && preg_match('/name="short_hostname_parts" value="2"/', $layoutHtml) === 1,
    'Layout must expose label format and default to the first two hostname components.');
check(substr_count($layoutHtml, 'name="hostname_parts"') === 1
    && str_contains($layoutHtml, 'id="hostname-check-all"')
    && substr_count($layoutHtml, 'name="hostname_device_ids[]"') === count($overridesData['inventoryDevices']),
    'Device labels must use one shared component field and a checkbox for every inventory device.');
$tierData = $layoutData;
$tierData['plan'] = ['changes' => 0, 'warnings' => [], 'conflicts' => [], 'operations' => []];
foreach ([1001, null, 1002, null] as $layer => $deviceId) {
    $tierData['plan']['operations'][] = ['kind' => 'node', 'action' => 'create', 'canonical_id' => 'tier-'.$layer,
        'layer' => $layer, 'display_name' => 'Tier test '.$layer,
        'generated_values' => ['device_id' => $deviceId, 'label' => 'Tier test '.$layer, 'x_pos' => 100, 'y_pos' => 100 * $layer]];
}
$tierDom = new DOMDocument;
@$tierDom->loadHTML(view('libremapper::settings', $tierData)->render()
    .view('libremapper::settings', array_replace($overridesData, ['plan' => $tierData['plan']]))->render());
$tierXPath = new DOMXPath($tierDom);
check(trim($tierXPath->evaluate('string(//tr[@data-device-id="1002"]/td[5])')) === 'Tier 3'
    && trim($tierXPath->evaluate('string(//*[local-name()="g"][@data-device-id="1002"]/*[@class="preview-tier"])')) === 'Tier 3',
    'Preview and Placement must rank tiers identically when external devices occupy other tiers.');
foreach ([
    'auto' => 'Automatic',
    'force' => 'Organic',
    'top' => 'Top to bottom',
    'left' => 'Left to right',
    'radial' => 'Radial rings',
    'full' => 'Full hostname',
    'short_hostname' => 'Short hostname',
    'sys_name' => 'LibreNMS device name (sysName)',
    'packed' => 'Simple layer ordering',
    'constrained' => 'Keep connected branches together',
] as $value => $label) {
    check(
        preg_match('/<option value="'.preg_quote($value, '/').'"[^>]*>'.preg_quote($label, '/').'<\/option>/', $guidedHtml) === 1,
        "$label must submit the unchanged $value value."
    );
}
foreach (['box', 'circle', 'database', 'ellipse', 'text', 'image', 'circularImage', 'diamond', 'dot', 'star', 'triangle', 'triangleDown', 'hexagon', 'square', 'icon'] as $style) {
    check(str_contains($guidedHtml, '<option value="'.$style.'"'), "Node style $style must be selectable.");
}
foreach (['dynamic', 'continuous', 'discrete', 'diagonalCross', 'straightCross', 'horizontal', 'vertical', 'curvedCW', 'curvedCCW', 'cubicBezier'] as $style) {
    check(str_contains($guidedHtml, '<option value="'.$style.'"'), "Link style $style must be selectable.");
}
check(
    preg_match('/class="collapse" id="selector-examples"/', $guidedHtml) === 1
        && str_contains($guidedHtml, 'data-target="#selector-examples" aria-expanded="false"'),
    'YAML examples must start closed with accessible disclosure state.'
);
check(str_contains($guidedHtml, 'Layout dimensions'), 'Advanced must explain layout dimensions.');
foreach (['advanced-map-display', 'advanced-layout-tuning', 'advanced-appearance'] as $section) {
    check(
        ! preg_match('/class="panel-collapse collapse in" id="'.preg_quote($section, '/').'"/', $guidedHtml),
        "Default $section controls must start collapsed."
    );
}
$guidedErrors = new \Illuminate\Support\ViewErrorBag;
$guidedErrors->put('default', new \Illuminate\Support\MessageBag(['libremapper' => ['Invalid guided input.']]));
view()->share('errors', $guidedErrors);
$guidedErrorHtml = view('libremapper::settings', $appearanceUiData)->render();
foreach (['advanced-map-display', 'advanced-layout-tuning', 'advanced-appearance'] as $section) {
    check(
        preg_match('/class="panel-collapse collapse in" id="'.preg_quote($section, '/').'"/', $guidedErrorHtml) === 1,
        "Validation errors must open $section."
    );
}
view()->share('errors', new \Illuminate\Support\ViewErrorBag);
$fileData = $controller->viewData(Request::create(
    '/plugin/settings/libremapper?tab=files&file=manual-topology.yaml'
));
check(
    array_column($fileData['files'], 'path') === [
        'libremapper.yaml', 'manual-topology.yaml', 'maps/php-freshness.yaml',
    ],
    'File choices must be lexical.'
);
check($fileData['selectedFile']['path'] === 'manual-topology.yaml', 'The requested file must be selected.');
check($controller->viewData(Request::create('/plugin/settings/libremapper?tab=advanced&subtab=configuration&file=manual-topology.yaml'))['activeSubtab'] === 'files',
    'Existing underlying-file bookmarks must open the Files subtab.');
$fileHtml = view('libremapper::settings', $fileData)->render();
check(substr_count($fileHtml, 'name="content"') === 1, 'Existing-file mode must render one editor.');
check(str_contains($fileHtml, 'SHA-256:'), 'Existing-file mode must show its loaded hash.');
check(
    str_contains($fileHtml, 'Load replaces unsaved text in the editor.')
        && str_contains($fileHtml, 'Every save validates the full configuration tree.')
        && str_contains($fileHtml, 'A provider is a topology source.')
        && str_contains($fileHtml, 'existing LibreNMS database credentials')
        && str_contains($fileHtml, 'file and regex providers remain advanced alternatives'),
    'Files must explain providers, destructive load, and full-tree validation behavior.'
);

$newRequest = Request::create('/plugin/settings/libremapper?tab=files&file=__new__');
$newData = $controller->viewData($newRequest);
check($newData['newFile'] && $newData['selectedFile'] === null, 'An empty file choice must select new-file mode.');
$newHtml = view('libremapper::settings', $newData)->render();
check(substr_count($newHtml, 'name="content"') === 1, 'New-file mode must render one editor.');
check(str_contains($newHtml, 'name="path"'), 'New-file mode must render the path control.');

$mapPath = $configRoot.'/maps/php-freshness.yaml';
$backupName = 'libremapper.yaml.pre-map-deploy-0bd3cce.bak';
$backupPath = $configRoot.'/'.$backupName;
$backupContents = "operator backup: \xff\n";
file_put_contents($backupPath, $backupContents);
$engine->saveFile('maps/php-freshness.yaml', (string) file_get_contents($mapPath), hash_file('sha256', $mapPath));
check(file_get_contents($backupPath) === $backupContents, 'Saving must leave regular operator backups untouched.');
check(! in_array($backupName, array_column($engine->files(), 'path'), true), 'Backups must stay hidden from Files.');
foreach (['.hidden.bak', 'unsafe.txt', 'executable.bak', 'executable.yaml', 'symlink.bak'] as $unsafeName) {
    $unsafePath = $configRoot.'/'.$unsafeName;
    if ($unsafeName === 'symlink.bak') {
        symlink($backupPath, $unsafePath);
    } else {
        file_put_contents($unsafePath, 'unsafe');
        if (str_starts_with($unsafeName, 'executable')) {
            chmod($unsafePath, 0770);
        }
    }
    fails(
        fn () => $engine->saveFile('maps/php-freshness.yaml', $map, hash_file('sha256', $mapPath)),
        in_array($unsafeName, ['.hidden.bak', 'symlink.bak'], true) ? 'Unsafe configuration path' : 'Unmanaged configuration file'
    );
    unlink($unsafePath);
}

fails(fn () => $engine->createFile('../escape.yaml', $map), 'Unsafe configuration file path');
fails(fn () => $engine->createFile('.hidden.yaml', $map), 'Unsafe configuration file path');
fails(fn () => $engine->createFile('maps/tool.yaml', str_repeat('x', 5 * 1024 * 1024 + 1)), '5 MiB');
$newGuidedInput = [
    'key' => 'guided-created',
    'name' => 'Guided Created',
    'provider' => 'native',
    'inventory_choices' => ['group:Reference Core'],
    'inventory_layers' => ['0'],
];
fails(
    fn () => $engine->guidedFile('__new__', array_replace($newGuidedInput, ['key' => 'Unsafe Key'])),
    'safe lowercase name'
);
fails(
    fn () => $engine->guidedFile('__new__', array_replace($newGuidedInput, ['key' => 'php-freshness'])),
    'already exists'
);
fails(
    fn () => $engine->guidedFile('__new__', array_replace($newGuidedInput, [
        'inventory_choices' => [], 'inventory_layers' => [],
    ])),
    'Select at least one'
);
fails(
    fn () => $engine->guidedFile('__new__', array_replace($newGuidedInput, ['provider' => 'external'])),
    'configured LibreNMS provider'
);
$inventoryEngine = new class(null, '/data/libremapper', $configRoot) extends Engine
{
    public function inventory(): array
    {
        return [
            'groups' => [['name' => 'Reference Core', 'device_count' => 2]],
            'locations' => [['location' => 'HQ (West) [1]', 'device_count' => 3]],
            'devices' => [
                ['device_id' => 1001, 'hostname' => 'core-one.example.net', 'sys_name' => 'Core One'],
                ['device_id' => 1002, 'hostname' => 'core-two.example.net', 'sys_name' => 'Core Two'],
            ],
        ];
    }
};
$operatorInput = [
    'name' => 'PHP Freshness',
    'rules' => [['field' => 'group', 'value' => 'Reference Core']],
];
$generatedDraft = $inventoryEngine->guidedFile('__new__', $operatorInput);
$generatedMap = Yaml::parse($generatedDraft['content']);
check($generatedDraft['key'] === 'php-freshness-2' && $generatedMap['sources'][0]['provider'] === 'native', 'Creation must generate a collision-safe key and infer the sole renamed LibreNMS provider.');
check($generatedMap['layout'] === ['type' => 'auto', 'infer_layers' => true, 'auto_grow' => true, 'separation' => 'constrained']
    && empty($generatedMap['sources'][0]['protocols']), 'New maps must use automatic hierarchy, canvas growth, constrained placement, and all protocols.');
$engine->createFile($generatedDraft['path'], $generatedDraft['content']);
check($inventoryEngine->guidedFile('__new__', $operatorInput)['key'] === 'php-freshness-3', 'Generated collision suffixes must advance deterministically.');
$engine->deleteFile($generatedDraft['path'], hash_file('sha256', $configRoot.'/'.$generatedDraft['path']));
$ambiguousMain = Yaml::parse($main);
$ambiguousMain['providers']['second-native'] = ['type' => 'librenms', 'settings' => []];
file_put_contents($configRoot.'/libremapper.yaml', dumpTestConfig($ambiguousMain, 12, 2, Yaml::DUMP_EMPTY_ARRAY_AS_SEQUENCE));
fails(fn () => $inventoryEngine->guidedFile('__new__', $operatorInput), 'configured LibreNMS provider');
$ambiguousMain['providers']['librenms'] = ['type' => 'librenms', 'settings' => []];
file_put_contents($configRoot.'/libremapper.yaml', dumpTestConfig($ambiguousMain, 12, 2, Yaml::DUMP_EMPTY_ARRAY_AS_SEQUENCE));
check(Yaml::parse($inventoryEngine->guidedFile('__new__', $operatorInput)['content'])['sources'][0]['provider'] === 'librenms', 'The conventional librenms provider must win when multiple sources exist.');
file_put_contents($configRoot.'/libremapper.yaml', $main);
$ruleDraft = $inventoryEngine->guidedFile('__new__', [
    'name' => 'Plain Rules',
    'rules' => [
        ['field' => 'group', 'value' => 'Reference Core', 'layer' => '-2'],
        ['field' => 'exact_location', 'value' => 'HQ (West) [1]'],
        ['field' => 'hostname', 'value' => '^core-'],
        ['field' => 'sys_name', 'value' => '^Core'],
        ['field' => 'os', 'value' => 'ios|saos'],
        ['field' => 'hardware', 'value' => 'ASR'],
        ['field' => 'type', 'value' => '^network$'],
    ],
]);
$plainSelectors = Yaml::parse($ruleDraft['content'])['sources'][0]['selectors'];
check($plainSelectors[1] === ['type' => 'location', 'value' => '^HQ \\(West\\) \\[1\\]$']
    && $plainSelectors[0]['layer'] === -2
    && $plainSelectors[3] === ['type' => 'attribute', 'value' => '^Core', 'attribute' => 'sys_name']
    && count($plainSelectors) === 7, 'Plain rules must encode exact locations and supported regex attributes.');
fails(fn () => $inventoryEngine->guidedFile('__new__', ['name' => 'Invalid', 'rules' => [['field' => 'group', 'value' => 'Gone']]]), 'missing, empty, or stale');
fails(fn () => $inventoryEngine->guidedFile('__new__', ['name' => 'Invalid', 'rules' => [['field' => 'exact_location', 'value' => 'Gone']]]), 'missing, empty, or stale');
foreach (['1.5', 'two', null, []] as $invalidLayer) {
    fails(fn () => $inventoryEngine->guidedFile('__new__', ['name' => 'Invalid layer', 'rules' => [['field' => 'group', 'value' => 'Reference Core', 'layer' => $invalidLayer]]]), 'Scope layers must be whole numbers');
}
$invalidRegex = $inventoryEngine->guidedFile('__new__', ['name' => 'Invalid Regex', 'rules' => [['field' => 'hostname', 'value' => '[']]]);
fails(fn () => $engine->createFile($invalidRegex['path'], $invalidRegex['content']), 'invalid selector regex');
check(! is_file($configRoot.'/'.$invalidRegex['path']), 'Invalid regex validation must not leave a draft file.');

$customizedMap = Yaml::parseFile($configRoot.'/maps/php-freshness.yaml');
$customizedMap['sources'][0]['selectors'] = [
    ['type' => 'group', 'value' => 'Reference Core', 'layer' => 42],
    ['type' => 'canonical', 'value' => 'custom-device', 'layer' => 7],
    ['type' => 'group', 'value' => '123', 'layer' => 6],
    ['type' => 'attribute', 'attribute' => 'vendor_tag', 'value' => '^private$', 'layer' => 4],
    ['type' => 'device_id', 'value' => 1001, 'layer' => 8],
];
$customizedMap['sources'][0]['protocols'] = ['lldp', 'site-private'];
$customizedMap['sources'][0]['interface_types'] = ['ethernetCsmacd'];
$customizedMap['sources'][] = ['provider' => 'external', 'selectors' => [['type' => 'canonical', 'value' => 'external-only']]];
$customizedMap['layout'] = ['type' => 'radial', 'radius' => 433, 'seed' => 12, 'auto_grow' => false];
$customizedMap['node_defaults'] = ['style' => 'circle', 'colour_bg' => '#123456'];
$customizedMap['edge_defaults'] = ['showpct' => true, 'text_colour' => '#654321'];
$customizedMap['device_exclusions'] = [1002];
$customizedMap['layer_overrides'] = [['type' => 'hostname', 'value' => '^custom', 'layer' => 99]];
$engine->saveFile('maps/php-freshness.yaml', dumpTestConfig($customizedMap, 12, 2), hash_file('sha256', $configRoot.'/maps/php-freshness.yaml'));
$contradictoryMap = $customizedMap;
$contradictoryMap['device_exclusions'] = [1001];
fails(fn () => $engine->saveFile('maps/php-freshness.yaml', dumpTestConfig($contradictoryMap, 12, 2), hash_file('sha256', $configRoot.'/maps/php-freshness.yaml')), 'both explicitly included and excluded');
fails(fn () => $inventoryEngine->guidedFile('php-freshness', ['map_hash' => str_repeat('0', 64), 'name' => 'Conflicting edit']), 'Draft changed since this page loaded');
$targetedDraft = $inventoryEngine->guidedFile('php-freshness', ['map_hash' => hash('sha256', json_encode($customizedMap, JSON_THROW_ON_ERROR)), 'rules' => [['index' => 0, 'field' => 'group', 'value' => 'Reference Core']]]);
check(Yaml::parse($targetedDraft['content']) === $customizedMap, 'Saving unchanged plain rules must preserve Advanced selectors, original layers, providers, protocols, appearance, exclusions, and custom layout.');
$scopeLayerDraft = $inventoryEngine->guidedFile('php-freshness', ['rules' => [['index' => 0, 'field' => 'group', 'value' => 'Reference Core', 'layer' => '-10']]]);
$scopeLayerExpected = $customizedMap;
$scopeLayerExpected['sources'][0]['selectors'][0]['layer'] = -10;
check(Yaml::parse($scopeLayerDraft['content']) === $scopeLayerExpected, 'Editing a scope layer must preserve the rule and all unrelated map settings.');
$includedDraft = $inventoryEngine->guidedFile('php-freshness', ['device_action' => 'include', 'device_id' => '1002']);
$includedMap = Yaml::parse($includedDraft['content']);
check($includedMap['device_exclusions'] === [] && end($includedMap['sources'][0]['selectors']) === ['type' => 'device_id', 'value' => 1002], 'Individual inclusion must remove an exclusion and append an exact device selector.');
$engine->saveFile($includedDraft['path'], $includedDraft['content'], $includedDraft['sha256']);
check(preg_match('/^device_exclusions:\s*\[\s*\]\s*$/m', $includedDraft['content']) === 1, 'An empty inclusion exception list must serialize as a YAML list accepted by the sidecar.');
$excludedDraft = $inventoryEngine->guidedFile('php-freshness', ['device_action' => 'exclude', 'device_id' => '1001']);
$excludedMap = Yaml::parse($excludedDraft['content']);
check($excludedMap['device_exclusions'] === [1001]
    && ! collect($excludedMap['sources'][0]['selectors'])->contains(fn ($rule) => ($rule['type'] ?? '') === 'device_id' && (int) $rule['value'] === 1001), 'Individual exclusion must remove contradictory exact inclusions.');
$engine->saveFile($excludedDraft['path'], $excludedDraft['content'], $excludedDraft['sha256']);
$automaticDraft = $inventoryEngine->guidedFile('php-freshness', ['device_action' => 'automatic', 'device_id' => '1001']);
check(Yaml::parse($automaticDraft['content'])['device_exclusions'] === [], 'Automatic membership must remove an individual exclusion.');
$engine->saveFile($automaticDraft['path'], $automaticDraft['content'], $automaticDraft['sha256']);
check(Yaml::parseFile($configRoot.'/'.$automaticDraft['path'])['device_exclusions'] === []
    && preg_match('/^device_exclusions:\s*\[\s*\]\s*$/m', $automaticDraft['content']) === 1, 'Exclude then Automatic must persist a sidecar-validated empty exclusion list.');
$placementDraft = $inventoryEngine->guidedFile('php-freshness', ['placement_device_id' => '1001', 'placement' => '3']);
$placementMap = Yaml::parse($placementDraft['content']);
check($placementMap['layer_overrides'] === [['type' => 'device_id', 'value' => 1001, 'layer' => 3], ...$customizedMap['layer_overrides']], 'Placement must prepend an exact override while retaining Advanced rules.');
$engine->saveFile($placementDraft['path'], $placementDraft['content'], $placementDraft['sha256']);
$automaticPlacement = $inventoryEngine->guidedFile('php-freshness', ['placement_device_id' => '1001', 'placement' => 'auto']);
check(Yaml::parse($automaticPlacement['content'])['layer_overrides'] === $customizedMap['layer_overrides'], 'Automatic placement must remove only the selected device override.');
$bulkPlacementInput = ['placement_action' => 'apply', 'placement_device_ids' => ['1001', '1002', '1001'], 'placement' => '-5'];
$bulkPlacementDraft = $inventoryEngine->guidedFile('php-freshness', $bulkPlacementInput);
$bulkPlacementMap = Yaml::parse($bulkPlacementDraft['content']);
check($bulkPlacementMap['layer_overrides'] === [
    ['type' => 'device_id', 'value' => 1001, 'layer' => -5], ['type' => 'device_id', 'value' => 1002, 'layer' => -5], ...$customizedMap['layer_overrides'],
] && $bulkPlacementMap['layout'] === $placementMap['layout'] && $bulkPlacementMap['sources'] === $placementMap['sources'], 'A shared layer must update selected devices once, preserve Advanced rules, and leave layout options and scope unchanged.');
$engine->saveFile($bulkPlacementDraft['path'], $bulkPlacementDraft['content'], $bulkPlacementDraft['sha256']);
$partialClear = $inventoryEngine->guidedFile('php-freshness', ['placement_device_ids' => [1001], 'placement' => '']);
check(Yaml::parse($partialClear['content'])['layer_overrides'] === [
    ['type' => 'device_id', 'value' => 1002, 'layer' => -5], ...$customizedMap['layer_overrides'],
], 'Clearing a shared layer must preserve unselected device and scope overrides.');
foreach ([[], '1001', null, [1001, 99999999]] as $invalidSelection) {
    fails(fn () => $inventoryEngine->guidedFile('php-freshness', array_replace($bulkPlacementInput, ['placement_device_ids' => $invalidSelection])), 'device');
}
foreach (['1.5', 'two', []] as $invalidLayer) {
    fails(fn () => $inventoryEngine->guidedFile('php-freshness', array_replace($bulkPlacementInput, ['placement' => $invalidLayer])), 'blank or a whole number');
}
fails(fn () => $inventoryEngine->guidedFile('php-freshness', ['placement_action' => 'apply', 'placement' => '0']), 'at least one current LibreNMS device');
fails(fn () => $inventoryEngine->guidedFile('php-freshness', $bulkPlacementInput + ['map_hash' => str_repeat('0', 64)]), 'Draft changed');
check(Yaml::parseFile($configRoot.'/'.$bulkPlacementDraft['path']) === $bulkPlacementMap, 'Invalid layer requests must leave saved map settings unchanged.');
$engine->saveFile($placementDraft['path'], $placementDraft['content'], hash_file('sha256', $configRoot.'/'.$placementDraft['path']));
$labelDraft = $inventoryEngine->guidedFile('php-freshness', ['node_label' => 'short_hostname', 'short_hostname_parts' => '3']);
$labelMap = Yaml::parse($labelDraft['content']);
check($labelMap['node_label'] === 'short_hostname' && $labelMap['short_hostname_parts'] === 3
    && $labelMap['layout'] === $placementMap['layout'] && $labelMap['sources'] === $placementMap['sources'],
    'Saving label controls must preserve existing topology and layout settings.');
foreach (['0', '128', '1.5', 'two'] as $invalidParts) {
    fails(fn () => $inventoryEngine->guidedFile('php-freshness', ['short_hostname_parts' => $invalidParts]), 'whole number from 1 to 127');
    fails(fn () => $inventoryEngine->guidedFile('php-freshness', ['hostname_device_id' => 1001, 'hostname_parts' => $invalidParts]), 'blank or a whole number from 1 to 127');
}
$deviceLabelDraft = $inventoryEngine->guidedFile('php-freshness', ['hostname_device_id' => 1001, 'hostname_parts' => '3']);
check(Yaml::parse($deviceLabelDraft['content'])['short_hostname_overrides'] === [['device_id' => 1001, 'parts' => 3]],
    'A device label exception must store an exact device ID and component count.');
$engine->saveFile($deviceLabelDraft['path'], $deviceLabelDraft['content'], $deviceLabelDraft['sha256']);
$secondLabelDraft = $inventoryEngine->guidedFile('php-freshness', ['hostname_device_id' => 1002, 'hostname_parts' => '4']);
$engine->saveFile($secondLabelDraft['path'], $secondLabelDraft['content'], $secondLabelDraft['sha256']);
$bulkInput = ['hostname_action' => 'apply', 'hostname_device_ids' => ['1001', '1002', '1001'], 'hostname_parts' => '5'];
$bulkLabelDraft = $inventoryEngine->guidedFile('php-freshness', $bulkInput);
check(Yaml::parse($bulkLabelDraft['content'])['short_hostname_overrides'] === [
    ['device_id' => 1001, 'parts' => 5], ['device_id' => 1002, 'parts' => 5],
], 'A shared component count must replace selected overrides once per device.');
$engine->saveFile($bulkLabelDraft['path'], $bulkLabelDraft['content'], $bulkLabelDraft['sha256']);
$bulkClear = $inventoryEngine->guidedFile('php-freshness', array_replace($bulkInput, ['hostname_parts' => '']));
check(Yaml::parse($bulkClear['content'])['short_hostname_overrides'] === [], 'A blank shared count must clear all selected overrides.');
foreach ([[], '1001', null] as $invalidSelection) {
    fails(fn () => $inventoryEngine->guidedFile('php-freshness', array_replace($bulkInput, ['hostname_device_ids' => $invalidSelection])), 'at least one current LibreNMS device');
}
fails(fn () => $inventoryEngine->guidedFile('php-freshness', ['hostname_action' => 'apply', 'hostname_parts' => '3']), 'at least one current LibreNMS device');
fails(fn () => $inventoryEngine->guidedFile('php-freshness', array_replace($bulkInput, ['hostname_device_ids' => [1001, 99999999]])), 'current LibreNMS device');
fails(fn () => $inventoryEngine->guidedFile('php-freshness', array_replace($bulkInput, ['hostname_parts' => '128'])), 'blank or a whole number from 1 to 127');
fails(fn () => $inventoryEngine->guidedFile('php-freshness', $bulkInput + ['map_hash' => str_repeat('0', 64)]), 'Draft changed');
check(Yaml::parseFile($configRoot.'/'.$bulkLabelDraft['path'])['short_hostname_overrides'] === [
    ['device_id' => 1001, 'parts' => 5], ['device_id' => 1002, 'parts' => 5],
], 'Invalid bulk requests must leave the saved overrides unchanged.');
$engine->saveFile($secondLabelDraft['path'], $secondLabelDraft['content'], hash_file('sha256', $configRoot.'/'.$secondLabelDraft['path']));
$clearedLabelDraft = $inventoryEngine->guidedFile('php-freshness', ['hostname_device_id' => 1001, 'hostname_parts' => '']);
check(Yaml::parse($clearedLabelDraft['content'])['short_hostname_overrides'] === [['device_id' => 1002, 'parts' => 4]],
    'Clearing a device label exception must preserve exceptions for other devices.');
$engine->saveFile($clearedLabelDraft['path'], $clearedLabelDraft['content'], $clearedLabelDraft['sha256']);
$lastLabelDraft = $inventoryEngine->guidedFile('php-freshness', ['hostname_device_id' => 1002, 'hostname_parts' => '']);
$engine->saveFile($lastLabelDraft['path'], $lastLabelDraft['content'], $lastLabelDraft['sha256']);
check(Yaml::parseFile($configRoot.'/'.$lastLabelDraft['path'])['short_hostname_overrides'] === [],
    'Clearing the final label override must persist a valid empty YAML list.');
fails(fn () => $inventoryEngine->guidedFile('php-freshness', ['hostname_device_id' => 99999999, 'hostname_parts' => 3]), 'current LibreNMS device');
fails(fn () => $inventoryEngine->guidedFile('php-freshness', ['device_action' => 'exclude', 'device_id' => 99999999]), 'current LibreNMS device');
fails(fn () => $inventoryEngine->guidedFile('php-freshness', ['placement_device_id' => 1001, 'placement' => '1.5']), 'blank or a whole number');
$engine->saveFile('maps/php-freshness.yaml', $map, hash_file('sha256', $configRoot.'/maps/php-freshness.yaml'));
$selectedFile = $inventoryEngine->guidedFile('__new__', array_replace($newGuidedInput, [
    'inventory_choices' => ['group:Reference Core', 'location:HQ (West) [1]'],
    'inventory_layers' => ['-2', '10'],
    'layout_type' => 'top',
]));
$selectedMap = Yaml::parse($selectedFile['content']);
check(
    $selectedMap['sources'][0]['selectors'] === [
        ['type' => 'group', 'value' => 'Reference Core', 'layer' => -2],
        ['type' => 'location', 'value' => '^HQ \\(West\\) \\[1\\]$', 'layer' => 10],
    ] && $selectedMap['layout']['type'] === 'top'
        && $selectedMap['layout']['auto_grow'] === true
        && $selectedMap['node_label'] === 'sys_name',
    'Guided selections must preserve layers, exact selectors, layout, and the readable label default.'
);
$mainWithOxidized = str_replace(
    '  external: {type: file, settings: {path: manual-topology.yaml}}',
    "  external: {type: file, settings: {path: manual-topology.yaml}}\n"
        .'  oxidized: {type: oxidized, settings: {repository: /oxidized.git, ref: HEAD}}',
    $main
);
file_put_contents($configRoot.'/libremapper.yaml', $mainWithOxidized);
$inspectionEngine = new class(null, '/data/libremapper', $configRoot) extends Engine
{
    public int $inspectionCalls = 0;
    public ?string $inspectionMap = null;

    public function validateCurrent(): array
    {
        return [];
    }

    public function inventory(): array
    {
        return [
            'groups' => [['name' => 'Reference Core', 'device_count' => 2]],
            'locations' => [],
        ];
    }

    public function parserCatalog(?string $path = null): array
    {
        return ['items' => [
            ['id' => 'saos10-lldp-neighbors', 'definition' => ['label' => 'Ciena SAOS 10 LLDP neighbors']],
            ['id' => 'saos10-nni-config', 'definition' => ['label' => 'Ciena SAOS 10 NNI configuration']],
        ], 'selections' => ['php-freshness' => ['oxidized' => ['saos10-lldp-neighbors']]]];
    }

    public function providerInspection(string $provider, ?string $map = null): array
    {
        $this->inspectionCalls++;
        $this->inspectionMap = $map;

        return [
            'provider' => $provider,
            'provider_type' => 'oxidized',
            'revision' => str_repeat('a', 40),
            'matched_files' => ['topology/r1--lldp-neighbors'],
            'matched_rules' => ['topology/r1--lldp-neighbors' => 'saos10-lldp-neighbors'],
            'skipped_files' => [],
            'resolution_failures' => [[
                'path' => 'topology/r1--lldp-neighbors',
                'rule' => 'saos10-lldp-neighbors',
                'block' => '2',
                'reason' => 'remote interface did not resolve uniquely',
            ]],
            'warnings' => ['one unresolved interface'],
            'blocked' => false,
            'parser_reports' => [
                ['name' => 'saos10-lldp-neighbors', 'label' => 'Ciena SAOS 10 LLDP neighbors', 'matched_files' => 1, 'links' => 1, 'unbound_interfaces' => 0, 'blocked' => false],
            ],
            'topology' => [
                'provider' => $provider,
                'devices' => [['source_id' => 'device:1'], ['source_id' => 'device:2']],
                'links' => [[
                    'a' => ['device' => 'device:1'],
                    'b' => ['device' => 'device:2'],
                    'protocol' => 'lldp',
                    'attributes' => [
                        'local_hostname' => 'r1.example.net',
                        'local_interface' => '1/1',
                        'remote_hostname' => 'bcr2.example.net',
                        'remote_interface' => '1/2',
                        'artifact_path' => 'topology/r1--lldp-neighbors',
                    ],
                ]],
            ],
        ];
    }
};
$configuredNewData = (new LibremapperController($inspectionEngine))->viewData(Request::create(
    '/plugin/settings/libremapper?map=__new__'
));
$configuredNewHtml = view('libremapper::settings', $configuredNewData)->render();
check(
    ! str_contains($configuredNewHtml, 'name="include_oxidized"')
        && ! str_contains($configuredNewHtml, 'name="layout_auto_grow"')
        && $inspectionEngine->inspectionCalls === 0,
    'New maps must default to Oxidized and auto-grow without inspecting until its tab is opened.'
);
$oxidizedTabData = (new LibremapperController($inspectionEngine))->viewData(Request::create(
    '/plugin/settings/libremapper?tab=oxidized&map=php-freshness'
));
$oxidizedTabHtml = view('libremapper::settings', $oxidizedTabData)->render();
check(str_contains($oxidizedTabHtml, 'id="oxidized-map-source"')
    && str_contains($oxidizedTabHtml, route('libremapper.parsers.selection', ['map' => 'php-freshness']))
    && $inspectionEngine->inspectionCalls === 0,
    'Oxidized Source must edit map parser selection without inspecting repository contents.');
foreach ([
    'name="parsers[]" value="saos10-nni-config"',
    'Save parser selection',
    'Edit global definition',
    'Manage global parser catalog',
    'section=report',
] as $copy) {
    check(str_contains($oxidizedTabHtml, $copy), "Oxidized Source must render: $copy");
}
$connectionData = (new LibremapperController($inspectionEngine))->viewData(Request::create(
    '/plugin/settings/libremapper?tab=advanced&subtab=oxidized&map=php-freshness'
));
$connectionHtml = view('libremapper::settings', $connectionData)->render();
check(str_contains($connectionHtml, 'Read-only fetch key') && str_contains($connectionHtml, 'Save and test source')
    && ! str_contains($connectionHtml, 'id="oxidized-map-source"') && ! str_contains($oxidizedTabHtml, 'Read-only fetch key')
    && $inspectionEngine->inspectionCalls === 0, 'The shared Oxidized connection belongs only in Advanced.');
$oxidizedReportData = (new LibremapperController($inspectionEngine))->viewData(Request::create(
    '/plugin/settings/libremapper?tab=links&subtab=oxidized&section=report&map=php-freshness'
));
$oxidizedReportHtml = view('libremapper::settings', $oxidizedReportData)->render();
foreach ([
    'Parser report',
    'Parsing succeeded.',
    '1 artifact matched,',
    '2 devices and',
    '1 connection produced.',
    str_repeat('a', 40),
    'saos10-lldp-neighbors',
    'r1.example.net',
    'bcr2.example.net',
    'remote interface did not resolve uniquely',
    'id="oxidized-parser-reports"',
] as $copy) {
    check(str_contains($oxidizedReportHtml, $copy), "Oxidized diagnostics must render: $copy");
}
check(
    $oxidizedReportData['activeTab'] === 'links' && $oxidizedReportData['activeSubtab'] === 'parsers'
        && $inspectionEngine->inspectionCalls === 1
        && $inspectionEngine->inspectionMap === 'php-freshness'
        && ! str_contains($oxidizedReportHtml, 'Read-only fetch key'),
    'Opening Report must inspect only the selected map and render diagnostics without Source controls.'
);
$oxidizedMapFile = $inventoryEngine->guidedFile('__new__', array_replace($newGuidedInput, [
    'include_oxidized' => true,
]));
$oxidizedMap = Yaml::parse($oxidizedMapFile['content']);
check(
    $oxidizedMap['sources'][1]['provider'] === 'oxidized'
        && $oxidizedMap['sources'][1]['selectors'] === $oxidizedMap['sources'][0]['selectors'],
    'Guided maps must reuse LibreNMS selectors for the built-in Oxidized source.'
);
file_put_contents($configRoot.'/libremapper.yaml', $main);
fails(
    fn () => $inventoryEngine->guidedFile('__new__', array_replace($newGuidedInput, [
        'inventory_choices' => ['group:Reference Core', 'group:Reference Core'],
        'inventory_layers' => ['0', '1'],
    ])),
    'selected only once'
);
fails(
    fn () => $inventoryEngine->guidedFile('__new__', array_replace($newGuidedInput, [
        'inventory_choices' => ['group:Renamed'],
    ])),
    'missing, empty, or stale'
);
fails(
    fn () => $inventoryEngine->guidedFile('__new__', array_replace($newGuidedInput, [
        'inventory_layers' => ['1.5'],
    ])),
    'integer layer'
);

$mapPath = $configRoot.'/maps/php-freshness.yaml';
$mapSha = hash_file('sha256', $mapPath);
$before = file_get_contents($mapPath);
fails(
    fn () => $engine->saveFile('maps/php-freshness.yaml', "key: wrong\n", $mapSha),
    'filename must match'
);
check(file_get_contents($mapPath) === $before, 'Failed validation must leave disk unchanged.');

$manualPath = $configRoot.'/manual-topology.yaml';
$manualSha = hash_file('sha256', $manualPath);
file_put_contents($manualPath, $manual."# external atomic writer won\n");
fails(
    fn () => $engine->saveFile('manual-topology.yaml', $manual, $manualSha),
    'explicitly confirm overwrite'
);
$engine->saveFile('manual-topology.yaml', $manual."# forced\n", $manualSha, true);
check(str_contains((string) file_get_contents($manualPath), '# forced'), 'Explicit force must resolve stale saves.');

file_put_contents($mapPath, "not: [valid\n");
fails(fn () => $engine->validateCurrent(), 'Validation failed');
check(count($engine->files()) === 3, 'Files must remain available while configuration is invalid.');
$invalidData = $controller->viewData(Request::create(
    '/plugin/settings/libremapper?tab=files&file=maps/php-freshness.yaml'
));
check($invalidData['configError'] !== null, 'Invalid trees must report their validation error.');
check(
    $invalidData['selectedFile']['path'] === 'maps/php-freshness.yaml',
    'File selection must remain usable for invalid-tree recovery.'
);
check(
    substr_count(view('libremapper::settings', $invalidData)->render(), 'name="content"') === 1,
    'Invalid-tree recovery must still render one editor.'
);
$engine->saveFile('maps/php-freshness.yaml', $map, hash_file('sha256', $mapPath));
$engine->validateCurrent();

$session = app('session')->driver();
$guidedCreateRequest = Request::create('/plugin/libremapper/config', 'PUT', $newGuidedInput + [
    'original_key' => '__new__',
    'scheduled' => '1',
]);
$guidedCreateRequest->setLaravelSession($session);
$guidedCreateResponse = $controller->update($guidedCreateRequest);
check(
    str_contains($guidedCreateResponse->getTargetUrl(), 'map=guided-created'),
    'Guided creation must redirect to and select the new key: '.json_encode($session->get('errors')?->all())
);
$createdMapPath = $configRoot.'/maps/guided-created.yaml';
$createdMap = Yaml::parseFile($createdMapPath);
check(
    $createdMap['key'] === 'guided-created'
        && $createdMap['name'] === 'Guided Created'
        && $createdMap['scheduled'] === false
        && $createdMap['manual_link_exclusions'] === []
        && $createdMap['sources'][0]['provider'] === 'native'
        && count($createdMap['sources'][0]['selectors']) === 1,
    'Guided creation must write one unscheduled LibreNMS source through the fragment workflow.'
);
$createdData = $controller->viewData(Request::create(
    '/plugin/settings/libremapper?map=guided-created'
));
check(
    ! $createdData['newMap'] && $createdData['selectedKey'] === 'guided-created',
    'A newly created map must render in normal editable Guided mode.'
);
$guidedEditRequest = Request::create('/plugin/libremapper/config', 'PUT', [
    'original_key' => 'guided-created',
    'key' => 'ignored-renamed-key',
    'name' => 'Guided Edited',
    'selectors_yaml' => "- {type: device_id, value: 1001}\n",
]);
$guidedEditRequest->setLaravelSession($session);
$guidedEditResponse = $controller->update($guidedEditRequest);
$editedMap = Yaml::parseFile($createdMapPath);
check(
    str_contains($guidedEditResponse->getTargetUrl(), 'map=guided-created')
        && $editedMap['key'] === 'guided-created'
        && $editedMap['name'] === 'Guided Edited',
    'Subsequent Guided edits must preserve the immutable key.'
);
$engine->deleteFile('maps/guided-created.yaml', hash_file('sha256', $createdMapPath));

$scratch = "note: scratch\n";
$createRequest = Request::create('/plugin/libremapper/files', 'POST', [
    'path' => 'scratch.yaml',
    'content' => $scratch,
]);
$createRequest->setLaravelSession($session);
$createResponse = $controller->createFile($createRequest);
check(str_contains($createResponse->getTargetUrl(), 'file=scratch.yaml'), 'Create must select the new file.');

$scratchPath = $configRoot.'/scratch.yaml';
$scratchSha = hash_file('sha256', $scratchPath);
$saveRequest = Request::create('/plugin/libremapper/files', 'PUT', [
    'path' => 'scratch.yaml',
    'content' => "note: saved\n",
    'sha256' => $scratchSha,
]);
$saveRequest->setLaravelSession($session);
$saveResponse = $controller->saveFile($saveRequest);
check(str_contains($saveResponse->getTargetUrl(), 'file=scratch.yaml'), 'Save must preserve the selected file.');

$staleSha = hash_file('sha256', $scratchPath);
file_put_contents($scratchPath, "note: external\n");
$staleContent = "note: entered text\n";
$staleRequest = Request::create('/plugin/libremapper/files', 'PUT', [
    'path' => 'scratch.yaml',
    'content' => $staleContent,
    'sha256' => $staleSha,
], [], [], [
    'HTTP_REFERER' => 'http://localhost/plugin/settings/libremapper?tab=files&file=scratch.yaml',
]);
$staleRequest->setLaravelSession($session);
$app->instance('request', $staleRequest);
$controller->saveFile($staleRequest);
check($session->getOldInput('content') === $staleContent, 'Stale saves must preserve entered content.');

$forceSaveRequest = Request::create('/plugin/libremapper/files', 'PUT', [
    'path' => 'scratch.yaml',
    'content' => $staleContent,
    'sha256' => $staleSha,
    'force' => '1',
]);
$forceSaveRequest->setLaravelSession($session);
$controller->saveFile($forceSaveRequest);
check(file_get_contents($scratchPath) === $staleContent, 'Explicit force must resolve controller saves.');

$deleteSha = hash_file('sha256', $scratchPath);
file_put_contents($scratchPath, "note: changed before delete\n");
$deleteRequest = Request::create('/plugin/libremapper/files', 'DELETE', [
    'path' => 'scratch.yaml',
    'sha256' => $deleteSha,
    'force' => '1',
]);
$deleteRequest->setLaravelSession($session);
$deleteResponse = $controller->deleteFile($deleteRequest);
check(! is_file($scratchPath), 'Explicit force must resolve controller deletes.');
check(
    str_contains($deleteResponse->getTargetUrl(), 'file=libremapper.yaml'),
    'Delete must select the first remaining file.'
);

$invalidContent = "key: mismatch\nname: Mismatch\nsources: []\n";
$invalidCreate = Request::create('/plugin/libremapper/files', 'POST', [
    'path' => 'maps/wrong.yaml',
    'content' => $invalidContent,
], [], [], [
    'HTTP_REFERER' => 'http://localhost/plugin/settings/libremapper?tab=files&file=__new__',
]);
$invalidCreate->setLaravelSession($session);
$app->instance('request', $invalidCreate);
$controller->createFile($invalidCreate);
check(
    $session->getOldInput('path') === 'maps/wrong.yaml'
        && $session->getOldInput('content') === $invalidContent,
    'Failed creates must preserve entered path and content.'
);

$guided = $engine->guidedFile('php-freshness', [
    'name' => $mapName,
    'scheduled' => true,
    'layout_type' => 'radial',
    'layout_radius' => 400,
    'selectors_yaml' => "- {type: device_id, value: 1001}\n",
    'protocols' => 'lldp, ospf',
    'node_style' => 'legacyNodeShape',
    'edge_style' => 'legacyEdgeCurve',
]);
$engine->saveFile($guided['path'], $guided['content'], $guided['sha256']);
check($engine->scheduledMapKeys() === ['php-freshness'], 'Map-local scheduled fields control eligibility.');
$nonDefaultHtml = view('libremapper::settings', $controller->viewData(Request::create(
    '/plugin/settings/libremapper?map=php-freshness&tab=advanced&subtab=appearance'
)))->render();
check(
    preg_match('/class="panel-collapse collapse in" id="advanced-layout-tuning"/', $nonDefaultHtml) === 0,
    'Advanced sections with non-default configured values must still start closed.'
);
check(
    preg_match('/<option value="legacyNodeShape" selected>legacyNodeShape \(current unsupported value\)<\/option>/', $nonDefaultHtml) === 1
        && preg_match('/<option value="legacyEdgeCurve" selected>legacyEdgeCurve \(current unsupported value\)<\/option>/', $nonDefaultHtml) === 1,
    'Unlisted configured styles must remain selected and submit unchanged.'
);

$friendlyPlan = $engine->plan('php-freshness');
check($friendlyPlan['device_count'] === 1 && $friendlyPlan['link_count'] === 0, 'Plan totals must describe resulting topology.');
check(
    $friendlyPlan['isolated_device_count'] === 1
        && collect($friendlyPlan['operations'])->firstWhere('kind', 'node')['discovered_link_count'] === 0,
    'Plan presentation must identify devices without discovered links.'
);
$planHtml = view('libremapper::settings', $controller->viewData(Request::create(
    '/plugin/settings/libremapper?map=php-freshness&tab=review'
)))->render();
$friendlyDevice = collect($friendlyPlan['operations'])->first(
    fn ($operation) => ($operation['kind'] ?? null) === 'node'
)['display_name'] ?? null;
check(is_string($friendlyDevice) && str_contains($planHtml, $friendlyDevice), 'Plan preview must show a recognizable device name.');
foreach (['1 device · 0 links', 'Show planned changes (', 'Review changes', 'Publish to LibreNMS', '<svg'] as $copy) {
    check(str_contains($planHtml, $copy), "Plan preview must render: $copy");
}
check(! str_contains($planHtml, 'Technical details'), 'Review must omit technical identifiers.');
check(strpos($planHtml, 'id="preview-options"') < strpos($planHtml, 'id="topology-preview"')
    && strpos($planHtml, 'id="topology-preview"') < strpos($planHtml, '1 device · 0 links')
    && strpos($planHtml, '1 device · 0 links') < strpos($planHtml, 'Show planned changes ('),
    'Review controls must precede topology, followed by statistics and publishing controls.');
check(str_contains($planHtml, 'Accessible topology table'), 'Review preview must retain an accessible table fallback.');
$scheduledOnly = str_replace('scheduled: true', 'scheduled: false', (string) file_get_contents($mapPath));
$engine->saveFile('maps/php-freshness.yaml', $scheduledOnly, hash_file('sha256', $mapPath));
check(! is_file($engine->planPath('php-freshness')), 'Saving a configuration draft must clear its previous review.');
check($engine->hasDraft('php-freshness'), 'Saving a map draft must pause its scheduled publication.');
$engine->plan('php-freshness');
$staleReview = file_get_contents($engine->planPath('php-freshness'));
check($engine->hasDraft('php-freshness'), 'Reviewing changes must leave the draft private.');
$engine->apply('php-freshness');
check(! $engine->hasDraft('php-freshness'), 'Successful publication must clear the unpublished draft marker.');
$engine->plan('php-freshness');
$fresh = file_get_contents($mapPath);
$changed = str_replace($mapName, $mapName.' Changed After Plan', (string) $fresh);
$engine->saveFile('maps/php-freshness.yaml', $changed, hash_file('sha256', $mapPath));
fails(fn () => $engine->apply('php-freshness'), 'No reviewed plan exists');
file_put_contents($engine->planPath('php-freshness'), $staleReview);
fails(fn () => $engine->apply('php-freshness'), 'configuration changed after this plan was generated');
$engine->saveFile('maps/php-freshness.yaml', $fresh, hash_file('sha256', $mapPath));
$engine->plan('php-freshness');
$conflict = $engine->reviewedPlan('php-freshness');
$conflict['conflicts'] = ['synthetic conflict'];
$conflict['warnings'] = [
    'a rule parser block 1: local interface a/1 did not resolve uniquely; retaining it as an unbound interface',
    'b rule parser block 2: remote interface b/2 did not resolve uniquely; retaining it as an unbound interface',
    'one other warning',
];
file_put_contents($engine->planPath('php-freshness'), json_encode($conflict, JSON_THROW_ON_ERROR));
$conflictHtml = view('libremapper::settings', $controller->viewData(Request::create(
    '/plugin/settings/libremapper?map=php-freshness&tab=review'
)))->render();
check(
    str_contains($conflictHtml, '1 conflicts block publishing.')
        && str_contains($conflictHtml, 'id="plan-conflicts"'),
    'Conflict details must start behind one compact blocking alert.'
);
check(
    str_contains($conflictHtml, '2 × Interface names did not match a unique LibreNMS port')
        && str_contains($conflictHtml, '1 × one other warning')
        && str_contains($conflictHtml, 'id="plan-warnings"'),
    'Repeated parser warnings must be summarized while retaining expandable details.'
);
fails(fn () => $engine->apply('php-freshness'), 'reviewed plan has conflicts');
$engine->deleteFile('maps/php-freshness.yaml', hash_file('sha256', $mapPath));
check(! is_file($engine->planPath('php-freshness')), 'Deleting a map draft must invalidate its stored review.');
$engine->createFile('maps/php-freshness.yaml', $fresh);

$manualMap = Yaml::parseFile($mapPath);
$manualMap['sources'][0]['selectors'] = [
    ['type' => 'device_id', 'value' => 1001],
    ['type' => 'device_id', 'value' => 1002],
];
$engine->saveFile(
    'maps/php-freshness.yaml',
    dumpTestConfig($manualMap, 12, 2),
    hash_file('sha256', $mapPath)
);
$manualPlan = $engine->plan('php-freshness');
$manualNodes = array_values(array_filter(
    $manualPlan['operations'],
    fn ($operation): bool => ($operation['kind'] ?? null) === 'node'
        && ($operation['action'] ?? null) !== 'delete'
));
check(count($manualNodes) === 2, 'Manual-link test requires two reviewed LibreNMS devices.');

$otherMap = $manualMap;
$otherMap['key'] = 'php-other';
$otherMap['name'] = $mapName.' Other';
$engine->createFile('maps/php-other.yaml', dumpTestConfig($otherMap, 12, 2));
$engine->plan('php-freshness');
$engine->plan('php-other');
$engine->addManualLink('php-freshness', $manualNodes[0]['canonical_id'], $manualNodes[1]['canonical_id'], manualInput($engine, 'php-freshness'));
check(! is_file($engine->planPath('php-freshness')) && ! is_file($engine->planPath('php-other')), 'Adding a global link must invalidate every reviewed plan.');
$manualMain = Yaml::parseFile($configRoot.'/libremapper.yaml');
$manualTopology = Yaml::parseFile($manualPath);
check(
    ($manualMain['manual_provider'] ?? null) === 'manual'
        && data_get($manualMain, 'providers.manual.settings.path') === 'manual-topology.yaml'
        && count($manualTopology['devices']) === 0
        && count($manualTopology['links']) === 1,
    'Guided manual links must enable and populate the existing manual topology.'
);
check(str_starts_with($manualTopology['links'][0]['a']['device'], 'librenms:')
    && str_starts_with($manualTopology['links'][0]['b']['device'], 'librenms:'),
    'Connections between LibreNMS devices must use direct inventory references.');
$manualPlan = $engine->plan('php-freshness');
$manualEdge = collect($manualPlan['operations'])->first(
    fn ($operation) => ($operation['kind'] ?? null) === 'edge'
        && collect($operation['sources'] ?? [])->contains(
            fn ($source) => ($source['provider'] ?? null) === 'manual'
        )
);
check(
    is_array($manualEdge) && ($manualEdge['protocol'] ?? null) === 'manual',
    'Plans must expose manual-link protocol and provider provenance.'
);
$manualData = $controller->viewData(Request::create(
    '/plugin/settings/libremapper?map=php-freshness&tab=links&subtab=manual'
));
$manualHtml = view('libremapper::settings', $manualData)->render();
check(! str_contains($manualHtml, 'Technical details'), 'Manual connections must omit technical identifiers.');
check(
    str_contains($manualHtml, 'Add missing connection')
        && str_contains($manualHtml, 'Manual records ·')
        && str_contains($manualHtml, 'Remove from all maps'),
    'Links must provide global creation, provenance, and removal controls.'
);
$manualSourceId = collect($manualEdge['sources'])->firstWhere('provider', 'manual')['source_id'];
$manualLinksUrl = route('plugin.settings', ['plugin' => 'libremapper', 'map' => 'php-freshness', 'tab' => 'links', 'subtab' => 'manual']);
check(str_contains($manualHtml, 'href="'.e($manualLinksUrl).'#manual-connections"') && str_contains($manualHtml, 'id="manual-connections"')
    && str_contains($manualHtml, 'id="manual-record-'.hash('sha256', $manualSourceId).'"'),
    'Manual source references must link to the actual editable shared record.');
$oxidizedLinksData = $manualData;
$oxidizedLinksData['activeSubtab'] = 'connections';
$oxidizedLinksData['providers']['oxidized'] = ['type' => 'oxidized'];
foreach ($oxidizedLinksData['plan']['link_choices'] as &$choice) {
    $choice['sources'][] = ['provider' => 'oxidized', 'source_id' => 'neighbor'];
}
unset($choice);
$oxidizedLinksHtml = view('libremapper::settings', $oxidizedLinksData)->render();
check(str_contains($oxidizedLinksHtml, 'subtab=parsers">Links → Parsers</a>'),
    'Oxidized sources must link directly to Links → Parsers.');
check(str_starts_with($manualSourceId, 'manual:'), 'New manual IDs must be global, not map-specific.');
$previousOldInput = $session->getOldInput();
$builderData = $manualData + [
    'planNodes' => collect($manualPlan['operations'])->where('kind', 'node')->where('action', '!=', 'delete')->keyBy('generated_values.device_id'),
    'editingLink' => collect($engine->manualLinks())->firstWhere('source_id', $manualSourceId),
];
$session->flashInput(['source_id' => $manualSourceId, 'a_device_id' => '1002', 'a_interface' => 'custom', 'a_label' => 'Failed edit label']);
$failedEditHtml = view('libremapper::link-form', $builderData)->render();
$unrelatedAddHtml = view('libremapper::link-form', array_replace($builderData, ['editingLink' => null]))->render();
check(str_contains($failedEditHtml, 'value="Failed edit label"') && ! str_contains($unrelatedAddHtml, 'Failed edit label'), 'Failed edit input must stay in its connection form.');
$session->flashInput(['a_device_id' => '1002', 'a_interface' => 'custom', 'a_label' => 'Failed add label']);
$failedAddHtml = view('libremapper::link-form', array_replace($builderData, ['editingLink' => null]))->render();
$unrelatedEditHtml = view('libremapper::link-form', $builderData)->render();
check(str_contains($failedAddHtml, 'value="Failed add label"') && ! str_contains($unrelatedEditHtml, 'Failed add label'), 'Failed add input must not replace existing connection values.');
$session->flashInput($previousOldInput);
$otherPlan = $engine->plan('php-other');
check(collect($engine->manualLinks())->firstWhere('source_id', $manualSourceId)['used_maps'] === [$mapName, $mapName.' Other'], 'Global relationships must show which reviewed maps use them.');
fails(
    fn () => $engine->addManualLink('php-other', $manualNodes[1]['canonical_id'], $manualNodes[0]['canonical_id'], manualInput($engine, 'php-other')),
    'already exists'
);
$inclusionRequest = Request::create('/plugin/libremapper/maps/php-freshness/manual-links/inclusion', 'PUT', [
    'source_id' => $manualSourceId, 'included' => '0',
    ...manualInput($engine, 'php-freshness'),
]);
$inclusionRequest->setLaravelSession($session);
$inclusionResponse = $controller->includeManualLink($inclusionRequest, 'php-freshness');
check(str_contains($inclusionResponse->getTargetUrl(), 'tab=links'), 'Inclusion saves must return to Links.');
check(! is_file($engine->planPath('php-freshness')), 'Map inclusion must invalidate its reviewed plan.');
check(! is_file($engine->planPath('php-other')), 'Visibility changes must invalidate reviews sharing the configuration fingerprint.');
check(count(Yaml::parseFile($manualPath)['links']) === 1, 'Excluding a global link must keep its source observation.');
$excluded = $engine->plan('php-freshness');
check($excluded['link_fallbacks'] === [] && count($otherPlan['link_fallbacks']) === 1, 'Exclusions must affect only the selected map.');
$engine->includeManualLink('php-freshness', $manualSourceId, true, manualInput($engine, 'php-freshness'));
$inlineMain = Yaml::parseFile($configRoot.'/libremapper.yaml');
$inlineMain['maps'][] = $otherMap;
$inlineMain['maps'][0]['key'] = 'php-inline';
$inlineMain['maps'][0]['name'] = $mapName.' Inline';
$engine->saveFile('libremapper.yaml', str_replace('maps: {  }', 'maps: []', dumpTestConfig($inlineMain, 12, 2)), hash_file('sha256', $configRoot.'/libremapper.yaml'));
$engine->includeManualLink('php-inline', $manualSourceId, false, manualInput($engine, 'php-inline'));
check(Yaml::parseFile($configRoot.'/libremapper.yaml')['maps'][0]['manual_link_exclusions'] === [$manualSourceId], 'Legacy inline maps must support exclusions.');
$inlineMain['maps'] = [];
$engine->saveFile('libremapper.yaml', str_replace('maps: {  }', 'maps: []', dumpTestConfig($inlineMain, 12, 2)), hash_file('sha256', $configRoot.'/libremapper.yaml'));
// Existing Guided IDs remain editable across maps.
$legacyTopology = Yaml::parseFile($manualPath);
$legacyTopology['links'][0]['source_id'] = 'guided:old-map:legacy';
$engine->saveFile('manual-topology.yaml', dumpTestConfig($legacyTopology, 12, 2, Yaml::DUMP_EMPTY_ARRAY_AS_SEQUENCE), hash_file('sha256', $manualPath));
$engine->plan('php-freshness');
$engine->plan('php-other');
$selectionPlan = $engine->reviewedPlan('php-freshness');
$selectionIds = array_column($selectionPlan['link_choices'], 'canonical_id');
check(count($selectionIds) > 0, 'Reviewed connections must be available for map selection.');
$selection = manualInput($engine, 'php-freshness', [
    'plan_sha256' => $selectionPlan['plan_sha256'], 'link_ids' => $selectionIds,
    'included_ids' => array_column(array_filter($selectionPlan['link_choices'], fn ($choice) => $choice['included']), 'canonical_id'),
]);
fails(fn () => $engine->saveLinkSelection('php-freshness', array_replace($selection, ['link_ids' => []])), 'incomplete');
$engine->saveLinkSelection('php-freshness', $selection);
$engine->plan('php-freshness');
$engine->plan('php-other');
$manualSourceId = 'guided:old-map:legacy';
fails(fn () => $engine->removeManualLinks('php-freshness', [$manualSourceId, 'missing'], manualInput($engine, 'php-freshness')), 'read-only');
check(count(Yaml::parseFile($manualPath)['links']) === 1, 'A rejected batch must leave every connection intact.');
$engine->removeManualLink('php-freshness', $manualSourceId, manualInput($engine, 'php-freshness'));
check(! is_file($engine->planPath('php-freshness')) && ! is_file($engine->planPath('php-other')), 'Global removal must invalidate every map review.');
$manualTopology = Yaml::parseFile($manualPath);
check(
    count($manualTopology['devices']) === 0 && $manualTopology['links'] === [],
    'Removing a link must preserve the shared device catalog.'
);

$interfaceRequest = Request::create('/plugin/libremapper/devices/1001/interfaces');
$interfaceRequest->setUserResolver(fn () => $admin);
$interfaceResponse = $controller->interfaceOptions($interfaceRequest, 1001)->getData(true);
check(count($interfaceResponse['interfaces']) > 0
    && isset($interfaceResponse['interfaces'][0]['port_id'], $interfaceResponse['interfaces'][0]['name'], $interfaceResponse['interfaces'][0]['usable']), 'The authenticated interface route must return classified LibreNMS port options.');
$leftPort = DB::table('ports')->where('device_id', 1001)->where('deleted', 0)->orderBy('port_id')->first();
$rightPort = DB::table('ports')->where('device_id', 1002)->where('deleted', 0)->orderBy('port_id')->first();
check($leftPort !== null && $rightPort !== null, 'Link-builder tests require one real port on each fixture device.');
$unknownEndpoints = ['a_device_id' => 1001, 'b_device_id' => 1002, 'a_interface' => '', 'b_interface' => ''];
$engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', $unknownEndpoints));
$logicalLink = $engine->manualLinks()[0];
$stableSourceId = $logicalLink['source_id'];
check($logicalLink['relationship_kind'] === 'logical' && ! isset($logicalLink['a']['interface']) && ! isset($logicalLink['b']['interface']), 'Unknown interfaces must create a logical network-wide fallback.');
$logicalPlan = $engine->plan('php-freshness');
check(collect($logicalPlan['operations'])->contains(fn ($item) => $item['kind'] === 'edge' && $item['protocol'] === 'manual' && ($item['generated_values']['port_id'] ?? null) === null), 'Logical fallbacks must not bind traffic counters.');
$engine->plan('php-other');
$realEndpoints = [
    'source_id' => $stableSourceId, 'a_device_id' => 1001, 'b_device_id' => 1002,
    'a_interface' => (string) $leftPort->port_id, 'b_interface' => (string) $rightPort->port_id,
];
$engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', $realEndpoints));
check(! is_file($engine->planPath('php-freshness')) && ! is_file($engine->planPath('php-other')), 'Editing a connection must invalidate every reviewed plan.');
$realLink = $engine->manualLinks()[0];
check(count($engine->manualLinks()) === 1 && $realLink['source_id'] === $stableSourceId
    && $realLink['a_endpoint']['librenms_port_id'] === $leftPort->port_id
    && $realLink['b_endpoint']['librenms_port_id'] === $rightPort->port_id, 'Real port selections must preserve exact IDs and edit the existing manual source.');
fails(fn () => $engine->saveManualDevice('php-freshness', manualInput($engine, 'php-freshness', [
    'hostname' => 'Duplicate inventory device', 'librenms_device_id' => 1002, 'layer' => 0,
])), 'matches LibreNMS inventory');
$realPlan = $engine->plan('php-freshness');
check(collect($realPlan['operations'])->contains(fn ($item) => $item['kind'] === 'edge'
    && in_array($item['generated_values']['port_id'] ?? null, [$leftPort->port_id, $rightPort->port_id], true)), 'Real port endpoints must bind counters to the selected LibreNMS port.');
$beforeInvalidLink = file_get_contents($manualPath);
fails(fn () => $engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', $realEndpoints + ['manual_sha256' => str_repeat('0', 64)])), 'File changed since it was loaded');
fails(fn () => $engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', array_replace($realEndpoints, ['a_interface' => (string) $rightPort->port_id]))), 'belongs to another device');
fails(fn () => $engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', array_replace($realEndpoints, ['a_interface' => $leftPort->port_id.'invalid']))), 'selected interface');
check(file_get_contents($manualPath) === $beforeInvalidLink && is_file($engine->planPath('php-freshness')), 'Rejected endpoints must preserve configuration and the existing reviewed plan.');
fails(fn () => $engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', array_replace($realEndpoints, ['b_device_id' => 1001, 'b_interface' => '']))), 'two different devices');
fails(fn () => $engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', array_replace($realEndpoints, ['b_device_id' => 99999999]))), 'current LibreNMS device');
fails(fn () => $engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', array_replace($realEndpoints, ['a_interface' => 'custom', 'a_label' => '']))), 'custom interface label');
$engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', array_replace($realEndpoints, [
    'a_interface' => 'custom', 'a_label' => $leftPort->ifName ?: $leftPort->ifDescr,
    'b_interface' => 'custom', 'b_label' => 'Documented remote connector',
])));
$customLink = $engine->manualLinks()[0];
check($customLink['source_id'] === $stableSourceId
    && ($customLink['a_endpoint']['attributes']['manual_label_only'] ?? false) === true
    && ($customLink['b_endpoint']['attributes']['manual_label_only'] ?? false) === true
    && ! isset($customLink['a_endpoint']['librenms_port_id'])
    && ! isset($customLink['b_endpoint']['librenms_port_id']), 'Custom labels must stay descriptive even when their names match real ports.');
$customPlan = $engine->plan('php-freshness');
check(collect($customPlan['operations'])->contains(fn ($item) => $item['kind'] === 'edge' && $item['protocol'] === 'manual' && ($item['generated_values']['port_id'] ?? null) === null), 'Custom interface labels must never bind counters.');
$engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', array_replace($realEndpoints, ['b_interface' => 'custom', 'b_label' => 'Remote connector'])));
$mixedLink = $engine->manualLinks()[0];
check($mixedLink['a_endpoint']['librenms_port_id'] === $leftPort->port_id
    && ($mixedLink['b_endpoint']['attributes']['manual_label_only'] ?? false), 'A partially known connection must combine a real port with a custom remote label.');
$engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', array_replace($realEndpoints, ['b_interface' => ''])));
$partialLink = $engine->manualLinks()[0];
check($partialLink['source_id'] === $stableSourceId && ! isset($partialLink['b']['interface']), 'A partially known connection may leave the remote interface unspecified.');
$engine->removeManualLink('php-freshness', $stableSourceId, manualInput($engine, 'php-freshness'));
check($engine->manualLinks() === [] && count(Yaml::parseFile($manualPath)['devices']) === 0, 'Removing an edited connection must not create manual device records.');
$unknownRemote = [
    'a_device_id' => '1001', 'a_interface' => (string) $leftPort->port_id,
    'b_device_id' => 'unknown', 'b_remote_label' => 'Uncatalogued peer', 'b_interface' => '',
];
fails(fn () => $engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', array_replace($unknownRemote, ['b_remote_label' => '']))), 'Name the unknown remote endpoint');
fails(fn () => $engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', array_replace($unknownRemote, ['b_interface' => (string) $rightPort->port_id]))), 'unknown endpoint cannot bind');
$inventoryHostname = collect($engine->inventory()['devices'])->firstWhere('device_id', 1002)['hostname'];
fails(fn () => $engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', array_replace($unknownRemote, ['b_remote_label' => $inventoryHostname]))), 'matches LibreNMS inventory');
fails(fn () => $engine->saveManualDevice('php-freshness', manualInput($engine, 'php-freshness', ['hostname' => $inventoryHostname, 'layer' => 0])), 'matches LibreNMS inventory');
$engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', $unknownRemote));
$unknownLink = $engine->manualLinks()[0];
check($unknownLink['a_device_id'] === 1001 && $unknownLink['b_device_id'] === null && $unknownLink['editable']
    && ! isset($unknownLink['b']['interface']), 'Unknown remote devices must remain editable external endpoints.');
$unknownPlan = $engine->plan('php-freshness');
check(collect($unknownPlan['operations'])->contains(fn ($item) => $item['kind'] === 'node'
    && ($item['generated_values']['device_id'] ?? null) === null && str_contains($item['display_name'], 'Uncatalogued peer')), 'The reviewed topology must include the documented external endpoint.');
$engine->removeManualLink('php-freshness', $unknownLink['source_id'], manualInput($engine, 'php-freshness'));
$engine->saveFile('manual-topology.yaml', $manual, hash_file('sha256', $manualPath));
$engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', array_replace($unknownRemote, ['b_interface' => 'custom', 'b_label' => 'Remote xe-0/0/0'])));
$unknownCustomLink = $engine->manualLinks()[0];
check($unknownCustomLink['b_endpoint']['name'] === 'Remote xe-0/0/0'
    && ($unknownCustomLink['b_endpoint']['attributes']['manual_label_only'] ?? false) === true
    && ! isset($unknownCustomLink['b_endpoint']['librenms_port_id']), 'Unknown endpoints must retain custom interface labels without binding counters.');
$engine->saveFile('manual-topology.yaml', $manual, hash_file('sha256', $manualPath));

fwrite(STDOUT, "PHP: manual device and graph controls.\n");
$engine->saveManualDevice('php-freshness', manualInput($engine, 'php-freshness', [
    'hostname' => 'External standalone', 'layer' => 4, 'short_hostname_parts' => 1, 'image' => '',
]));
$createdManualDevice = collect($engine->manualCatalog()['devices'])->firstWhere('hostname', 'External standalone');
$manualDeviceId = $createdManualDevice['source_id'];
check(in_array($manualDeviceId, Yaml::parseFile($mapPath)['manual_device_inclusions'], true), 'New manual devices must seed the current map.');
$standalonePlan = $engine->plan('php-freshness');
check(collect($standalonePlan['operations'])->contains(fn ($item) => $item['kind'] === 'node' && $item['display_name'] === 'External standalone'), 'Standalone inclusions must appear in review.');
$staleManualInput = manualInput($engine, 'php-freshness');
$engine->saveManualDevice('php-freshness', manualInput($engine, 'php-freshness', [
    'source_id' => $manualDeviceId, 'hostname' => 'Renamed standalone', 'layer' => 2, 'image' => '',
]));
check(collect($engine->manualCatalog()['devices'])->firstWhere('hostname', 'Renamed standalone')['source_id'] === $manualDeviceId, 'Device edits must preserve source identity.');
fails(fn () => $engine->includeManualDevice('php-freshness', $manualDeviceId, false, $staleManualInput), 'File changed since it was loaded');
$engine->includeManualDevice('php-other', $manualDeviceId, true, manualInput($engine, 'php-other'));
$engine->includeManualDevice('php-freshness', $manualDeviceId, false, manualInput($engine, 'php-freshness'));
$excludedManualPlan = $engine->plan('php-freshness');
check(! collect($excludedManualPlan['operations'])->contains(fn ($item) => $item['kind'] === 'node' && $item['action'] !== 'delete' && $item['display_name'] === 'Renamed standalone'), 'Manual exclusions must remove standalone devices.');
$engine->includeManualDevice('php-freshness', $manualDeviceId, true, manualInput($engine, 'php-freshness'));
$engine->saveManualDevice('php-freshness', manualInput($engine, 'php-freshness', ['hostname' => 'Second external', 'layer' => 0]));
$secondExternal = collect($engine->manualCatalog()['devices'])->firstWhere('hostname', 'Second external');
$beforeLayerMap = file_get_contents($mapPath);
$beforeLayerCatalog = file_get_contents($manualPath);
$beforeOtherMap = file_get_contents($configRoot.'/maps/php-other.yaml');
$manualLayerInput = ['placement_action' => 'apply', 'placement_device_ids' => [1001],
    'placement_manual_device_ids' => [$manualDeviceId, $secondExternal['source_id'], $manualDeviceId],
    'placement' => '-6', 'layout_type' => 'top', 'layout_infer_layers' => false];
$manualLayerDraft = $engine->guidedFile('php-freshness', manualInput($engine, 'php-freshness', $manualLayerInput));
$manualLayerRules = Yaml::parse($manualLayerDraft['content'])['layer_overrides'];
check(array_slice($manualLayerRules, 0, 3) === [
    ['type' => 'device_id', 'value' => 1001, 'layer' => -6],
    ['type' => 'canonical', 'value' => 'manual:'.$manualDeviceId, 'layer' => -6],
    ['type' => 'canonical', 'value' => 'manual:'.$secondExternal['source_id'], 'layer' => -6],
], 'Mixed layer selection must deduplicate manual devices and retain their stable source identities.');
$engine->saveFile($manualLayerDraft['path'], $manualLayerDraft['content'], $manualLayerDraft['sha256']);
$manualLayerPlan = $engine->plan('php-freshness');
foreach (['Renamed standalone', 'Second external'] as $name) {
    check(collect($manualLayerPlan['operations'])->firstWhere('display_name', $name)['layer'] === -6, 'Manual devices must use map-specific layers in reviewed layouts.');
}
check(file_get_contents($manualPath) === $beforeLayerCatalog && file_get_contents($configRoot.'/maps/php-other.yaml') === $beforeOtherMap,
    'Map-specific manual layers must not change the shared catalog or other maps.');
foreach (['gone', [$manualDeviceId, 'gone'], [[$manualDeviceId]]] as $invalidManualSelection) {
    fails(fn () => $engine->guidedFile('php-freshness', manualInput($engine, 'php-freshness', array_replace($manualLayerInput, ['placement_manual_device_ids' => $invalidManualSelection]))), 'device');
}
fails(fn () => $engine->guidedFile('php-freshness', manualInput($engine, 'php-freshness', $manualLayerInput + ['manual_sha256' => str_repeat('0', 64)])), 'File changed since it was loaded');
fails(fn () => $engine->guidedFile('php-freshness', manualInput($engine, 'php-freshness', $manualLayerInput + ['map_hash' => str_repeat('0', 64)])), 'Draft changed');
$manualClearDraft = $engine->guidedFile('php-freshness', manualInput($engine, 'php-freshness', ['placement_manual_device_ids' => [$manualDeviceId], 'placement' => '']));
$engine->saveFile($manualClearDraft['path'], $manualClearDraft['content'], $manualClearDraft['sha256']);
$manualClearPlan = collect($engine->plan('php-freshness')['operations']);
check($manualClearPlan->firstWhere('display_name', 'Renamed standalone')['layer'] === 2
    && $manualClearPlan->firstWhere('display_name', 'Second external')['layer'] === -6,
    'Clearing a manual layer must restore its shared default and retain unselected overrides.');
$engine->saveFile('maps/php-freshness.yaml', $beforeLayerMap, hash_file('sha256', $mapPath));
$engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', [
    'a_device_id' => 'manual:'.$manualDeviceId, 'b_device_id' => 'manual:'.$secondExternal['source_id'],
]));
$externalLink = collect($engine->manualLinks())->first(fn ($item) => $item['a']['device'] === $manualDeviceId);
check($externalLink['editable'], 'External-to-external links must be editable.');
$secondLinkId = $engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', [
    'a_device_id' => 'manual:'.$manualDeviceId, 'b_device_id' => 'manual:'.$secondExternal['source_id'],
    'a_interface' => 'custom', 'a_label' => 'Second connection',
]));
fails(fn () => $engine->removeManualDevice('php-freshness', $manualDeviceId, manualInput($engine, 'php-freshness')), $externalLink['source_id']);
fails(fn () => $engine->removeManualDevices('php-freshness', [$manualDeviceId, $secondExternal['source_id']], manualInput($engine, 'php-freshness')), $externalLink['source_id']);
check(collect($engine->manualCatalog()['devices'])->contains('source_id', $secondExternal['source_id']), 'A blocked batch must leave every device intact.');
$engine->removeManualLinks('php-freshness', [$externalLink['source_id'], $secondLinkId], manualInput($engine, 'php-freshness'));
check(! collect($engine->manualLinks())->contains('source_id', $secondLinkId), 'Batch removal must remove all selected connections.');
$engine->removeManualDevices('php-freshness', [$manualDeviceId, $secondExternal['source_id']], manualInput($engine, 'php-freshness'));
check(! in_array($manualDeviceId, Yaml::parseFile($configRoot.'/maps/php-other.yaml')['manual_device_inclusions'], true), 'Deleting unused devices must remove membership across maps.');
check(! collect($engine->manualCatalog()['devices'])->contains('source_id', $secondExternal['source_id']), 'Batch deletion must remove all selected devices.');

// A file-defined external link must survive a form round trip without losing metadata.
$roundTrip = Yaml::parseFile($manualPath);
$roundTrip['devices'] = [
    ['source_id' => 'round-a', 'hostname' => 'Roundtrip A', 'attributes' => ['custom' => 'keep'],
        'interfaces' => [['source_id' => 'connector', 'device_source_id' => 'round-a', 'name' => 'Optical connector', 'attributes' => ['custom' => 'keep']]]],
    ['source_id' => 'round-b', 'hostname' => 'Roundtrip B'],
];
$roundTrip['links'] = [['source_id' => 'round-link', 'protocol' => 'private-protocol', 'confidence' => 0.8,
    'attributes' => ['custom' => 'keep'], 'a' => ['device' => 'round-a', 'interface' => 'connector'], 'b' => ['device' => 'round-b']]];
$roundTrip['suppress_links'] = [['a' => ['device' => 'round-a'], 'b' => ['device' => 'round-b'], 'protocol' => 'lldp']];
$engine->saveFile('manual-topology.yaml', dumpTestConfig($roundTrip, 12, 2, Yaml::DUMP_EMPTY_ARRAY_AS_SEQUENCE), hash_file('sha256', $manualPath));
$engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', [
    'source_id' => 'round-link', 'a_device_id' => 'manual:round-a', 'b_device_id' => 'manual:round-b', 'a_interface' => 'file:connector',
]));
check(Yaml::parseFile($manualPath) === $roundTrip, 'UI → Advanced → UI round trips must preserve unexposed file fields.');
$jsonMainBefore = file_get_contents($configRoot.'/libremapper.yaml');
$engine->createFile('shared-manual.json', json_encode($roundTrip, JSON_THROW_ON_ERROR));
$jsonMain = Yaml::parse($jsonMainBefore);
$jsonMain['providers']['manual']['settings']['path'] = $configRoot.'/shared-manual.json';
$engine->saveFile('libremapper.yaml', dumpTestConfig($jsonMain, 12, 2), hash_file('sha256', $configRoot.'/libremapper.yaml'));
$engine->saveManualLink('php-freshness', manualInput($engine, 'php-freshness', [
    'source_id' => 'round-link', 'a_device_id' => 'manual:round-a', 'b_device_id' => 'manual:round-b', 'a_interface' => 'file:connector',
]));
check(json_decode(file_get_contents($configRoot.'/shared-manual.json'), true, flags: JSON_THROW_ON_ERROR) === $roundTrip, 'Editors must use the configured JSON file and preserve its format.');
check(Yaml::parseFile($configRoot.'/libremapper.yaml')['providers']['manual']['settings']['path'] === 'shared-manual.json', 'Managed absolute file paths must become relative for staged validation.');
$engine->saveFile('libremapper.yaml', $jsonMainBefore, hash_file('sha256', $configRoot.'/libremapper.yaml'));
$engine->deleteFile('shared-manual.json', hash_file('sha256', $configRoot.'/shared-manual.json'));

fails(fn () => $engine->removeManualDevice('php-freshness', 'round-a', manualInput($engine, 'php-freshness')), 'suppress_links');
$engine->saveFile('manual-topology.yaml', $manual, hash_file('sha256', $manualPath));

$graphPlan = $engine->plan('php-freshness');
$graphEdge = collect($graphPlan['operations'])->first(fn ($item) => $item['kind'] === 'edge' && $item['action'] !== 'delete' && count($item['graph']['endpoint_device_ids'] ?? []) > 0);
if ($graphEdge) {
    $owner = $graphEdge['graph']['endpoint_device_ids'][0];
    $alternate = DB::table('ports')->where('device_id', $owner)->where('deleted', 0)->first();
    $graphInput = ['canonical_id' => $graphEdge['canonical_id'], 'port_id' => $alternate->port_id,
        'map_hash' => manualInput($engine, 'php-freshness')['map_hash']];
    $engine->saveGraphOverride('php-freshness', $graphInput);
    $overriddenPlan = $engine->plan('php-freshness');
    $overriddenEdge = collect($overriddenPlan['operations'])->firstWhere('canonical_id', $graphEdge['canonical_id']);
    check($overriddenEdge['graph']['mode'] === 'overridden' && $overriddenEdge['generated_values']['port_id'] === $alternate->port_id, 'Reviewed graph choices must use live endpoint ownership.');
    $graphInput['map_hash'] = manualInput($engine, 'php-freshness')['map_hash'];
    $graphInput['port_id'] = null;
    $engine->saveGraphOverride('php-freshness', $graphInput);
    $engine->plan('php-freshness');
    check(Yaml::parseFile($mapPath)['link_graph_overrides'][0]['port_id'] === null, 'Automatic must be recorded explicitly.');
    $graphInput['map_hash'] = manualInput($engine, 'php-freshness')['map_hash'];
    $graphInput['remove'] = true;
    $engine->saveGraphOverride('php-freshness', $graphInput);
}

$racingEngine = new class(null, '/data/libremapper', $configRoot) extends Engine
{
    public string $racePath = '';

    public function inventory(): array
    {
        $inventory = parent::inventory();
        if ($this->racePath !== '') {
            file_put_contents($this->racePath, "# concurrent writer\n", FILE_APPEND);
            $this->racePath = '';
        }
        return $inventory;
    }
};
$beforeRace = file_get_contents($manualPath);
$raceInput = manualInput($engine, 'php-freshness', $unknownEndpoints);
$racingEngine->racePath = $manualPath;
fails(fn () => $racingEngine->saveManualLink('php-freshness', $raceInput), 'Configuration changed during validation');
check(file_get_contents($manualPath) === $beforeRace."# concurrent writer\n", 'An external edit during record resolution must survive the rejected batch.');
$engine->saveFile('manual-topology.yaml', $beforeRace, hash_file('sha256', $manualPath));

$advancedTopology = $legacyTopology;
$advancedTopology['links'][0]['source_id'] = 'core-reference:ab';
foreach (['a', 'b'] as $side) {
    $reference = $advancedTopology['links'][0][$side]['device'];
    $advancedTopology['devices'][] = ['source_id' => $reference, 'librenms_device_id' => (int) substr($reference, 9)];
}
$engine->createFile('core-reference.yaml', dumpTestConfig($advancedTopology, 12, 2, Yaml::DUMP_EMPTY_ARRAY_AS_SEQUENCE));
$advancedMain = Yaml::parseFile($configRoot.'/libremapper.yaml');
$advancedMain['providers']['core-reference'] = ['type' => 'file', 'settings' => ['path' => 'core-reference.yaml']];
$engine->saveFile('libremapper.yaml', str_replace('maps: {  }', 'maps: []', dumpTestConfig($advancedMain, 12, 2)), hash_file('sha256', $configRoot.'/libremapper.yaml'));
$advancedMap = Yaml::parseFile($mapPath);
$advancedMap['sources'][] = ['provider' => 'core-reference'];
unset($advancedMap['manual_link_exclusions']);
$engine->saveFile('maps/php-freshness.yaml', dumpTestConfig($advancedMap, 12, 2), hash_file('sha256', $mapPath));
$engine->plan('php-freshness');
$advancedData = $controller->viewData(Request::create('/plugin/settings/libremapper?map=php-freshness&tab=links'));
$advancedHtml = view('libremapper::settings', $advancedData)->render();
check($advancedData['activeTab'] === 'links' && str_contains($advancedHtml, 'Edit read-only source in Advanced') && ! str_contains($advancedHtml, 'Technical details'), 'Advanced file sources must remain accessible without technical details.');
fails(fn () => $engine->removeManualLink('php-freshness', 'core-reference:ab', manualInput($engine, 'php-freshness')), 'read-only');
fails(fn () => $engine->includeManualLink('php-freshness', 'core-reference:ab', false, manualInput($engine, 'php-freshness')), 'Choose an editable global');

$originalSettings = (array) $plugin->settings;
$freshRoot = '/data/libremapper/runtime/candidate-'.bin2hex(random_bytes(8));
$oldDefaultRoot = '/data/libremapper/runtime/candidate-'.bin2hex(random_bytes(8));
$customizedRoot = '/data/libremapper/runtime/candidate-'.bin2hex(random_bytes(8));
$migrationRoot = '/data/libremapper/runtime/candidate-'.bin2hex(random_bytes(8));
$cleanupPaths = array_merge($cleanupPaths, [$freshRoot, $oldDefaultRoot, $customizedRoot, $migrationRoot]);
foreach ([$freshRoot, $oldDefaultRoot, $customizedRoot, $migrationRoot] as $root) {
    mkdir($root);
}
$legacy = <<<YAML
version: 1
state_path: /legacy/state.sqlite3
manual_provider: manual
providers:
  manual: {type: file, settings: {path: /legacy/manual.yaml}}
  native: &native {type: librenms, settings: {}}
maps:
  - key: migrated-map
    name: Migrated Map
    sources:
      - provider: native
        selectors: [{type: device_id, value: 1001}]
YAML;
try {
    $plugin->settings = [];
    $plugin->saveOrFail();
    (new Engine(null, '/data/libremapper', $freshRoot))->ensureConfig($plugin);
    $freshMain = Yaml::parseFile($freshRoot.'/libremapper.yaml');
    check(
        $freshMain['providers'] === ['librenms' => ['type' => 'librenms', 'settings' => []]]
            && $freshMain['maps'] === []
            && scandir($freshRoot.'/maps') === ['.', '..'],
        'Fresh defaults must configure librenms without installing a sample map.'
    );
    $firstMapData = (new LibremapperController(new Engine(null, '/data/libremapper', $freshRoot)))->viewData(Request::create('/plugin/settings/libremapper'));
    check($firstMapData['newMap'] && str_contains(view('libremapper::settings', $firstMapData)->render(), 'Create your first map'), 'An empty installation must open the first-map workspace.');

    $oldDefault = "version: 1\nstate_path: /data/libremapper/state.sqlite3\nmap_directory: maps\nlibrenms: {  }\nproviders: {  }\nmaps: []\n";
    foreach ([$oldDefaultRoot, $customizedRoot] as $root) {
        mkdir($root.'/maps');
        file_put_contents($root.'/libremapper.yaml', $oldDefault);
        file_put_contents($root.'/manual-topology.yaml', $manual);
    }
    file_put_contents($customizedRoot.'/custom.yaml', "version: 1\n");
    $plugin->settings = ['migration_complete' => true];
    $plugin->saveOrFail();
    (new Engine(null, '/data/libremapper', $oldDefaultRoot))->ensureConfig($plugin);
    (new Engine(null, '/data/libremapper', $customizedRoot))->ensureConfig($plugin);
    check(
        data_get(Yaml::parseFile($oldDefaultRoot.'/libremapper.yaml'), 'providers.librenms.type') === 'librenms',
        'An exact old untouched default tree must gain the librenms provider.'
    );
    check(
        Yaml::parseFile($customizedRoot.'/libremapper.yaml')['providers'] === []
            && is_file($customizedRoot.'/custom.yaml'),
        'A customized configuration tree must remain untouched during upgrade.'
    );

    $plugin->settings = [
        'config_yaml' => $legacy,
        'manual_topology_yaml' => $manual,
        'scheduled_maps' => ['migrated-map'],
        'initialized_maps' => ['migrated-map'],
    ];
    $plugin->saveOrFail();
    $migrationEngine = new Engine(
        null,
        '/data/libremapper',
        $migrationRoot
    );
    $migrationEngine->ensureConfig($plugin);
    $migratedSettings = (array) $plugin->fresh()->settings;
    check($migratedSettings['migration_complete'] === true, 'Migration must be marked complete.');
    check($migratedSettings['config_yaml'] === $legacy, 'Legacy database YAML must remain frozen.');
    $migratedMain = Yaml::parseFile($migrationRoot.'/libremapper.yaml');
    $migratedMap = Yaml::parseFile($migrationRoot.'/maps/migrated-map.yaml');
    check($migratedMain['maps'] === [], 'Migration must split inline maps.');
    check($migratedMain['map_directory'] === 'maps', 'Migration must configure map fragments.');
    check($migratedMain['providers']['manual']['settings']['path'] === 'manual-topology.yaml', 'Manual topology path must be relative.');
    check($migratedMap['scheduled'] === true, 'Migration must move schedule choices into maps.');
} finally {
    $plugin->settings = $originalSettings;
    $plugin->saveOrFail();
}

$filesBeforeReset = $engine->files();
$backup = $engine->reset('RESET');
$cleanupPaths[] = $backup;
check(is_dir($backup.'/maps'), 'Reset must back up the complete configuration tree.');
check(file_get_contents($backup.'/'.$backupName) === $backupContents, 'Reset backups must retain inert operator backups.');
check(count($filesBeforeReset) === 5, 'Reset test must include both maps and the advanced source fixture.');
check($engine->mapKeys() === [], 'Reset must install a valid empty configuration.');
check(
    data_get($engine->configuration(), 'providers.librenms.type') === 'librenms',
    'Reset defaults must configure the librenms provider.'
);
fails(fn () => $engine->reset('yes'), 'Type RESET');

$lock = fopen('/data/libremapper/action.lock', 'c+');
check($lock !== false && flock($lock, LOCK_EX | LOCK_NB), 'Test must acquire the process lock.');
try {
    fails(fn () => $engine->validateCurrent(), 'already running');
} finally {
    flock($lock, LOCK_UN);
    fclose($lock);
}

require __DIR__.'/parsers.php';

$cleanup();
check(! is_dir($testRoot), 'PHP test state and temporary configuration must be removed.');
check(
    ! DB::table('custom_maps')->where('name', 'like', 'PHP Freshness %')->exists(),
    'PHP freshness maps must be removed after the test.'
);

echo "PHP plugin checks passed.\n";
