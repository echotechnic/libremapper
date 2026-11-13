<?php

declare(strict_types=1);

use Illuminate\Contracts\Console\Kernel;
use Illuminate\Http\Client\Request as HttpRequest;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Http;
use Libremapper\Plugin\Engine;
use Libremapper\Plugin\Http\LibremapperController;

require '/opt/librenms/vendor/autoload.php';
require '/opt/libremapper/package/plugin/src/Engine.php';
$app = require '/opt/librenms/bootstrap/app.php';
$app->make(Kernel::class)->bootstrap();
require '/opt/libremapper/package/plugin/src/Http/LibremapperController.php';
set_exception_handler(static function (Throwable $error): void {
    fwrite(STDERR, $error->getMessage()."\n");
    exit(1);
});
Cache::setDefaultDriver('array');

foreach ([
    'layout' => false, 'layout&subtab=overrides' => true, 'review' => false,
    'advanced' => false, 'devices' => true, 'devices&subtab=manual' => true,
    'links' => true, 'links&subtab=parsers' => false,
] as $tab => $needsInventory) {
    Cache::flush();
    Http::fake(fn (HttpRequest $request) => Http::response([
        'exit_code' => 0,
        'stdout' => match ($request['operation']) {
            'inventory' => json_encode(['devices' => [], 'groups' => [], 'locations' => []]),
            'catalog' => json_encode(['items' => [], 'selections' => []]),
            default => '',
        },
        'stderr' => '',
    ]));
    (new LibremapperController(app(Engine::class)))->viewData(
        Request::create('/plugin/settings/libremapper?map=generated-core-top&tab='.$tab)
    );
    $inventoryCalls = Http::recorded(fn (HttpRequest $request) => $request['operation'] === 'inventory')->count();
    if ($inventoryCalls !== (int) $needsInventory) {
        throw new RuntimeException("$tab made $inventoryCalls inventory calls; expected ".(int) $needsInventory);
    }
}
$root = sys_get_temp_dir().'/libremapper-workspace-'.bin2hex(random_bytes(8));
mkdir($root);
try {
    file_put_contents($root.'/libremapper.yaml', "manual_provider: manual\nproviders:\n  manual:\n    type: file\n    settings: {path: manual.yaml}\nmaps: []\n");
    file_put_contents($root.'/manual.yaml', "devices: []\nlinks:\n  - source_id: one\n    a: {device: 'librenms:1'}\n    b: {device: 'librenms:2'}\n");
    $devices = [['device_id' => 1, 'hostname' => 'a'], ['device_id' => 2, 'hostname' => 'b']];
    Http::fake(fn () => Http::response(['exit_code' => 0, 'stderr' => '',
        'stdout' => json_encode(['devices' => $devices, 'groups' => [], 'locations' => []])]));
    $engine = new Engine(dataRoot: $root, configRoot: $root, apiToken: 'test');
    $reused = $engine->manualLinks($devices);
    if (($reused[0]['display_name'] ?? '') !== 'a ↔ b' || Http::recorded()->count() !== 0) {
        throw new RuntimeException('Manual links must reuse the provided inventory without another request.');
    }
    $engine->manualLinks();
    if (Http::recorded()->count() !== 1) {
        throw new RuntimeException('Manual links must still fetch inventory when it was not provided.');
    }
} finally {
    unlink($root.'/libremapper.yaml');
    unlink($root.'/manual.yaml');
    rmdir($root);
}
echo "Workspace inventory checks passed.\n";
