<?php

namespace Libremapper\Plugin;

use Illuminate\Support\ServiceProvider;
use Libremapper\Plugin\Hooks\MenuEntry;
use Libremapper\Plugin\Hooks\Settings;
use LibreNMS\Interfaces\Plugins\Hooks\MenuEntryHook;
use LibreNMS\Interfaces\Plugins\Hooks\SettingsHook;
use LibreNMS\Interfaces\Plugins\PluginManagerInterface;

class LibremapperServiceProvider extends ServiceProvider
{
    public function register(): void
    {
        $this->app->singleton(Engine::class);
    }

    public function boot(PluginManagerInterface $plugins): void
    {
        $plugins->publishHook(Engine::PLUGIN, MenuEntryHook::class, MenuEntry::class);
        $plugins->publishHook(Engine::PLUGIN, SettingsHook::class, Settings::class);
        if (! $plugins->pluginEnabled(Engine::PLUGIN)) {
            return;
        }

        $this->app->make(\Illuminate\Contracts\Http\Kernel::class)->appendMiddlewareToGroup('web', WorkspaceRedirect::class);
        $this->loadRoutesFrom(__DIR__.'/../routes/web.php');
        $this->loadRoutesFrom(__DIR__.'/../routes/mcp.php');
        $this->loadViewsFrom(__DIR__.'/../resources/views', Engine::PLUGIN);
    }
}
