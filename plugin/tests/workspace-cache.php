<?php

declare(strict_types=1);

use Illuminate\Contracts\Console\Kernel;
use Illuminate\Support\Carbon;
use Illuminate\Support\Facades\Cache;
use Libremapper\Plugin\Engine;

require '/opt/librenms/vendor/autoload.php';
require '/opt/libremapper/package/plugin/src/Engine.php';
$app = require '/opt/librenms/bootstrap/app.php';
$app->make(Kernel::class)->bootstrap();
set_exception_handler(static function (Throwable $error): void {
    fwrite(STDERR, $error->getMessage()."\n");
    exit(1);
});
Cache::setDefaultDriver('array');
Cache::flush();
Carbon::setTestNow('2026-01-01 00:00:00');

$root = sys_get_temp_dir().'/libremapper-cache-'.bin2hex(random_bytes(8));
mkdir($root);
mkdir($root.'/config');
file_put_contents($root.'/config/libremapper.yaml', "version: 1\n");
$makeEngine = fn ($token = 'test') => new class($root, $token) extends Engine {
    public int $validations = 0;
    public int $inventories = 0;
    public bool $fail = false;
    public bool $changeConfig = false;
    private string $config;

    public function __construct(string $root, string $token)
    {
        parent::__construct('http://sidecar', $root, $root.'/config', $token);
        $this->config = $root.'/config/libremapper.yaml';
    }
    public function validateCurrent(): array
    {
        $this->validations++;
        if ($this->fail) throw new RuntimeException('validation failed');
        return ['valid' => true];
    }
    public function inventory(): array
    {
        $this->inventories++;
        if ($this->fail) throw new RuntimeException('inventory failed');
        if ($this->changeConfig) file_put_contents($this->config, "version: changed\n");
        return ['devices' => [], 'groups' => [], 'locations' => []];
    }
};
try {
    $engine = $makeEngine();
    $engine->workspaceValidation();
    $engine->workspaceInventory();
    $engine->workspaceValidation();
    $engine->workspaceInventory();
    if ($engine->validations !== 1 || $engine->inventories !== 1) throw new RuntimeException('Repeated workspace reads must use the cache.');
    $second = $makeEngine();
    $second->workspaceValidation();
    $second->workspaceInventory();
    if ($second->validations !== 0 || $second->inventories !== 0) throw new RuntimeException('Cache must work across engine instances.');
    $otherToken = $makeEngine('different-token');
    $otherToken->workspaceValidation();
    if ($otherToken->validations !== 1) throw new RuntimeException('Cache must be isolated by sidecar credentials.');
    file_put_contents($root.'/config/libremapper.yaml', "version: 2\n");
    $engine->workspaceValidation();
    $engine->workspaceInventory();
    if ($engine->validations !== 2 || $engine->inventories !== 2) throw new RuntimeException('Configuration changes must invalidate cached reads.');
    Carbon::setTestNow('2026-01-01 00:00:16');
    $engine->workspaceValidation();
    $engine->workspaceInventory();
    if ($engine->validations !== 3 || $engine->inventories !== 3) throw new RuntimeException('Cached reads must expire after 15 seconds.');
    file_put_contents($root.'/config/libremapper.yaml', "version: 3\n");
    $engine->fail = true;
    foreach (['workspaceValidation', 'workspaceInventory'] as $method) {
        try { $engine->$method(); throw new RuntimeException('Expected failure'); }
        catch (RuntimeException $error) { if ($error->getMessage() === 'Expected failure') throw $error; }
    }
    $engine->fail = false;
    $engine->workspaceValidation();
    $engine->workspaceInventory();
    if ($engine->validations !== 5 || $engine->inventories !== 5) throw new RuntimeException('Failures must not be cached.');
    $engine->validateCurrent();
    $engine->inventory();
    if ($engine->validations !== 6 || $engine->inventories !== 6) throw new RuntimeException('Direct validation and inventory must stay fresh.');
    file_put_contents($root.'/config/libremapper.yaml', "version: 4\n");
    $engine->changeConfig = true;
    $engine->workspaceInventory();
    $engine->changeConfig = false;
    file_put_contents($root.'/config/libremapper.yaml', "version: 4\n");
    $engine->workspaceInventory();
    if ($engine->inventories !== 8) throw new RuntimeException('Reads during a configuration change must not be cached.');
    Cache::setDefaultDriver('unavailable');
    $engine->workspaceValidation();
    $engine->workspaceInventory();
    if ($engine->validations !== 7 || $engine->inventories !== 9) throw new RuntimeException('An unavailable cache must fall back to live reads.');
    Cache::setDefaultDriver('array');
} finally {
    Carbon::setTestNow();
    @unlink($root.'/action.lock');
    unlink($root.'/config/libremapper.yaml');
    rmdir($root.'/config');
    rmdir($root);
}
echo "Workspace cache checks passed.\n";
