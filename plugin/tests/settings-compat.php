<?php

declare(strict_types=1);

use App\Models\Plugin;
use App\Models\User;
use Illuminate\Contracts\Console\Kernel;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Auth;
use Libremapper\Plugin\Engine;
use Libremapper\Plugin\Hooks\Settings;
use Libremapper\Plugin\WorkspaceRedirect;

require '/opt/librenms/vendor/autoload.php';
$app = require '/opt/librenms/bootstrap/app.php';
$app->make(Kernel::class)->bootstrap();
set_exception_handler(static function (Throwable $error): void {
    fwrite(STDERR, $error->getMessage()."\n".$error->getTraceAsString()."\n");
    exit(1);
});
if (getenv('LIBREMAPPER_DISABLE_SERVICES') !== 'true') throw new RuntimeException('Settings checks require the isolated synthetic stack.');
$router = $app['router'];
if (! in_array(WorkspaceRedirect::class, $router->gatherRouteMiddleware($router->getRoutes()->getByName('plugin.settings')), true)) {
    throw new RuntimeException('Legacy bookmark redirects must be registered on the Settings route.');
}
$admin = User::where('username', 'admin')->firstOrFail();
Auth::login($admin);
view()->share('errors', new \Illuminate\Support\ViewErrorBag);
if (! $admin->can('api.access')) throw new RuntimeException('Regression must exercise authenticated API-token enumeration.');
foreach (['devices', 'advanced'] as $tab) {
    $request = Request::create('/plugin/settings/libremapper', 'GET', ['tab' => $tab, 'subtab' => $tab === 'advanced' ? 'agent-access' : 'inventory']);
    $request->setUserResolver(fn () => $admin);
    $app->instance('request', $request);
    $result = (new Settings)->handle(Engine::PLUGIN, []);
    $html = view($result['content_view'], $result['settings'])->render();
    if (! str_contains($html, 'Libremapper') || ($tab === 'advanced' && ! str_contains($html, 'id="agent-access"'))) {
        throw new RuntimeException('Authenticated Settings must render the requested workspace and token controls.');
    }
}
if (! Plugin::where('plugin_name', Engine::PLUGIN)->value('plugin_active')) throw new RuntimeException('Settings must keep the plugin enabled.');
echo "Authenticated Settings compatibility checks passed.\n";
