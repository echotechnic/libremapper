<?php

namespace Libremapper\Plugin\Hooks;

use Illuminate\Contracts\Auth\Authenticatable;
use LibreNMS\Interfaces\Plugins\Hooks\MenuEntryHook;

class MenuEntry implements MenuEntryHook
{
    public function authorize(Authenticatable $user, array $settings = []): bool
    {
        return $user->can('plugin.admin');
    }

    public function handle(string $pluginName): array
    {
        return ["$pluginName::menu", []];
    }
}
