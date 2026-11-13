<?php

namespace Libremapper\Plugin\Http;

use App\Models\Plugin;
use Illuminate\Http\RedirectResponse;
use Illuminate\Http\Request;
use Illuminate\Routing\Controller;
use Illuminate\Support\Facades\Storage;
use Libremapper\Plugin\Engine;
use Libremapper\Plugin\InterfaceOptions;
use Libremapper\Plugin\McpAccess;
use Illuminate\Http\JsonResponse;
use Symfony\Component\Yaml\Yaml;
use Throwable;

class LibremapperController extends Controller
{
    public function __construct(private Engine $engine)
    {
    }

    public function index(Request $request): RedirectResponse
    {
        $subtab = $request->query('subtab', match ($request->query('tab')) { 'oxidized' => 'parsers', 'files' => 'files', default => '' });
        return $this->settingsRedirect((string) $request->query('map'), self::workspaceTab((string) $request->query('tab', 'devices')), is_string($request->query('file')) ? $request->query('file') : null, (string) $subtab, section: (string) $request->query('section', ''));
    }

    /** @return array<string, mixed> */
    public function viewData(Request $request): array
    {
        $plugin = $this->plugin();
        $settings = Engine::settings((array) $plugin->settings);
        $configError = null;
        $config = ['providers' => [], 'maps' => []];
        try {
            $this->engine->ensureConfig($plugin);
            $this->engine->workspaceValidation();
            $config = $this->engine->configuration();
            $this->engine->recordConfigError($plugin, null);
            $settings = Engine::settings((array) $plugin->fresh()->settings);
        } catch (Throwable $error) {
            $configError = $error->getMessage();
            try {
                $this->engine->recordConfigError($plugin, $error);
            } catch (Throwable) {
                // The Files tab and reset remain available even if metadata cannot be recorded.
            }
        }

        $defaultTab = $configError ? 'advanced' : 'devices';
        $requestedTab = self::workspaceTab((string) $request->query('tab', $defaultTab));
        $activeTab = is_string($requestedTab) && in_array(
            $requestedTab,
            ['devices', 'links', 'layout', 'review', 'advanced'],
            true
        ) ? $requestedTab : $defaultTab;

        $activeSubtab = (string) $request->query('subtab', $request->query('tab') === 'oxidized' ? 'parsers' : ($request->query('tab') === 'files' || $request->has('file') || $configError ? 'files' : ''));
        if ($activeTab === 'links' && $activeSubtab === 'oxidized') $activeSubtab = 'parsers';
        if ($activeTab === 'advanced' && $request->has('file')) $activeSubtab = 'files';
        $subtabs = match ($activeTab) {
            'links' => ['connections', 'parsers', 'manual'],
            'layout' => ['topology', 'overrides'],
            'advanced' => ['configuration', 'appearance', 'oxidized', 'parsers', 'agent-access', 'files', 'maintenance'],
            default => ['inventory', 'matched', 'manual'],
        };
        if (! in_array($activeSubtab, $subtabs, true)) $activeSubtab = $subtabs[0];
        $oxidizedSection = $request->query('section') === 'report' ? 'report' : 'source';
        $maps = array_values(array_filter($config['maps'] ?? [], 'is_array'));
        foreach ($maps as &$item) {
            if (! isset($item['key']) && isset($item['name'])) {
                $item['key'] = trim((string) preg_replace(
                    '/[^a-z0-9]+/', '-', strtolower((string) $item['name'])
                ), '-');
            }
        }
        unset($item);
        $providers = is_array($config['providers'] ?? null) ? $config['providers'] : [];
        $manualProvider = is_string($config['manual_provider'] ?? null)
            ? $config['manual_provider']
            : null;
        $librenmsProviders = array_keys(array_filter(
            $providers,
            fn ($provider): bool => is_array($provider) && ($provider['type'] ?? null) === 'librenms'
        ));
        sort($librenmsProviders, SORT_NATURAL | SORT_FLAG_CASE);
        $oxidizedProvider = is_array($providers['oxidized'] ?? null)
            && ($providers['oxidized']['type'] ?? null) === 'oxidized'
            ? $providers['oxidized']
            : null;
        $oxidizedSettings = is_array($oxidizedProvider['settings'] ?? null)
            ? $oxidizedProvider['settings']
            : [];
        $oxidizedRepository = is_string($oxidizedSettings['repository'] ?? null)
            ? $oxidizedSettings['repository']
            : '';
        $oxidizedMode = $oxidizedProvider === null
            ? 'disabled'
            : (str_starts_with($oxidizedRepository, '/') ? 'local' : 'gitea');
        $oxidizedRef = is_string($oxidizedSettings['ref'] ?? null)
            ? $oxidizedSettings['ref']
            : 'HEAD';
        $oxidizedMaps = array_values(array_filter(array_map(
            static function (array $item): ?string {
                $enabled = collect($item['sources'] ?? [])->contains(
                    fn ($source) => is_array($source) && ($source['provider'] ?? null) === 'oxidized'
                );

                return $enabled ? (string) ($item['name'] ?? $item['key'] ?? 'Unnamed map') : null;
            },
            $maps
        )));
        $oxidizedInspection = null;
        $oxidizedInspectionError = null;
        $canCreateGuidedMap = is_string($config['map_directory'] ?? null);
        $requested = $request->query('map', $maps[0]['key'] ?? '__new__');
        $requestedKey = is_string($requested) ? $requested : '';
        $newMap = $requestedKey === '__new__';
        $map = $newMap ? [] : collect($maps)->first(
            fn ($item) => ($item['key'] ?? null) === $requestedKey
        );
        if (! $newMap && ! $map) {
            $map = $maps[0] ?? [];
            $newMap = ! $map;
        }
        $selectedKey = $newMap ? '__new__' : (string) ($map['key'] ?? '');
        $source = collect($map['sources'] ?? [])->first(
            fn ($item) => is_array($item) && ($providers[$item['provider']]['type'] ?? null) === 'librenms'
        ) ?? [];
        $usesOxidized = collect($map['sources'] ?? [])->contains(
            fn ($item) => is_array($item) && ($item['provider'] ?? null) === 'oxidized'
        );
        $parserCatalog = ['items' => [], 'selections' => []];
        $parserCatalogError = null;
        if ($configError === null && (($activeTab === 'advanced' && $activeSubtab === 'parsers')
            || ($activeTab === 'links' && $activeSubtab === 'parsers'))) {
            try { $parserCatalog = $this->engine->parserCatalog(); }
            catch (Throwable $error) { $parserCatalogError = $error->getMessage(); }
        }
        $mapParsers = $parserCatalog['selections'][$selectedKey]['oxidized'] ?? [];
        if ($activeTab === 'links' && $activeSubtab === 'parsers' && $oxidizedSection === 'report'
            && $oxidizedProvider !== null && ! $newMap && $configError === null) {
            try { $oxidizedInspection = $this->engine->providerInspection('oxidized', $selectedKey); }
            catch (Throwable $error) { $oxidizedInspectionError = $error->getMessage(); }
        }
        $plan = null;
        if (! $newMap && $selectedKey !== '' && is_file($this->engine->planPath($selectedKey))) {
            try {
                $plan = $this->engine->reviewedPlan($selectedKey);
            } catch (Throwable) {
                $plan = null;
            }
        }
        $manualCatalog = ['path' => 'manual-topology.yaml', 'sha256' => '', 'devices' => []];
        $manualLinks = [];
        $manualLinksError = null;
        if ($configError === null) {
            try {
                $manualCatalog = $this->engine->manualCatalog();
            } catch (Throwable $error) {
                $manualLinksError = $error->getMessage();
            }
        }
        try {
            $files = $this->engine->files();
        } catch (Throwable) {
            $files = [];
        }
        $newFile = ! $files || $request->query('file') === '__new__';
        $requestedFile = $newFile ? null : (string) $request->query('file', '');
        $selectedFile = $newFile ? null : collect($files)->first(
            fn ($file) => $file['path'] === $requestedFile
        ) ?? ($files[0] ?? null);
        try {
            $nodeImages = array_values(array_filter(array_map(
                fn (string $path): ?string => in_array(
                    strtolower(pathinfo($path, PATHINFO_EXTENSION)),
                    ['svg', 'png', 'jpg', 'gif'],
                    true
                ) ? pathinfo($path, PATHINFO_BASENAME) : null,
                Storage::disk('base')->files('html/images/custommap/icons')
            )));
            sort($nodeImages, SORT_NATURAL | SORT_FLAG_CASE);
        } catch (Throwable) {
            $nodeImages = [];
        }
        $inventoryDevices = [];
        $inventoryGroups = [];
        $inventoryLocations = [];
        $inventoryError = null;
        if ($configError === null && ($activeTab === 'devices'
            || ($activeTab === 'links' && $activeSubtab !== 'parsers')
            || ($activeTab === 'layout' && $activeSubtab === 'overrides' && ! $newMap))) {
            try {
                $inventory = $this->engine->workspaceInventory();
                $inventoryDevices = $inventory['devices'] ?? [];
                $inventoryGroups = $inventory['groups'];
                $inventoryLocations = $inventory['locations'];
            } catch (Throwable $error) {
                $inventoryError = $error->getMessage();
            }
        }

        if ($configError === null && (($activeTab === 'devices' && $activeSubtab === 'manual')
            || ($activeTab === 'links' && $activeSubtab !== 'parsers'))) {
            try {
                $manualLinks = $this->engine->manualLinks($inventoryError === null ? $inventoryDevices : null);
            } catch (Throwable $error) {
                $manualLinksError = $error->getMessage();
            }
        }

        foreach ($manualCatalog['devices'] as &$manualDevice) {
            $associations = Engine::manualAssociationCandidates($manualDevice, $inventoryDevices);
            $manualDevice['_association_id'] = count($associations) === 1 ? $associations[0] : null;
            $manualDevice['_association_count'] = count($associations);
        }
        unset($manualDevice);

        $draft = !$newMap && $this->engine->hasDraft($selectedKey);
        $agentTokens = $activeTab === 'advanced' && $activeSubtab === 'agent-access' && $request->user()?->can('api.access')
            ? McpAccess::tokenOptions($request->user()) : collect();
        return compact(
            'draft', 'settings', 'maps', 'map', 'source', 'selectedKey', 'plan', 'files', 'selectedFile',
            'newFile', 'newMap', 'canCreateGuidedMap', 'librenmsProviders', 'configError',
            'nodeImages', 'inventoryDevices', 'inventoryGroups', 'inventoryLocations', 'inventoryError',
            'oxidizedMode', 'oxidizedRepository', 'oxidizedRef', 'oxidizedProvider', 'usesOxidized',
            'oxidizedMaps', 'oxidizedInspection', 'oxidizedInspectionError', 'manualProvider',
            'activeTab', 'activeSubtab', 'oxidizedSection', 'parserCatalog', 'parserCatalogError', 'mapParsers', 'manualCatalog', 'manualLinks', 'manualLinksError', 'providers', 'agentTokens'
        ) + [
            'selectorsYaml' => $newMap ? '' : Yaml::dump($source['selectors'] ?? [], 8, 2),
            'parserEditorId' => (string) $request->query('edit', ''),
            'parserCloneId' => (string) $request->query('clone', ''),
            'mainSha256' => collect($files)->firstWhere('path', 'libremapper.yaml')['sha256'] ?? '',
            'layerOverridesYaml' => Yaml::dump($map['layer_overrides'] ?? [], 8, 2),
        ];
    }

