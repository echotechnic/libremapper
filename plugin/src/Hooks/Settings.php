<?php

namespace Libremapper\Plugin\Hooks;

use Illuminate\Contracts\Auth\Authenticatable;
use Libremapper\Plugin\Http\LibremapperController;
use LibreNMS\Interfaces\Plugins\Hooks\SettingsHook;

class Settings implements SettingsHook
{
    public function authorize(Authenticatable $user, array $settings = []): bool
    {
        return $user->can('plugin.admin');
    }

    public function handle(string $pluginName, array $settings): array
    {
        return [
            'content_view' => "$pluginName::settings",
            'settings' => app(LibremapperController::class)->viewData(request()),
        ];
    }
}