    public function update(Request $request): RedirectResponse
    {
        $validated = $request->validate([
            'original_key' => 'required|string',
            'scheduled' => 'nullable|boolean',
        ]);
        $key = (string) $validated['original_key'];
        $isNew = $key === '__new__';
        try {
            $plugin = $this->plugin();
            $this->engine->ensureConfig($plugin);
            $settings = Engine::settings((array) $plugin->settings);
            if (! $isNew && $request->boolean('scheduled')
                && ! in_array($key, $settings['initialized_maps'], true)) {
                throw new \RuntimeException('Apply this map successfully before enabling its schedule.');
            }
            $input = $request->all();
            foreach (['layout_auto_grow', 'layout_infer_layers', 'edge_showpct', 'edge_showbps', 'scheduled', 'include_oxidized'] as $field) {
                if ($request->has($field)) {
                    $input[$field] = $request->boolean($field);
                }
            }
            $file = $this->engine->guidedFile($key, $input);
            if ($isNew) {
                $this->engine->createFile($file['path'], $file['content']);
            } else {
                $this->engine->saveFile($file['path'], $file['content'], $file['sha256']);
            }

            return $this->settingsRedirect($file['key'], self::workspaceTab((string) $request->input('tab', 'devices')), subtab: (string) $request->input('subtab', ''))->with(
                'status',
                $isNew ? 'Map draft created. Review changes before publishing.' : 'Draft saved. Review changes before publishing.'
            );
        } catch (Throwable $error) {
            return back()->withInput()->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public function updateOxidized(Request $request): RedirectResponse
    {
        $validated = $request->validate([
            'mode' => 'required|string|in:disabled,gitea,local',
            'repository' => 'nullable|string|max:2048',
            'ref' => 'nullable|string|max:255',
            'password' => 'nullable|string|max:65536',
            'sha256' => 'required|string|size:64',
        ]);
        try {
            $this->engine->ensureConfig($this->plugin());
            $this->engine->configureOxidized(
                (string) $validated['mode'],
                (string) ($validated['repository'] ?? ''),
                (string) ($validated['ref'] ?? 'HEAD'),
                (string) ($validated['password'] ?? ''),
                sha256: $validated['sha256']
            );

            return $this->settingsRedirect((string) $request->input('map'), 'advanced', subtab: 'oxidized')->with(
                'status',
                $validated['mode'] === 'disabled'
                    ? 'Oxidized source disabled.'
                    : 'Oxidized connection verified and saved as a draft.'
            );
        } catch (Throwable $error) {
            return back()->withInput($request->except('password'))->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public function createFile(Request $request): RedirectResponse
    {
        $validated = $request->validate(['path' => 'required|string', 'content' => 'required|string|max:5242880']);
        try {
            $this->engine->ensureConfig($this->plugin());
            $this->engine->createFile((string) $validated['path'], (string) $validated['content']);

            return $this->settingsRedirect((string) $request->input('map'), 'files', file: (string) $validated['path'])
                ->with('status', 'Configuration file created.');
        } catch (Throwable $error) {
            return back()->withInput()->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public function saveParser(Request $request): RedirectResponse
    {
        $input = $request->validate([
            'id' => 'required|string|max:64', 'original_id' => 'nullable|string|max:64',
            'definition' => 'required|string|max:65536', 'sha256' => 'required|string|size:64',
        ]);
        try {
            $this->engine->ensureConfig($this->plugin());
            $original = (string) ($input['original_id'] ?? '');
            if ($original !== '' && $original !== $input['id']) throw new \RuntimeException('Parser IDs cannot change during editing. Clone the parser instead.');
            $definition = json_decode($input['definition'], true, flags: JSON_THROW_ON_ERROR);
            if (! is_array($definition) || array_is_list($definition)) throw new \RuntimeException('Enter a parser definition object.');
            $this->engine->saveParser($input['id'], $definition, $input['sha256'], $original === '');
            return redirect()->route('plugin.settings', ['plugin' => Engine::PLUGIN, 'map' => $request->input('map'),
                'tab' => 'advanced', 'subtab' => 'parsers', 'edit' => $input['id']])
                ->with('status', 'Global parser saved. Maps using it are drafts; review changes before publishing.');
        } catch (Throwable $error) {
            return back()->withInput($request->only(['id', 'original_id', 'definition']))->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public function parserAction(Request $request): RedirectResponse
    {
        $input = $request->validate(['id' => 'required|string|max:64', 'sha256' => 'required|string|size:64', 'action' => 'required|in:reset,delete']);
        try {
            $this->engine->ensureConfig($this->plugin());
            $reset = $input['action'] === 'reset';
            $this->engine->removeParser($input['id'], $input['sha256'], $reset);
            return redirect()->route('plugin.settings', array_filter(['plugin' => Engine::PLUGIN, 'map' => $request->input('map'),
                'tab' => 'advanced', 'subtab' => 'parsers', 'edit' => $reset ? $input['id'] : null]))
                ->with('status', $reset ? 'Built-in parser reset to its packaged definition.' : 'Unused parser deleted.');
        } catch (Throwable $error) {
            return back()->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public function parserPreview(Request $request): JsonResponse
    {
        try {
            $raw = $request->getContent();
            if (strlen($raw) > 1048576) throw new \RuntimeException('Preview request exceeds 1 MiB.');
            // Preserve artifact whitespace and empty samples through Laravel's input normalizers.
            $input = json_decode($raw, true, flags: JSON_THROW_ON_ERROR);
            if (! is_array($input) || array_is_list($input)) throw new \RuntimeException('Preview input must be a JSON object.');
            return response()->json($this->engine->previewParser($input, str_ends_with($request->path(), '/files')));
        } catch (Throwable $error) {
            return response()->json(['error' => $error->getMessage()], 422);
        }
    }

    public function saveParserSelection(Request $request, string $map): RedirectResponse
    {
        $input = $request->validate(['sha256' => 'required|string|size:64', 'map_hash' => 'required|string|size:64',
            'parsers' => 'sometimes|array', 'parsers.*' => 'required|string|max:64|distinct']);
        try {
            $this->engine->ensureConfig($this->plugin());
            $this->engine->saveParserSelection($map, $input['parsers'] ?? [], $input);
            return $this->settingsRedirect($map, 'links', subtab: 'parsers')->with('status', 'Parser selection saved for this map. Review changes before publishing.');
        } catch (Throwable $error) {
            return back()->withInput()->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public function saveFile(Request $request): RedirectResponse
    {
        $validated = $request->validate([
            'path' => 'required|string',
            'content' => 'required|string|max:5242880',
            'sha256' => 'required|string|size:64',
            'force' => 'nullable|boolean',
        ]);
        try {
            $this->engine->ensureConfig($this->plugin());
            $this->engine->saveFile(
                (string) $validated['path'],
                (string) $validated['content'],
                (string) $validated['sha256'],
                $request->boolean('force')
            );

            return $this->settingsRedirect((string) $request->input('map'), 'files', file: (string) $validated['path'])
                ->with('status', 'Configuration file saved.');
        } catch (Throwable $error) {
            return back()->withInput()->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public function deleteFile(Request $request): RedirectResponse
    {
        $validated = $request->validate([
            'path' => 'required|string',
            'sha256' => 'required|string|size:64',
            'force' => 'nullable|boolean',
        ]);
        try {
            $this->engine->ensureConfig($this->plugin());
            $this->engine->deleteFile(
                (string) $validated['path'],
                (string) $validated['sha256'],
                $request->boolean('force')
            );
            $nextFile = $this->engine->files()[0]['path'] ?? null;

            return $this->settingsRedirect((string) $request->input('map'), 'files', file: $nextFile)
                ->with('status', 'Configuration file deleted.');
        } catch (Throwable $error) {
            return back()->with('conflict_path', $validated['path'])
                ->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public function reset(Request $request): RedirectResponse
    {
        $validated = $request->validate(['confirmation' => 'required|string']);
        try {
            $backup = $this->engine->reset((string) $validated['confirmation']);

            return $this->settingsRedirect(tab: 'files')
                ->with('status', "Defaults restored. Backup saved to $backup.");
        } catch (Throwable $error) {
            return back()->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public function plan(Request $request, string $map): RedirectResponse
    {
        $validated = $request->validate(['adopt' => 'nullable|boolean', 'relayout' => 'nullable|boolean']);
        try {
            $plugin = $this->plugin();
            $this->engine->ensureConfig($plugin);
            $plan = $this->engine->reviewMap(
                $plugin, $map,
                (bool) ($validated['adopt'] ?? false),
                (bool) ($validated['relayout'] ?? false)
            );

            return $this->settingsRedirect($map, in_array($request->input('tab'), ['links', 'layout', 'devices'], true) ? $request->input('tab') : 'review')
                ->with('status', 'Plan ready for review.');
        } catch (Throwable $error) {
            return back()->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public function apply(Request $request, string $map): RedirectResponse
    {
        try {
            $plugin = $this->plugin();
            $this->engine->ensureConfig($plugin);
            $validated = $request->validate(['plan_sha256' => 'required|string|size:64', 'confirm_warnings' => 'nullable|boolean']);
            $result = $this->engine->publishMap($plugin, $map, $validated['plan_sha256'], $request->boolean('confirm_warnings'));
            $message = ! $result['status_refreshed'] ? 'Published to LibreNMS, but status refresh failed. Review changes again to refresh status.'
                : ($result['remaining_changes'] === 0 ? 'Published to LibreNMS. The map is current.' : 'Published to LibreNMS. New inventory changes are ready for review.');
            return $this->settingsRedirect($map, 'review')->with('status', $message);
        } catch (Throwable $error) {
            return back()->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public function addManualLink(Request $request, string $map): RedirectResponse
    {
        $validated = $request->validate([
            'a' => 'required|string|max:512',
            'b' => 'required|string|max:512',
        ]);
        try {
            $this->engine->ensureConfig($this->plugin());
            $this->engine->addManualLink($map, (string) $validated['a'], (string) $validated['b'], $this->manualFreshness($request));

            return $this->settingsRedirect($map, 'links', subtab: 'manual')->with(
                'status',
                'Global manual link saved. Refresh reconciliation before applying.'
            );
        } catch (Throwable $error) {
            return back()->withInput()->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public function removeManualLink(Request $request, string $map): RedirectResponse
    {
        $validated = $request->validate(['source_id' => 'required_without:source_ids|string|max:512', 'source_ids' => 'required_without:source_id|array|min:1', 'source_ids.*' => 'required|string|max:512|distinct']);
        try {
            $this->engine->ensureConfig($this->plugin());
            $this->engine->removeManualLinks($map, $validated['source_ids'] ?? [$validated['source_id']], $this->manualFreshness($request));

            return $this->settingsRedirect($map, 'links', subtab: 'manual')->with(
                'status',
                'Shared manual connections removed from the draft. Review changes before publishing.'
            );
        } catch (Throwable $error) {
            return back()->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public function saveLinkSelection(Request $request, string $map): RedirectResponse
    {
        $input = $request->validate([
            'map_hash' => 'required|string|size:64', 'plan_sha256' => 'required|string|size:64',
            'link_ids' => 'sometimes|array', 'link_ids.*' => 'required|string|size:24|distinct',
            'included_ids' => 'sometimes|array', 'included_ids.*' => 'required|string|size:24|distinct',
            'manual_included_ids' => 'sometimes|array', 'manual_included_ids.*' => 'required|string|max:512|distinct',
        ]);
        try {
            $this->engine->ensureConfig($this->plugin());
            $this->engine->saveLinkSelection($map, $input);
            return $this->settingsRedirect($map, 'links', subtab: 'connections')->with('status', 'Connection selection saved for this map. Review changes before publishing.');
        } catch (Throwable $error) {
            return back()->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public function includeManualLink(Request $request, string $map): RedirectResponse
    {
        $validated = $request->validate([
            'source_id' => 'required|string|max:512',
            'included' => 'required|boolean',
        ]);
        try {
            $this->engine->ensureConfig($this->plugin());
            $this->engine->includeManualLink($map, (string) $validated['source_id'], (bool) $validated['included'], $this->manualFreshness($request));

            return $this->settingsRedirect($map, 'links', subtab: 'manual')->with(
                'status', 'Map inclusion saved. Refresh reconciliation before applying.'
            );
        } catch (Throwable $error) {
            return back()->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public static function workspaceTab(string $tab): string
    {
        return ['guided' => 'devices', 'oxidized' => 'links', 'files' => 'advanced'][$tab] ?? $tab;
    }

    public function interfaceOptions(Request $request, int $device): JsonResponse
    {
        return response()->json(['interfaces' => InterfaceOptions::options($device, $request->user())]);
    }

    public function saveManualLink(Request $request, string $map): RedirectResponse
    {
        $input = $request->validate([
            'source_id' => 'nullable|string|max:512',
            'a_device_id' => 'required|string|max:1024', 'b_device_id' => 'required|string|max:1024',
            'a_remote_label' => 'nullable|string|max:255', 'b_remote_label' => 'nullable|string|max:255',
            'a_interface' => 'nullable|string|max:1024', 'b_interface' => 'nullable|string|max:1024',
            'a_label' => 'nullable|string|max:255', 'b_label' => 'nullable|string|max:255',
        ]);
        try {
            $this->engine->ensureConfig($this->plugin());
            $this->engine->saveManualLink($map, $input + $this->manualFreshness($request));
            return $this->settingsRedirect($map, 'links', subtab: 'manual')->with('status', 'Network-wide manual connection saved as a draft. Review changes to publish.');
        } catch (Throwable $error) {
            return back()->withInput()->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    private function manualFreshness(Request $request): array
    {
        return $request->validate(['manual_sha256' => 'required|string|size:64', 'map_hash' => 'required|string|size:64']);
    }

    public function saveManualDevice(Request $request, string $map): RedirectResponse
    {
        $input = $request->validate([
            'source_id' => 'nullable|string|max:512', 'hostname' => 'required|string|max:255',
            'librenms_device_id' => 'nullable|integer|min:1', 'image' => 'nullable|string|max:255',
            'layer' => 'required|integer', 'short_hostname_parts' => 'nullable|integer|min:1|max:127',
        ]) + $this->manualFreshness($request);
        try {
            $this->engine->ensureConfig($this->plugin());
            $this->engine->saveManualDevice($map, $input);
            return $this->settingsRedirect($map, 'devices', subtab: 'manual')->with('status', 'Shared manual device saved as a draft. Review changes to publish.');
        } catch (Throwable $error) {
            return back()->withInput()->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public function includeManualDevice(Request $request, string $map): RedirectResponse
    {
        $input = $request->validate(['source_id' => 'required|string|max:512', 'included' => 'required|boolean']) + $this->manualFreshness($request);
        try {
            $this->engine->ensureConfig($this->plugin());
            $this->engine->includeManualDevice($map, $input['source_id'], (bool) $input['included'], $input);
            return $this->settingsRedirect($map, 'devices', subtab: 'manual')->with('status', 'Manual device membership saved as a draft.');
        } catch (Throwable $error) {
            return back()->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public function removeManualDevice(Request $request, string $map): RedirectResponse
    {
        $input = $request->validate(['source_id' => 'required_without:source_ids|string|max:512', 'source_ids' => 'required_without:source_id|array|min:1', 'source_ids.*' => 'required|string|max:512|distinct']) + $this->manualFreshness($request);
        try {
            $this->engine->ensureConfig($this->plugin());
            $this->engine->removeManualDevices($map, $input['source_ids'] ?? [$input['source_id']], $input);
            return $this->settingsRedirect($map, 'devices', subtab: 'manual')->with('status', 'Shared manual device removed from draft configuration.');
        } catch (Throwable $error) {
            return back()->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    public function saveGraphOverride(Request $request, string $map): RedirectResponse
    {
        $input = $request->validate([
            'canonical_id' => 'required|string|max:512', 'port_id' => 'nullable|integer|min:1',
            'map_hash' => 'required|string|size:64', 'remove' => 'nullable|boolean',
        ]);
        try {
            $this->engine->ensureConfig($this->plugin());
            $this->engine->saveGraphOverride($map, $input);
            $this->engine->plan($map);
            return $this->settingsRedirect($map, 'links')->with('status', 'Traffic interface saved as a draft. Review changes to publish.');
        } catch (Throwable $error) {
            return back()->withInput()->withErrors(['libremapper' => $error->getMessage()]);
        }
    }

    private function settingsRedirect(
        ?string $map = null,
        ?string $tab = null,
        ?string $file = null,
        ?string $subtab = null,
        ?string $section = null
    ): RedirectResponse
    {
        return redirect()->route('plugin.settings', array_filter([
            'plugin' => Engine::PLUGIN,
            'map' => $map,
            'tab' => $tab === null ? null : self::workspaceTab($tab),
            'file' => $file,
            'subtab' => $subtab ?? match ($tab) { 'oxidized' => 'parsers', 'files' => 'files', default => null },
            'section' => $section,
        ], fn ($value): bool => $value !== null && $value !== ''));
    }

    private function plugin(): Plugin
    {
        return Plugin::query()->where('plugin_name', Engine::PLUGIN)->where('version', 2)->firstOrFail();
    }

}
