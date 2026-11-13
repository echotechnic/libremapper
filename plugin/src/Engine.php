<?php

namespace Libremapper\Plugin;

use App\Models\Plugin;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Http;
use RuntimeException;
use Symfony\Component\Yaml\Yaml;
use Throwable;

class Engine
{
    public const PLUGIN = 'libremapper';
    private const MAX_FILE_SIZE = 5 * 1024 * 1024;

    private const OLD_DEFAULT_CONFIG = "version: 1\nstate_path: /data/libremapper/state.sqlite3\nmap_directory: maps\nlibrenms: {}\nproviders: {}\nmaps: []\n";

    private const DEFAULT_CONFIG = "version: 1\nstate_path: /data/libremapper/state.sqlite3\nmap_directory: maps\nlibrenms: {}\nproviders:\n  librenms:\n    type: librenms\n    settings: {}\nmaps: []\n";

    private const DEFAULT_TOPOLOGY = "version: 1\ndevices: []\nlinks: []\nsuppress_links: []\n";

    private string $apiUrl;

    private string $apiToken;

    private string $dataRoot;

    private string $configRoot;

    private bool $lockHeld = false;

    public function __construct(
        ?string $apiUrl = null,
        ?string $dataRoot = null,
        ?string $configRoot = null,
        ?string $apiToken = null
    ) {
        $this->dataRoot = rtrim($dataRoot ?? getenv('LIBREMAPPER_DATA_ROOT') ?: '/data/libremapper', '/');
        $this->configRoot = rtrim($configRoot ?? getenv('LIBREMAPPER_CONFIG_ROOT') ?: '/config', '/');
        $this->apiUrl = rtrim($apiUrl ?? getenv('LIBREMAPPER_API_URL') ?: 'http://127.0.0.1:8001', '/');
        $this->apiToken = $apiToken ?? getenv('LIBREMAPPER_API_TOKEN') ?: '';
    }

    /** @return array{initialized_maps: array<int, string>, last_runs: array<string, mixed>, migration_complete: bool, config_error: ?string} */
    public static function settings(array $settings): array
    {
        return array_replace([
            'initialized_maps' => [],
            'last_runs' => [],
            'migration_complete' => false,
            'config_error' => null,
        ], $settings);
    }

    public function ensureConfig(Plugin $plugin): void
    {
        $this->locked(function () use ($plugin): void {
            $settings = self::settings((array) $plugin->settings);
            if ($settings['migration_complete']) {
                $this->requireConfigRoot();
                $this->repairOldDefaults();

                return;
            }
            $this->requireConfigRoot();
            if (count(scandir($this->configRoot) ?: []) > 2) {
                $this->repairOldDefaults();
                $settings['migration_complete'] = true;
                $plugin->settings = $settings;
                $plugin->saveOrFail();

                return;
            }
            $this->migrate($plugin, $settings);
        });
    }

    /** @return array<string, mixed> */
    public function validateCurrent(): array
    {
        return $this->locked(fn (): array => $this->validatePath($this->configPath(), structural: true));
    }

    public function workspaceValidation(): array
    {
        return $this->locked(fn (): array => $this->workspaceCache('validation', fn (): array => $this->validateCurrent()));
    }

    public function workspaceInventory(): array
    {
        return $this->workspaceCache('inventory', fn (): array => $this->inventory());
    }

    private function workspaceCache(string $kind, callable $load): array
    {
        try {
            $fingerprint = $this->treeFingerprint();
            $token = $this->apiToken ?: (is_readable($this->dataRoot.'/api-token')
                ? trim((string) file_get_contents($this->dataRoot.'/api-token')) : '');
            $key = 'libremapper:workspace:'.hash('sha256', implode("\0", [
                static::class, $kind, $this->apiUrl, $this->configRoot, $this->dataRoot, $token, $fingerprint,
            ]));
            $cached = Cache::get($key);
            if (is_array($cached)) return $cached;
        } catch (Throwable) {
            return $load();
        }

        $result = $load();
        try {
            // shortcut: display data can lag by 15 seconds; use live reads for decisions and writes.
            if (hash_equals($fingerprint, $this->treeFingerprint())) Cache::put($key, $result, 15);
        } catch (Throwable) {
            // An unavailable cache must not prevent the workspace from loading.
        }
        return $result;
    }

    /** @return array<string, mixed> */
    public function configuration(): array
    {
        [$config, $fragments] = $this->rawConfiguration();
        $maps = $config['maps'] ?? [];
        if (! is_array($maps) || ! array_is_list($maps)) {
            throw new RuntimeException('Main configuration maps must be a YAML list.');
        }
        foreach ($fragments as $map) {
            $maps[] = $map;
        }
        $config['maps'] = $maps;

        return $config;
    }

    /** @return array<int, string> */
    public function mapKeys(): array
    {
        return array_values(array_filter(array_map(
            fn ($map): ?string => is_array($map) ? $this->mapKey($map) : null,
            $this->configuration()['maps'] ?? []
        )));
    }

    /** @return array<int, string> */
    public function scheduledMapKeys(): array
    {
        return array_values(array_filter(array_map(
            fn ($map): ?string => is_array($map) && ($map['scheduled'] ?? false)
                ? $this->mapKey($map)
                : null,
            $this->configuration()['maps'] ?? []
        )));
    }

    /** @return array<int, array{path: string, content: string, sha256: string}> */
    public function files(): array
    {
        $files = [];
        foreach ($this->managedPaths(false) as $relative) {
            $path = $this->configRoot.'/'.$relative;
            $content = file_get_contents($path);
            if ($content === false) {
                continue;
            }
            $files[] = ['path' => $relative, 'content' => $content, 'sha256' => hash('sha256', $content)];
        }

        return $files;
    }

    public function createFile(string $relative, string $content): void
    {
        $this->changeFile('create', $relative, $content, '', false);
    }

    public function saveFile(string $relative, string $content, string $sha256, bool $force = false): void
    {
        $this->changeFile('save', $relative, $content, $sha256, $force);
    }

    public function deleteFile(string $relative, string $sha256, bool $force = false): void
    {
        $this->changeFile('delete', $relative, '', $sha256, $force);
    }

    /** @return array<string, mixed> */
    public function parserCatalog(?string $path = null): array
    {
        $result = $this->execute(['operation' => 'catalog', 'config' => $path ?? $this->configPath()]);
        $catalog = json_decode($result['stdout'], true, flags: JSON_THROW_ON_ERROR);
        if (! is_array($catalog['items'] ?? null) || ! is_array($catalog['selections'] ?? null)) {
            throw new RuntimeException('Libremapper sidecar returned an invalid parser catalog.');
        }
        return $catalog;
    }

    public function saveParser(string $id, array $definition, string $sha256, bool $create): void
    {
        $this->locked(function () use ($id, $definition, $sha256, $create): void {
            $this->assertCurrentHash($this->configPath(), $sha256, false);
            if (preg_match('/\A[a-z0-9][a-z0-9._-]{0,63}\z/', $id) !== 1) {
                throw new RuntimeException('Use a stable lowercase parser ID of at most 64 characters.');
            }
            $existing = collect($this->parserCatalog()['items'])->firstWhere('id', '===', $id);
            if ($create === ($existing !== null)) {
                throw new RuntimeException($create ? 'That parser ID already exists.' : 'The parser no longer exists.');
            }
            if (strlen(json_encode($definition, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR)) > 65536) {
                throw new RuntimeException('Parser definitions are limited to 64 KiB.');
            }
            [$main] = $this->rawConfiguration();
            $main['oxidized_parsers'][$id] = $definition;
            $this->saveFile('libremapper.yaml', $this->dumpConfig($main), $sha256);
        });
    }

    public function removeParser(string $id, string $sha256, bool $reset): void
    {
        $this->locked(function () use ($id, $sha256, $reset): void {
            $this->assertCurrentHash($this->configPath(), $sha256, false);
            $entry = collect($this->parserCatalog()['items'])->firstWhere('id', '===', $id);
            if ($entry === null) throw new RuntimeException('The parser no longer exists.');
            if ($reset !== $entry['builtin']) {
                throw new RuntimeException($entry['builtin'] ? 'Built-in parsers can be reset, not deleted.' : 'Only built-in parsers can be reset.');
            }
            if (! $reset && ($entry['maps'] || $entry['legacy_defaults'])) {
                throw new RuntimeException('Parser is still used by maps: '.implode(', ', $entry['maps'])
                    .'; source defaults: '.implode(', ', $entry['legacy_defaults']).'. Disable those references first.');
            }
            [$main] = $this->rawConfiguration();
            unset($main['oxidized_parsers'][$id]);
            if (empty($main['oxidized_parsers'])) unset($main['oxidized_parsers']);
            $this->saveFile('libremapper.yaml', $this->dumpConfig($main), $sha256);
        });
    }

    /** @return array<string, mixed> */
    public function previewParser(array $input, bool $files = false): array
    {
        $payload = array_intersect_key($input, array_flip(['provider', 'parser_id', 'definition', 'filename', 'sample', 'query']));
        if (strlen(json_encode($payload, JSON_THROW_ON_ERROR)) > 1048576) throw new RuntimeException('Preview request exceeds 1 MiB.');
        $result = $this->execute(['operation' => $files ? 'parser-files' : 'preview', 'config' => $this->configPath()] + $payload);
        return json_decode($result['stdout'], true, flags: JSON_THROW_ON_ERROR);
    }

    public function saveParserSelection(string $map, array $parsers, array $input): void
    {
        $this->locked(function () use ($map, $parsers, $input): void {
            $this->assertCurrentHash($this->configPath(), (string) ($input['sha256'] ?? ''), false);
            $catalog = $this->parserCatalog();
            if (! array_is_list($parsers) || count(array_filter($parsers, 'is_string')) !== count($parsers)
                || count(array_unique($parsers)) !== count($parsers)
                || array_diff($parsers, array_column($catalog['items'], 'id'))) {
                throw new RuntimeException('Choose current parsers without duplicates.');
            }
            $before = $this->treeFingerprint();
            [$relative, $document, $index] = $this->mapDocument($map, (string) ($input['map_hash'] ?? ''));
            if ($index === null) $config = &$document; else $config = &$document['maps'][$index];
            $providers = $this->configuration()['providers'] ?? [];
            if (($providers['oxidized']['type'] ?? '') !== 'oxidized') throw new RuntimeException('Configure the Oxidized source first.');
            $found = false;
            foreach ($config['sources'] as &$source) {
                if (($source['provider'] ?? '') === 'oxidized') {
                    $source['parsers'] = array_values($parsers);
                    $found = true;
                }
            }
            unset($source);
            if (! $found) {
                if (! $parsers) return;
                $native = collect($config['sources'])->first(fn ($source) => ($providers[$source['provider']]['type'] ?? '') === 'librenms');
                $config['sources'][] = ['provider' => 'oxidized', 'selectors' => $native['selectors'] ?? [], 'parsers' => array_values($parsers)];
            }
            $this->saveConfigurationBatch([$relative => $this->dumpConfig($document)], $before, structural: true);
        });
    }

    /** Only catalog edits use selective invalidation; other edits retain all existing freshness checks. */
    private function parserChangeImpact(string $candidate): ?array
    {
        [$before] = $this->rawConfiguration();
        [$after] = $this->rawConfiguration($candidate);
        unset($before['oxidized_parsers'], $after['oxidized_parsers']);
        $sortMappings = static function (array $value) use (&$sortMappings): array {
            foreach ($value as &$item) if (is_array($item)) $item = $sortMappings($item);
            unset($item);
            if (! array_is_list($value)) ksort($value);
            return $value;
        };
        if ($sortMappings($before) !== $sortMappings($after)) return null;
        $this->validatePath($candidate.'/libremapper.yaml', structural: true);
        $old = collect($this->parserCatalog()['items'])->keyBy('id');
        $new = collect($this->parserCatalog($candidate.'/libremapper.yaml')['items'])->keyBy('id');
        $maps = [];
        foreach (array_unique([...$old->keys()->all(), ...$new->keys()->all()]) as $id) {
            $a = $old->get($id);
            $b = $new->get($id);
            if (($a['definition'] ?? null) !== ($b['definition'] ?? null)) {
                $maps = [...$maps, ...($a['maps'] ?? []), ...($b['maps'] ?? [])];
            }
        }
        return array_values(array_unique($maps));
    }

    public function configureOxidized(string $mode, string $repository, string $ref, string $privateKey = '', ?array $parsers = null, ?string $sha256 = null): void
    {
        if (! in_array($mode, ['disabled', 'gitea', 'local'], true)) {
            throw new RuntimeException('Choose Disabled, Remote Git, or Local Oxidized.');
        }
        $this->locked(function () use ($mode, $repository, $ref, $privateKey, $parsers, $sha256): void {
            if ($sha256 !== null) $this->assertCurrentHash($this->configPath(), $sha256, false);
            $before = $this->treeFingerprint();
            [$main, $fragments] = $this->rawConfiguration();
            $providers = is_array($main['providers'] ?? null) ? $main['providers'] : [];
            if (isset($providers['oxidized']) && ($providers['oxidized']['type'] ?? null) !== 'oxidized') {
                throw new RuntimeException('Provider name oxidized is already used by an advanced provider.');
            }
            $currentSettings = is_array($providers['oxidized']['settings'] ?? null) ? $providers['oxidized']['settings'] : [];
            $credentialBackups = [];
            $changes = [];
            try {
                if ($mode === 'disabled') {
                    foreach ($this->parserCatalog()['selections'] as $sources) {
                        if ($sources['oxidized'] ?? []) throw new RuntimeException('Disable all Oxidized parsers on every map before disconnecting the source.');
                    }
                    $strip = static function (array $map): array {
                        $map['sources'] = array_values(array_filter($map['sources'] ?? [], fn ($source) => ($source['provider'] ?? '') !== 'oxidized'));
                        return $map;
                    };
                    $main['maps'] = array_map($strip, $main['maps'] ?? []);
                    foreach ($fragments as $key => $map) {
                        $updated = $strip($map);
                        if ($map !== $updated) $changes[$this->mapFragmentPath($main, $key)] = $this->dumpConfig($updated);
                    }
                    unset($providers['oxidized']);
                } else {
                    if ($parsers !== null) {
                        $known = array_column($this->parserCatalog()['items'], 'id');
                        if (! $parsers || ! array_is_list($parsers)
                            || count(array_filter($parsers, 'is_string')) !== count($parsers)
                            || count(array_unique($parsers)) !== count($parsers) || array_diff($parsers, $known)) {
                            throw new RuntimeException('Choose at least one supported Oxidized parser without duplicates.');
                        }
                        $currentSettings['parsers'] = array_values($parsers);
                    }
                    $ref = trim($ref) ?: 'HEAD';
                    $settings = $mode === 'gitea'
                        ? $this->oxidizedGitSettings(trim($repository), $ref, trim($privateKey), $currentSettings, $credentialBackups)
                        : ['repository' => '/oxidized.git', 'ref' => $ref];
                    $settings = array_replace(array_diff_key($currentSettings, array_flip(['repository', 'ref', 'private_key', 'known_hosts', 'accept_new_host_key'])), $settings);
                    $providers['oxidized'] = ['type' => 'oxidized', 'settings' => $settings];
                }
                $main['providers'] = $providers;
                $changes['libremapper.yaml'] = $this->dumpConfig($main);
                if ($mode !== 'disabled') {
                    $candidate = $this->newCandidate();
                    try {
                        $this->copyTree($this->configRoot, $candidate);
                        $this->atomicWrite($candidate.'/libremapper.yaml', $changes['libremapper.yaml']);
                        $this->validatePath($candidate.'/libremapper.yaml', structural: true);
                        $this->execute(['operation' => 'test-source', 'provider' => 'oxidized', 'config' => $candidate.'/libremapper.yaml']);
                    } finally {
                        $this->removeTree($candidate);
                    }
                }
                $this->saveConfigurationBatch($changes, $before, structural: true);
            } catch (Throwable $error) {
                foreach ($credentialBackups as $path => $contents) {
                    if ($contents === null) @unlink($path);
                    else $this->atomicWrite($path, $contents, 0600);
                }
                throw $error;
            }
        });
    }

    /** @param array<string, mixed> $current @param array<string, ?string> $backups @return array<string, mixed> */
    private function oxidizedGitSettings(
        string $repository,
        string $ref,
        string $privateKey,
        array $current,
        array &$backups
    ): array {
        if ($repository === '') {
            throw new RuntimeException('Enter the remote repository SSH URL.');
        }
        $directory = $this->dataRoot.'/oxidized';
        $keyPath = $directory.'/id_git_fetch';
        $knownHostsPath = $directory.'/known_hosts';
        $currentKey = is_string($current['private_key'] ?? null) ? $current['private_key'] : '';
        $currentKnownHosts = is_string($current['known_hosts'] ?? null) ? $current['known_hosts'] : '';
        $managed = $privateKey !== ''
            || ($currentKey === $keyPath && $currentKnownHosts === $knownHostsPath)
            || ($currentKey === '' && is_file($keyPath) && is_file($knownHostsPath));

        if (! $managed) {
            if ($currentKey === '' || $currentKnownHosts === '') {
                throw new RuntimeException('Paste the read-only repository private key.');
            }

            return [
                'repository' => $repository,
                'ref' => $ref,
                'private_key' => $currentKey,
                'known_hosts' => $currentKnownHosts,
                'accept_new_host_key' => (bool) ($current['accept_new_host_key'] ?? false),
            ];
        }

        if (is_link($directory) || is_link($keyPath) || is_link($knownHostsPath)) {
            throw new RuntimeException('Oxidized credential paths must not be symbolic links.');
        }
        $this->ensureDirectory($directory);
        if (! chmod($directory, 0700)) {
            throw new RuntimeException("Unable to protect credential directory $directory.");
        }
        if ($privateKey !== '') {
            if (strlen($privateKey) > 65536 || str_contains($privateKey, "\0")
                || preg_match('/\A-----BEGIN ((?:[A-Z0-9]+ )?PRIVATE KEY)-----\R.+\R-----END \1-----\z/s', $privateKey) !== 1) {
                throw new RuntimeException('Paste a valid SSH private key in PEM or OpenSSH format.');
            }
            $storedKey = is_file($keyPath) ? file_get_contents($keyPath) : null;
            if ($storedKey === false) {
                throw new RuntimeException("Unable to read existing private key $keyPath.");
            }
            $backups[$keyPath] = $storedKey;
            $this->atomicWrite($keyPath, $privateKey."\n", 0600);
        } elseif (! is_file($keyPath)) {
            throw new RuntimeException('Paste the read-only repository private key.');
        }
        $storedKnownHosts = is_file($knownHostsPath) ? file_get_contents($knownHostsPath) : null;
        if ($storedKnownHosts === false) {
            throw new RuntimeException("Unable to read existing host keys $knownHostsPath.");
        }
        $backups[$knownHostsPath] = $storedKnownHosts;
        if (! is_file($knownHostsPath)) {
            $this->atomicWrite($knownHostsPath, '', 0600);
        }
        if (! chmod($keyPath, 0600) || ! chmod($knownHostsPath, 0600)) {
            throw new RuntimeException('Unable to protect the stored Oxidized Git credentials.');
        }

        return [
            'repository' => $repository,
            'ref' => $ref,
            'private_key' => $keyPath,
            'known_hosts' => $knownHostsPath,
            'accept_new_host_key' => true,
        ];
    }

    /** @return array{path: string, content: string, sha256: string, key: string} */
    public function guidedFile(string $key, array $input): array
    {
        [$main, $fragments] = $this->rawConfiguration();
        $relative = 'libremapper.yaml';
        $document = $main;
        $mapIndex = null;
        $isNew = $key === '__new__';
        if ($isNew) {
            $directory = $main['map_directory'] ?? null;
            if (! is_string($directory)) {
                throw new RuntimeException('Guided map creation requires map_directory; configure it in Files.');
            }
            $name = is_string($input['name'] ?? null) ? trim($input['name']) : '';
            $newKey = is_string($input['key'] ?? null) ? trim($input['key']) : '';
            if ($newKey === '') {
                $base = trim((string) preg_replace('/[^a-z0-9]+/', '-', strtolower($name)), '-') ?: 'map';
                $newKey = $base;
                for ($suffix = 2; in_array($newKey, $this->mapKeys(), true); $suffix++) {
                    $newKey = $base.'-'.$suffix;
                }
            }
            if (preg_match('/^[a-z0-9]+(?:-[a-z0-9]+)*$/', $newKey) !== 1) {
                throw new RuntimeException('Map key must be a safe lowercase name using letters, numbers, and single hyphens.');
            }
            foreach (array_merge($main['maps'] ?? [], array_values($fragments)) as $existing) {
                if (is_array($existing) && $this->mapKey($existing) === $newKey) {
                    throw new RuntimeException("Map key already exists: $newKey");
                }
            }
            $name = is_string($input['name'] ?? null) ? trim($input['name']) : '';
            if ($name === '') {
                throw new RuntimeException('Map name is required.');
            }
            $available = array_keys(array_filter($main['providers'] ?? [],
                fn ($item): bool => ($item['type'] ?? null) === 'librenms'));
            $defaultProvider = in_array('librenms', $available, true) ? 'librenms'
                : (count($available) === 1 ? $available[0] : '');
            $provider = is_string($input['provider'] ?? null) ? $input['provider'] : $defaultProvider;
            if ((($main['providers'] ?? [])[$provider]['type'] ?? null) !== 'librenms') {
                throw new RuntimeException('Select a configured LibreNMS provider.');
            }
            $selectors = array_key_exists('rules', $input)
                ? $this->workspaceSelectors($input['rules'], []) : $this->inventorySelectors($input);

            $key = $newKey;
            $input['name'] = $name;
            $relative = $directory.'/'.$key.'.yaml';
            $map = $this->newMapDefaults($key, $name, [['provider' => $provider, 'selectors' => $selectors]]);
        } else {
            foreach ($main['maps'] ?? [] as $index => $candidate) {
                if (is_array($candidate) && $this->mapKey($candidate) === $key) {
                    $mapIndex = $index;
                    break;
                }
            }
            if ($mapIndex === null) {
                $fragment = $fragments[$key] ?? null;
                if (! is_array($fragment)) {
                    throw new RuntimeException("Unknown map: $key");
                }
                $relative = $this->mapFragmentPath($main, $key);
                $map = $fragment;
            } else {
                $map = $main['maps'][$mapIndex];
            }
            $loadedMap = $map;
            $loadedMap['key'] ??= $key;
            if (isset($input['map_hash']) && ! hash_equals(hash('sha256', json_encode($loadedMap, JSON_THROW_ON_ERROR)), (string) $input['map_hash'])) {
                throw new RuntimeException('Draft changed since this page loaded. Reload before saving.');
            }
        }

        if (!$isNew && isset($input['name']) && $input['name'] !== $map['name']) {
            $map['key'] ??= $key;
        }
        foreach (['name', 'menu_group', 'width', 'height', 'node_label'] as $field) {
            if (array_key_exists($field, $input)) {
                $map[$field] = $input[$field];
            }
        }
        foreach (['node_align', 'edge_separation', 'short_hostname_parts'] as $field) {
            if (array_key_exists($field, $input)) {
                if ($field === 'short_hostname_parts' && filter_var($input[$field], FILTER_VALIDATE_INT, ['options' => ['min_range' => 1, 'max_range' => 127]]) === false) {
                    throw new RuntimeException('Short hostname components must be a whole number from 1 to 127.');
                }
                $map[$field] = (int) $input[$field];
            }
        }
        if ($isNew || array_key_exists('scheduled', $input)) {
            $map['scheduled'] = $isNew ? false : (bool) $input['scheduled'];
        }
        $updates = $this->typedFields($input, [
            'type' => 'string', 'width' => 'int', 'height' => 'int', 'margin' => 'int',
            'layer_spacing' => 'int', 'node_spacing' => 'int', 'radius' => 'int',
            'center_layer' => 'nullable-int', 'separation' => 'string', 'auto_grow' => 'bool', 'infer_layers' => 'bool',
        ], 'layout_');
        if ($updates) {
            $map['layout'] = array_replace($map['layout'] ?? [], $updates);
        }
        $updates = $this->typedFields($input, [
            'style' => 'string', 'image' => 'string', 'size' => 'int', 'text_size' => 'int',
            'text_colour' => 'string', 'colour_bg' => 'string', 'colour_bdr' => 'string',
        ], 'node_');
        if ($updates) {
            $map['node_defaults'] = array_replace($map['node_defaults'] ?? [], $updates);
        }
        $updates = $this->typedFields($input, [
            'style' => 'string', 'showpct' => 'bool', 'showbps' => 'bool', 'text_size' => 'int',
            'text_colour' => 'string',
        ], 'edge_');
        if ($updates) {
            $map['edge_defaults'] = array_replace($map['edge_defaults'] ?? [], $updates);
        }
        if (array_key_exists('layer_overrides_yaml', $input)) {
            $map['layer_overrides'] = $this->yamlList((string) $input['layer_overrides_yaml'], 'Layer overrides');
        }
        $providers = $main['providers'] ?? [];
        $oldSelectors = null;
        $newSelectors = null;
        foreach ($map['sources'] ?? [] as $sourceIndex => $source) {
            if (! is_array($source) || ($providers[$source['provider']]['type'] ?? null) !== 'librenms') {
                continue;
            }
            $oldSelectors = $source['selectors'] ?? [];
            if (array_key_exists('rules', $input)) {
                $map['sources'][$sourceIndex]['selectors'] = $this->workspaceSelectors($input['rules'], $oldSelectors);
            }
            if (array_key_exists('device_action', $input)) {
                $id = filter_var($input['device_id'] ?? null, FILTER_VALIDATE_INT);
                $known = array_column($this->inventory()['devices'] ?? [], 'device_id');
                if (! $id || ! in_array($id, $known, true)
                    || ! in_array($input['device_action'], ['include', 'exclude', 'automatic'], true)) {
                    throw new RuntimeException('Choose a current LibreNMS device and a valid inclusion action.');
                }
                $map['sources'][$sourceIndex]['selectors'] = array_values(array_filter(
                    $map['sources'][$sourceIndex]['selectors'],
                    fn ($rule): bool => ($rule['type'] ?? '') !== 'device_id' || (int) $rule['value'] !== $id
                ));
                $map['device_exclusions'] = array_values(array_diff($map['device_exclusions'] ?? [], [$id]));
                if ($input['device_action'] === 'include') {
                    $map['sources'][$sourceIndex]['selectors'][] = ['type' => 'device_id', 'value' => $id];
                } elseif ($input['device_action'] === 'exclude') {
                    $map['device_exclusions'][] = $id;
                }
            }
            if (array_key_exists('selectors_yaml', $input)) {
                $map['sources'][$sourceIndex]['selectors'] = $this->yamlList((string) $input['selectors_yaml'], 'Selectors');
            }
            if (array_key_exists('protocols', $input)) {
                $map['sources'][$sourceIndex]['protocols'] = array_values(array_filter(array_map(
                    'trim', explode(',', (string) $input['protocols'])
                )));
            }
            $newSelectors = $map['sources'][$sourceIndex]['selectors'];
            break;
        }
        if (array_key_exists('placement_device_ids', $input) || array_key_exists('placement_manual_device_ids', $input) || array_key_exists('placement_action', $input) || array_key_exists('placement_device_id', $input)) {
            $ids = $input['placement_device_ids'] ?? (isset($input['placement_device_id']) ? [$input['placement_device_id']] : []);
            $manualIds = $input['placement_manual_device_ids'] ?? [];
            if (! is_array($ids) || ! array_is_list($ids) || ! is_array($manualIds) || ! array_is_list($manualIds) || (! $ids && ! $manualIds)) {
                throw new RuntimeException('Choose at least one current LibreNMS device or manual device for the layer.');
            }
            $known = $ids ? array_column($this->inventory()['devices'] ?? [], 'device_id') : [];
            foreach ($ids as &$id) {
                $id = filter_var($id, FILTER_VALIDATE_INT);
                if (! $id || ! in_array($id, $known, true)) {
                    throw new RuntimeException('Choose a current device and placement.');
                }
            }
            unset($id);
            $ids = array_values(array_unique($ids));
            $manualTargets = [];
            if ($manualIds) {
                if (! is_string($main['manual_provider'] ?? null)) throw new RuntimeException('Choose a configured manual device for the layer.');
                $catalog = $this->manualCatalog();
                $this->assertManualFreshness($key, $catalog['path'], $input);
                foreach ($manualIds as $id) {
                    if (! is_string($id) || ! in_array($id, array_column($catalog['devices'], 'source_id'), true)) {
                        throw new RuntimeException('Choose a current manual device for the layer.');
                    }
                    $manualTargets[] = $main['manual_provider'].':'.$id;
                }
                $manualTargets = array_values(array_unique($manualTargets));
            }
            $placement = $input['placement'] ?? '';
            if (! is_int($placement) && ! is_string($placement)) {
                throw new RuntimeException('Layer value must be blank or a whole number.');
            }
            $placement = is_string($placement) ? trim($placement) : $placement;
            $placement = in_array($placement, ['', 'auto'], true) ? null : filter_var($placement, FILTER_VALIDATE_INT);
            if ($placement === false) throw new RuntimeException('Layer value must be blank or a whole number.');
            $map['layer_overrides'] = array_values(array_filter($map['layer_overrides'] ?? [],
                fn ($rule): bool => !(($rule['type'] ?? '') === 'device_id' && in_array((int) $rule['value'], $ids, true))
                    && !(($rule['type'] ?? '') === 'canonical' && in_array($rule['value'], $manualTargets, true))));
            if ($placement !== null) {
                $map['layer_overrides'] = [
                    ...array_map(fn ($id) => ['type' => 'device_id', 'value' => $id, 'layer' => $placement], $ids),
                    ...array_map(fn ($id) => ['type' => 'canonical', 'value' => $id, 'layer' => $placement], $manualTargets),
                    ...$map['layer_overrides'],
                ];
            }
        }
        if (array_key_exists('hostname_device_ids', $input) || array_key_exists('hostname_action', $input) || array_key_exists('hostname_device_id', $input)) {
            $ids = $input['hostname_device_ids'] ?? (isset($input['hostname_device_id']) ? [$input['hostname_device_id']] : []);
            if (! is_array($ids) || ! $ids) {
                throw new RuntimeException('Choose at least one current LibreNMS device for the hostname label.');
            }
            $known = array_column($this->inventory()['devices'] ?? [], 'device_id');
            foreach ($ids as &$id) {
                $id = filter_var($id, FILTER_VALIDATE_INT);
                if (! $id || ! in_array($id, $known, true)) {
                    throw new RuntimeException('Choose a current LibreNMS device for the hostname label.');
                }
            }
            unset($id);
            $ids = array_values(array_unique($ids));
            $parts = $input['hostname_parts'] ?? '';
            if (! is_int($parts) && ! is_string($parts)) {
                throw new RuntimeException('Short hostname components must be blank or a whole number from 1 to 127.');
            }
            $parts = is_string($parts) ? trim($parts) : $parts;
            $parts = $parts === '' ? null : filter_var($parts, FILTER_VALIDATE_INT, ['options' => ['min_range' => 1, 'max_range' => 127]]);
            if ($parts === false) {
                throw new RuntimeException('Short hostname components must be blank or a whole number from 1 to 127.');
            }
            $map['short_hostname_overrides'] = array_values(array_filter($map['short_hostname_overrides'] ?? [],
                fn ($override): bool => ! in_array((int) ($override['device_id'] ?? 0), $ids, true)));
            if ($parts !== null) {
                foreach ($ids as $id) {
                    $map['short_hostname_overrides'][] = ['device_id' => $id, 'parts' => $parts];
                }
            }
        }
        if ($oldSelectors !== $newSelectors) {
            foreach ($map['sources'] as &$otherSource) {
                if (($providers[$otherSource['provider']]['type'] ?? '') === 'oxidized'
                    && ($otherSource['selectors'] ?? []) === $oldSelectors) {
                    $otherSource['selectors'] = $newSelectors;
                }
            }
            unset($otherSource);
        }
        $hasOxidized = collect($map['sources'] ?? [])->contains(
            fn ($source): bool => ($source['provider'] ?? '') === 'oxidized');
        if ($isNew || (array_key_exists('include_oxidized', $input) && (bool) $input['include_oxidized'] !== $hasOxidized)) {
        $map = $this->synchronizeOxidizedSource(
            $map,
            $providers,
            (bool) ($input['include_oxidized'] ?? (($providers['oxidized']['type'] ?? '') === 'oxidized'))
        );
        }

        if ($isNew && (bool) ($input['include_oxidized'] ?? true)) {
            foreach ($providers as $providerName => $providerConfig) {
                if (($providerConfig['type'] ?? '') === 'oxidized' && $providerName !== 'oxidized') {
                    $map['sources'][] = ['provider' => $providerName, 'selectors' => $newSelectors];
                }
            }
        }
        if ($mapIndex === null) {
            $document = $map;
        } else {
            $document['maps'][$mapIndex] = $map;
        }
        $path = $this->configRoot.'/'.$relative;

        return [
            'path' => $relative,
            'content' => $this->dumpConfig($document),
            'sha256' => $isNew ? '' : (hash_file('sha256', $path) ?: ''),
            'key' => $key,
        ];
    }

    public function reset(string $confirmation): string
    {
        if ($confirmation !== 'RESET') {
            throw new RuntimeException('Type RESET to confirm restoring defaults.');
        }

        return $this->locked(function (): string {
            $this->requireConfigRoot();
            $this->ensureDirectory($this->dataRoot.'/backups');
            $backup = $this->dataRoot.'/backups/'.gmdate('Ymd-His');
            if (file_exists($backup)) {
                $backup .= '-'.bin2hex(random_bytes(3));
            }
            $this->ensureDirectory($backup);
            $this->copyRawTree($this->configRoot, $backup);
            $candidate = $this->newCandidate();
            try {
                $this->writeDefaults($candidate);
                $this->validatePath($candidate.'/libremapper.yaml');
                try {
                    $this->clearTree($this->configRoot);
                    $this->copyTree($candidate, $this->configRoot);
                } catch (Throwable $error) {
                    $this->clearTree($this->configRoot);
                    $this->copyRawTree($backup, $this->configRoot);
                    throw $error;
                }
            } finally {
                $this->removeTree($candidate);
            }

            return $backup;
        });
    }

    /** @return array<string, mixed> */
    public function inventory(): array
    {
        $result = $this->execute(['operation' => 'inventory', 'config' => $this->configPath()]);
        $inventory = json_decode($result['stdout'], true, flags: JSON_THROW_ON_ERROR);
        if (! is_array($inventory)
            || ! is_array($inventory['groups'] ?? null)
            || ! is_array($inventory['locations'] ?? null)) {
            throw new RuntimeException('Libremapper sidecar returned invalid inventory.');
        }
        $inventory['groups'] = array_values(array_filter(
            $inventory['groups'],
            fn ($group): bool => is_array($group)
                && is_string($group['name'] ?? null)
                && $group['name'] !== ''
                && is_int($group['device_count'] ?? null)
                && $group['device_count'] > 0
        ));
        $inventory['locations'] = array_values(array_filter(
            $inventory['locations'],
            fn ($location): bool => is_array($location)
                && is_string($location['location'] ?? null)
                && $location['location'] !== ''
                && is_int($location['device_count'] ?? null)
                && $location['device_count'] > 0
        ));

        return $inventory;
    }

    /** @return array<string, mixed> */
    public function providerInspection(string $provider, ?string $map = null): array
    {
        $result = $this->execute([
            'operation' => 'inspect',
            'config' => $this->configPath(),
            'provider' => $provider,
            ...($map === null ? [] : ['map' => $map]),
        ]);
        $inspection = json_decode($result['stdout'], true, flags: JSON_THROW_ON_ERROR);
        if (! is_array($inspection)
            || ! is_string($inspection['provider'] ?? null)
            || ! is_string($inspection['provider_type'] ?? null)
            || ! is_bool($inspection['blocked'] ?? null)
            || ! is_array($inspection['matched_files'] ?? null)
            || ! is_array($inspection['matched_rules'] ?? null)
            || ! is_array($inspection['skipped_files'] ?? null)
            || ! is_array($inspection['resolution_failures'] ?? null)
            || ! is_array($inspection['warnings'] ?? null)
            || ! is_array($inspection['topology'] ?? null)
            || ! is_array($inspection['topology']['devices'] ?? null)
            || ! is_array($inspection['topology']['links'] ?? null)) {
            throw new RuntimeException('Libremapper sidecar returned an invalid provider inspection.');
        }

        return $inspection;
    }

    /** @return array<string, mixed> */
    public function plan(string $map, bool $adopt = false, bool $relayout = false): array
    {
        return $this->locked(fn (): array => $this->planUnlocked($map, $adopt, $relayout));
    }

    /** @return array<string, mixed> */
    private function planUnlocked(string $map, bool $adopt, bool $relayout): array
    {
        $this->validatePath($this->configPath(), $map);
        if (! in_array($map, $this->mapKeys(), true)) {
            throw new RuntimeException("Unknown map: $map");
        }
        $this->ensureDirectory($this->dataRoot.'/plans');
        $path = $this->planPath($map);
        $temporary = $path.'.'.bin2hex(random_bytes(8)).'.tmp';
        try {
            $result = $this->execute([
                'operation' => 'plan',
                'map' => $map,
                'config' => $this->configPath(),
                'output' => $temporary,
                'adopt' => $adopt,
                'relayout' => $relayout,
            ]);
            $plan = json_decode((string) file_get_contents($temporary), true, flags: JSON_THROW_ON_ERROR);
            if (! is_array($plan)) {
                throw new RuntimeException('The Python engine returned an invalid plan.');
            }
            if (! rename($temporary, $path)) {
                throw new RuntimeException('Unable to save the reviewed plan.');
            }

            return $this->presentPlan($plan, $result) + ['plan_sha256' => hash_file('sha256', $path)];
        } finally {
            @unlink($temporary);
        }
    }

    /** @return array<string, mixed> */
    public function reviewedPlan(string $map): array
    {
        $path = $this->planPath($map);
        if (! is_file($path)) {
            throw new RuntimeException("No reviewed plan exists for map $map.");
        }
        $contents = (string) file_get_contents($path);
        $plan = json_decode($contents, true, flags: JSON_THROW_ON_ERROR);

        return $this->presentPlan($plan, []) + ['plan_sha256' => hash('sha256', $contents)];
    }

    /** @return array<string, mixed> */
    public function apply(string $map, ?string $expectedPlan = null): array
    {
        return $this->locked(function () use ($map, $expectedPlan): array {
            $this->validatePath($this->configPath(), $map);
            $plan = $this->reviewedPlan($map);
            if ($expectedPlan !== null && ! hash_equals($plan['plan_sha256'], $expectedPlan)) {
                throw new RuntimeException('Reviewed plan changed. Review the current plan before publishing.');
            }
            if ($plan['conflicts']) {
                throw new RuntimeException('Apply is blocked because the reviewed plan has conflicts.');
            }

            return array_replace($plan, $this->execute([
                'operation' => 'apply',
                'plan' => $this->planPath($map),
                'config' => $this->configPath(),
            ]));
        });
    }

    public function reviewMap(Plugin $plugin, string $map, bool $adopt = false, bool $relayout = false): array
    {
        return $this->locked(function () use ($plugin, $map, $adopt, $relayout): array {
            $plan = $this->planUnlocked($map, $adopt, $relayout);
            $this->recordRun($plugin, $map, 'planned', $plan);
            return $plan;
        });
    }

    public function publishMap(Plugin $plugin, string $map, string $expectedPlan, bool $confirmWarnings): array
    {
        return $this->locked(function () use ($plugin, $map, $expectedPlan, $confirmWarnings): array {
            $plan = $this->reviewedPlan($map);
            if (! hash_equals($plan['plan_sha256'], $expectedPlan)) throw new RuntimeException('Reviewed plan changed. Review the current plan before publishing.');
            if ($plan['warnings'] && ! $confirmWarnings) throw new RuntimeException('Confirm the reviewed warnings before applying.');
            $result = $this->apply($map, $expectedPlan);
            $this->updatePluginSettings($plugin, function (array $settings) use ($map): array {
                $settings['initialized_maps'] = array_values(array_unique([...$settings['initialized_maps'], $map]));
                return $settings;
            });
            $this->recordRun($plugin, $map, 'applied', $result);
            try {
                $fresh = $this->planUnlocked($map, false, false);
                $this->recordRun($plugin, $map, 'applied', $result + ['librenms_map_id' => collect($fresh['operations'])->firstWhere('kind', 'map')['librenms_id'] ?? null]);
                return ['published' => true, 'status_refreshed' => true, 'remaining_changes' => $fresh['changes']] + $result;
            } catch (Throwable) {
                return ['published' => true, 'status_refreshed' => false, 'remaining_changes' => null] + $result;
            }
        });
    }

    private function recordRun(Plugin $plugin, string $map, string $status, array $result): void
    {
        $this->updatePluginSettings($plugin, function (array $settings) use ($map, $status, $result): array {
            $settings['last_runs'][$map] = [
                'status' => $status, 'at' => now()->toIso8601String(),
                'changes' => $result['changes'] ?? null, 'warnings' => count($result['warnings'] ?? []),
                'conflicts' => count($result['conflicts'] ?? []), 'plan_sha256' => $result['plan_sha256'] ?? null,
                'librenms_map_id' => $result['librenms_map_id'] ?? collect($result['operations'] ?? [])->firstWhere('kind', 'map')['librenms_id'] ?? $settings['last_runs'][$map]['librenms_map_id'] ?? null,
            ];
            return $settings;
        });
    }

    private function updatePluginSettings(Plugin $plugin, callable $update): void
    {
        \Illuminate\Support\Facades\DB::transaction(function () use ($plugin, $update): void {
            $locked = $plugin->newQuery()->whereKey($plugin->getKey())->lockForUpdate()->firstOrFail();
            $locked->settings = $update(self::settings((array) $locked->settings));
            if ($locked->isDirty('settings')) $locked->saveOrFail();
        });
    }

    public function mapConfiguration(string $key): array
    {
        $map = collect($this->configuration()['maps'])->first(fn ($map) => $this->mapKey($map) === $key);
        if (! $map) throw new RuntimeException('Unknown map.');
        $map['key'] ??= $key;
        return $map;
    }

    public function saveMap(string $key, array $patch, ?string $mapHash): void
    {
        $this->locked(function () use ($key, $patch, $mapHash): void {
            if (! preg_match('/^[a-z0-9]+(?:-[a-z0-9]+)*$/', $key)) throw new RuntimeException('Choose a safe map key.');
            $schema = json_decode(file_get_contents(__DIR__.'/../resources/mcp-map-schema.json'), true, flags: JSON_THROW_ON_ERROR);
            if (array_diff(array_keys($patch), array_keys($schema['properties']))) throw new RuntimeException('Unknown map fields.');
            if (array_key_exists('name', $patch)) {
                $patch['name'] = trim($patch['name']);
                if ($patch['name'] === '') throw new RuntimeException('Map name is required.');
            }
            $before = $this->treeFingerprint();
            if ($mapHash === null) {
                if (in_array($key, $this->mapKeys(), true)) throw new RuntimeException('Map key already exists.');
                [$main] = $this->rawConfiguration();
                if (! is_string($main['map_directory'] ?? null)) throw new RuntimeException('Map creation requires map_directory.');
                $path = $main['map_directory'].'/'.$key.'.yaml';
                if (file_exists($this->configRoot.'/'.$path)) throw new RuntimeException('Map file already exists.');
                $document = $this->newMapDefaults($key, $patch['name'] ?? '', $patch['sources'] ?? []);
                $index = null;
            } else {
                [$path, $document, $index] = $this->mapDocument($key, $mapHash);
            }
            if ($index === null) $map = &$document; else $map = &$document['maps'][$index];
            $map = self::mergeMapPatch($map, $patch);
            $map['key'] = $key;
            $this->saveConfigurationBatch([$this->safeRelativePath($path) => $this->dumpConfig($document)], $before);
        });
    }

    private function newMapDefaults(string $key, string $name, array $sources): array
    {
        return ['key' => $key, 'name' => $name, 'scheduled' => false, 'node_label' => 'sys_name',
            'manual_link_exclusions' => [], 'sources' => $sources,
            'layout' => ['type' => 'auto', 'infer_layers' => true, 'auto_grow' => true, 'separation' => 'constrained']];
    }

    private static function mergeMapPatch(array $original, array $patch): array
    {
        foreach ($patch as $key => $value) {
            $objects = ['layout', 'identity', 'node_defaults', 'edge_defaults', 'logical_edge_defaults', 'aliases', 'layer_margins'];
            $original[$key] = is_array($value) && (in_array($key, $objects, true) || ! array_is_list($value))
                ? self::mergeMapPatch($original[$key] ?? [], $value) : $value;
        }
        return $original;
    }

    public function addManualLink(string $map, string $left, string $right, array $input): void
    {
        $nodes = [];
        foreach ($this->reviewedPlan($map)['operations'] as $operation) {
            if (($operation['kind'] ?? '') === 'node' && ($operation['action'] ?? '') !== 'delete') {
                $nodes[$operation['canonical_id']] = $operation['generated_values']['device_id'] ?? null;
            }
        }
        if (! isset($nodes[$left], $nodes[$right])) {
            throw new RuntimeException('Choose two LibreNMS devices from the reviewed preview.');
        }
        $this->saveManualLink($map, ['a_device_id' => $nodes[$left], 'b_device_id' => $nodes[$right]] + $input);
    }

    public function saveManualLink(string $map, array $input): string
    {
        return $this->locked(function () use ($map, $input): string {
            $before = $this->treeFingerprint();
            [$main] = $this->rawConfiguration();
            [$relative, $topology, $mainChanged] = $this->guidedManualTopology($main, true);
            $this->assertManualFreshness($map, $relative, $input);
            $inventory = collect($this->inventory()['devices'] ?? [])->keyBy('device_id');
            $editing = (string) ($input['source_id'] ?? '');
            $existing = collect($topology['links'])->firstWhere('source_id', $editing);
            if ($editing !== '' && ! $existing) {
                throw new RuntimeException('The manual link no longer exists.');
            }
            $endpoints = [];
            $associatedDevices = [];
            $createdDevices = [];
            foreach (['a', 'b'] as $side) {
                $choice = (string) ($input[$side.'_device_id'] ?? '');
                $reference = false;
                if ($choice === 'unknown') {
                    $label = trim((string) ($input[$side.'_remote_label'] ?? ''));
                    if ($label === '' || strlen($label) > 255) {
                        throw new RuntimeException('Name the unknown remote endpoint (up to 255 characters).');
                    }
                    $sourceId = 'guided-remote:'.substr(hash('sha256', strtolower($label)), 0, 16);
                    $device = ['source_id' => $sourceId, 'hostname' => $label,
                        'attributes' => ['libremapper_guided' => true]];
                } elseif (str_starts_with($choice, 'manual:')) {
                    $sourceId = substr($choice, 7);
                    $device = collect($topology['devices'])->firstWhere('source_id', $sourceId);
                    if (! $device) throw new RuntimeException('Choose a current manual device.');
                } else {
                    $id = filter_var($choice, FILTER_VALIDATE_INT);
                    $record = $inventory->get($id);
                    if (! $id || ! $record) throw new RuntimeException('Choose a current LibreNMS device for each endpoint.');
                    $matches = collect($topology['devices'])->filter(fn ($item) =>
                        in_array($id, self::manualAssociationCandidates($item, [$record]), true));
                    if ($matches->count() > 1) throw new RuntimeException('Ambiguous LibreNMS association; select a specific manual device.');
                    $device = $matches->first();
                    if (! $device) {
                        $reference = true;
                        $sourceId = 'librenms:'.$id;
                        $interfaces = collect($topology['inventory_overrides'][$sourceId]['interfaces'] ?? [])->keyBy('source_id')->all();
                        foreach (array_merge($topology['links'], $topology['suppress_links']) as $link) {
                            foreach (['a', 'b'] as $endpointSide) {
                                $endpoint = $link[$endpointSide] ?? [];
                                if (($endpoint['device'] ?? '') === $sourceId && isset($endpoint['interface_definition'])) {
                                    $interfaces[$endpoint['interface_definition']['source_id']] = $endpoint['interface_definition'];
                                }
                            }
                        }
                        $device = ['source_id' => $sourceId, 'librenms_device_id' => $id,
                            'hostname' => $record['hostname'], 'interfaces' => array_values($interfaces)];
                    }
                    $sourceId = $device['source_id'];
                }
                $index = collect($topology['devices'])->search(fn ($item) => $item['source_id'] === $sourceId);
                $newDevice = $index === false;
                if ($index !== false && $choice === 'unknown') {
                    $existingDevice = $topology['devices'][$index];
                    if (($existingDevice['hostname'] ?? '') !== $label || isset($existingDevice['librenms_device_id'])) {
                        throw new RuntimeException('Remote endpoint identity is already used. Select the existing manual device.');
                    }
                    $device = $existingDevice;
                }
                if ($index === false && ! $reference) {
                    $index = count($topology['devices']);
                    $topology['devices'][] = $device;
                    $createdDevices[] = $sourceId;
                }
                if ($reference && ! collect($topology['links'])->contains(fn ($link) =>
                    ($link['a']['device'] ?? '') === $sourceId || ($link['b']['device'] ?? '') === $sourceId)) {
                    $createdDevices[] = $sourceId;
                }
                $associations = self::manualAssociationCandidates($device, $inventory->all());
                if (count($associations) > 1) throw new RuntimeException('Ambiguous LibreNMS association; select an association in Devices → Manual first.');
                if ($choice === 'unknown' && $associations && $newDevice) throw new RuntimeException('This device matches LibreNMS inventory; select the LibreNMS device instead.');
                $associatedId = $associations[0] ?? 0;
                $associatedDevices[] = $associatedId;
                $endpoint = array_replace($existing[$side] ?? [], ['device' => $sourceId]);
                unset($endpoint['interface'], $endpoint['interface_definition']);
                $mode = (string) ($input[$side.'_interface'] ?? '');
                $interface = null;
                if (str_starts_with($mode, 'file:')) {
                    $interface = collect($device['interfaces'] ?? [])->firstWhere('source_id', substr($mode, 5));
                    if (! $interface) throw new RuntimeException('The file-defined interface no longer exists.');
                    if (isset($interface['librenms_port_id']) && ! ($interface['attributes']['manual_label_only'] ?? false)) {
                        $this->requirePort((int) $interface['librenms_port_id'], $associatedId);
                    }
                } elseif ($mode === 'custom') {
                    $label = trim((string) ($input[$side.'_label'] ?? ''));
                    if ($label === '' || strlen($label) > 255) throw new RuntimeException('Enter a custom interface label (up to 255 characters).');
                    $oldInterface = collect($device['interfaces'] ?? [])->firstWhere('source_id', $existing[$side]['interface'] ?? '');
                    $interface = ['source_id' => 'manual-label:'.bin2hex(random_bytes(8)),
                        'device_source_id' => $sourceId, 'name' => $label,
                        'attributes' => ['manual_label_only' => true]];
                    if ($oldInterface && ($existing[$side]['device'] ?? '') === $sourceId
                        && ($oldInterface['attributes']['manual_label_only'] ?? false)) {
                        $interface = array_replace($oldInterface, ['name' => $label]);
                    }
                } elseif ($mode !== '') {
                    $id = $associatedId;
                    if (! $id) throw new RuntimeException('An unknown endpoint cannot bind a LibreNMS port.');
                    $portId = filter_var($mode, FILTER_VALIDATE_INT, ['options' => ['min_range' => 1]]);
                    if ($portId === false) throw new RuntimeException('Choose a valid selected interface.');
                    $port = $this->requirePort($portId, $id);
                    $interface = ['source_id' => 'librenms-port:'.$port->port_id,
                        'device_source_id' => $sourceId, 'name' => $port->ifName ?: $port->ifDescr,
                        'if_index' => (int) $port->ifIndex, 'librenms_port_id' => (int) $port->port_id];
                }
                if ($interface !== null) {
                    $interfaces = $reference ? $device['interfaces'] : ($topology['devices'][$index]['interfaces'] ?? []);
                    $found = false;
                    foreach ($interfaces as &$item) {
                        if ($item['source_id'] === $interface['source_id']) {
                            $item = array_replace($item, $interface);
                            $found = true;
                        }
                    }
                    unset($item);
                    if (! $found) $interfaces[] = $interface;
                    if ($reference) {
                        $endpoint['interface_definition'] = $interface;
                    } else {
                        $topology['devices'][$index]['interfaces'] = $interfaces;
                        if (isset($interface['librenms_port_id']) && ! ($interface['attributes']['manual_label_only'] ?? false)) $topology['devices'][$index]['librenms_device_id'] = $associatedId;
                    }
                    $endpoint['interface'] = $interface['source_id'];
                }
                $endpoints[] = $endpoint;
            }
            if ($endpoints[0]['device'] === $endpoints[1]['device']
                || ($associatedDevices[0] && $associatedDevices[0] === $associatedDevices[1])) throw new RuntimeException('Choose two different devices.');
            $signature = array_map(fn ($endpoint) => $endpoint['device'].'/'.($endpoint['interface'] ?? ''), $endpoints);
            sort($signature);
            $sourceId = $editing ?: 'manual:'.substr(hash('sha256', implode("\0", $signature)), 0, 16);
            foreach ($topology['links'] as $link) {
                if ($link['source_id'] === $editing) continue;
                $other = array_map(fn ($endpoint) => $endpoint['device'].'/'.($endpoint['interface'] ?? ''), [$link['a'], $link['b']]);
                sort($other);
                if ($other === $signature || $link['source_id'] === $sourceId) throw new RuntimeException('That manual link already exists.');
            }
            $link = array_replace($existing ?? ['source_id' => $sourceId, 'protocol' => 'manual',
                'relationship_kind' => isset($endpoints[0]['interface']) || isset($endpoints[1]['interface']) ? 'direct' : 'logical',
                'attributes' => ['libremapper_guided' => true]], ['a' => $endpoints[0], 'b' => $endpoints[1]]);
            if ($existing) {
                foreach ($topology['links'] as &$item) if ($item['source_id'] === $editing) $item = $link;
                unset($item);
            } else {
                $topology['links'][] = $link;
            }
            $changes = [$relative => $this->dumpConfig($topology, $relative)];
            if ($mainChanged) $changes['libremapper.yaml'] = $this->dumpConfig($main);
            if ($createdDevices) {
                [$mapPath, $document, $mapIndex] = $this->mapDocument($map, $input['map_hash']);
                if ($mapIndex === null) $mapConfig = &$document; else $mapConfig = &$document['maps'][$mapIndex];
                $mapConfig['manual_device_inclusions'] = array_values(array_unique(array_merge($mapConfig['manual_device_inclusions'] ?? [], $createdDevices)));
                $mapConfig['manual_device_exclusions'] = array_values(array_diff($mapConfig['manual_device_exclusions'] ?? [], $createdDevices));
                if ($mainChanged && $mapPath === 'libremapper.yaml') {
                    $document['providers'] = $main['providers'];
                    $document['manual_provider'] = $main['manual_provider'];
                }
                $changes[$mapPath] = $this->dumpConfig($document);
            }
            $this->saveConfigurationBatch($changes, $before);
            return $sourceId;
        });
    }

    public static function manualAssociationCandidates(array $device, array $inventory): array
    {
        if (isset($device['librenms_device_id'])) return [(int) $device['librenms_device_id']];
        $names = array_map(fn ($name) => strtolower(rtrim(trim((string) $name), '.')),
            array_filter([$device['hostname'] ?? null, $device['fqdn'] ?? null, $device['management_ip'] ?? null]));
        return array_values(array_map(fn ($item) => $item['device_id'], array_filter($inventory,
            fn ($item) => in_array(strtolower(rtrim($item['hostname'], '.')), $names, true))));
    }

    private function requirePort(int $portId, int $deviceId): \App\Models\Port
    {
        $query = \App\Models\Port::query()->where('device_id', $deviceId)->where('port_id', $portId)->where('deleted', 0);
        if (auth()->user()) $query->hasAccess(auth()->user());
        $port = $query->first();
        if (! $port) throw new RuntimeException('The selected interface is missing, inaccessible, deleted, or belongs to another device.');
        return $port;
    }

    public function manualCatalog(): array
    {
        [$main] = $this->rawConfiguration();
        [$relative, $topology] = $this->guidedManualTopology($main, true);
        return ['path' => $relative, 'sha256' => hash_file('sha256', $this->configRoot.'/'.$relative), 'devices' => $topology['devices'], 'links' => $topology['links'], 'suppress_links' => $topology['suppress_links']];
    }

    private function assertManualFreshness(string $map, string $relative, array $input): void
    {
        $this->assertCurrentHash($this->configRoot.'/'.$relative, (string) ($input['manual_sha256'] ?? ''), false);
        $this->mapDocument($map, (string) ($input['map_hash'] ?? ''));
    }

    private function mapDocument(string $map, string $hash): array
    {
        [$main, $fragments] = $this->rawConfiguration();
        foreach ($main['maps'] ?? [] as $index => $candidate) {
            if ($this->mapKey($candidate) === $map) {
                $candidate['key'] ??= $map;
                if (! hash_equals(hash('sha256', json_encode($candidate, JSON_THROW_ON_ERROR)), $hash)) throw new RuntimeException('Draft changed since this page loaded. Reload before saving.');
                return ['libremapper.yaml', $main, $index];
            }
        }
        $candidate = $fragments[$map] ?? null;
        if (! $candidate) throw new RuntimeException('Choose a configured map.');
        if (! hash_equals(hash('sha256', json_encode($candidate, JSON_THROW_ON_ERROR)), $hash)) throw new RuntimeException('Draft changed since this page loaded. Reload before saving.');
        return [$this->mapFragmentPath($main, $map), $candidate, null];
    }

    private function mapFragmentPath(array $main, string $key): string
    {
        foreach (['yaml', 'yml'] as $extension) {
            $relative = $main['map_directory'].'/'.$key.'.'.$extension;
            if (is_file($this->configRoot.'/'.$relative)) return $relative;
        }
        throw new RuntimeException('The map fragment no longer exists.');
    }

    public function saveManualDevice(string $map, array $input): string
    {
        return $this->locked(function () use ($map, $input): string {
            $before = $this->treeFingerprint();
            [$main] = $this->rawConfiguration();
            [$relative, $topology, $mainChanged] = $this->guidedManualTopology($main, true);
            $this->assertManualFreshness($map, $relative, $input);
            [$mapPath, $document, $mapIndex] = $this->mapDocument($map, $input['map_hash']);
            $sourceId = (string) ($input['source_id'] ?? '');
            $index = collect($topology['devices'])->search(fn ($item) => $item['source_id'] === $sourceId);
            if ($sourceId !== '' && $index === false) throw new RuntimeException('The manual device no longer exists.');
            $existing = $index === false ? [] : $topology['devices'][$index];
            $name = trim((string) ($input['hostname'] ?? ''));
            if ($name === '' || strlen($name) > 255) throw new RuntimeException('Enter a device name (up to 255 characters).');
            $association = (string) ($input['librenms_device_id'] ?? '');
            $id = $association === '' ? null : filter_var($association, FILTER_VALIDATE_INT, ['options' => ['min_range' => 1]]);
            $inventory = $this->inventory()['devices'];
            if ($association !== '' && (! $id || ! collect($inventory)->contains('device_id', $id))) throw new RuntimeException('Choose a current LibreNMS association.');
            $nextAssociations = self::manualAssociationCandidates(array_replace($existing, ['hostname' => $name, 'librenms_device_id' => $id]), $inventory);
            if (count($nextAssociations) > 1) throw new RuntimeException('Ambiguous LibreNMS association; select a device explicitly.');
            $nextId = $nextAssociations[0] ?? null;
            $oldAssociations = self::manualAssociationCandidates($existing, $inventory);
            $oldId = count($oldAssociations) === 1 ? $oldAssociations[0] : null;
            if ($nextId && ($index === false || $oldId !== $nextId)) throw new RuntimeException('This device matches LibreNMS inventory; select the LibreNMS device when adding connections.');
            if ($oldId !== $nextId) {
                foreach ($existing['interfaces'] ?? [] as $interface) {
                    if (isset($interface['librenms_port_id']) && ! ($interface['attributes']['manual_label_only'] ?? false)) {
                        $this->requirePort((int) $interface['librenms_port_id'], (int) $nextId);
                    }
                }
                // shortcut: clear choices for the old device first; resolve individual edges if shared associations need independent edits.
                if ($oldId) foreach ($this->configuration()['maps'] as $otherMap) {
                    foreach ($otherMap['link_graph_overrides'] ?? [] as $override) {
                        $port = isset($override['port_id']) ? \App\Models\Port::find($override['port_id']) : null;
                        if ($port && (int) $port->device_id === $oldId) throw new RuntimeException('Return traffic interface choices on '.$otherMap['name'].' to Automatic before changing this association.');
                    }
                }
            }
            $parts = ($input['short_hostname_parts'] ?? '') === '' ? null : filter_var($input['short_hostname_parts'], FILTER_VALIDATE_INT, ['options' => ['min_range' => 1, 'max_range' => 127]]);
            $layer = filter_var($input['layer'] ?? 0, FILTER_VALIDATE_INT);
            if ($parts === false || $layer === false) throw new RuntimeException('Enter a valid placement and hostname component count (1–127).');
            $image = (string) ($input['image'] ?? '');
            if ($image !== '' && (basename($image) !== $image || ! in_array(strtolower(pathinfo($image, PATHINFO_EXTENSION)), ['svg', 'png', 'jpg', 'gif'], true))) throw new RuntimeException('Choose a map image filename.');
            $sourceId = $sourceId ?: 'manual-device:'.bin2hex(random_bytes(8));
            $device = array_replace($existing, ['source_id' => $sourceId, 'hostname' => $name,
                'librenms_device_id' => $id, 'layer' => $layer, 'short_hostname_parts' => $parts]);
            $device['attributes']['map_image'] = $image;
            if ($index === false) {
                $topology['devices'][] = $device;
                if ($mapIndex === null) $mapConfig = &$document; else $mapConfig = &$document['maps'][$mapIndex];
                $mapConfig['manual_device_inclusions'][] = $sourceId;
            } else $topology['devices'][$index] = $device;
            $changes = [$relative => $this->dumpConfig($topology, $relative)];
            if ($mainChanged) $changes['libremapper.yaml'] = $this->dumpConfig($main);
            if ($index === false) {
                if ($mainChanged && $mapPath === 'libremapper.yaml') {
                    $document['providers'] = $main['providers'];
                    $document['manual_provider'] = $main['manual_provider'];
                }
                $changes[$mapPath] = $this->dumpConfig($document);
            }
            $this->saveConfigurationBatch($changes, $before);
            return $sourceId;
        });
    }

    public function includeManualDevice(string $map, string $sourceId, bool $included, array $input): void
    {
        $this->locked(function () use ($map, $sourceId, $included, $input): void {
            $before = $this->treeFingerprint();
            $catalog = $this->manualCatalog();
            $this->assertManualFreshness($map, $catalog['path'], $input);
            if (! collect($catalog['devices'])->contains('source_id', $sourceId)) throw new RuntimeException('Choose a current manual device.');
            [$path, $document, $index] = $this->mapDocument($map, $input['map_hash']);
            if ($index === null) $config = &$document; else $config = &$document['maps'][$index];
            foreach (['manual_device_inclusions', 'manual_device_exclusions'] as $field) $config[$field] = array_values(array_diff($config[$field] ?? [], [$sourceId]));
            $config[$included ? 'manual_device_inclusions' : 'manual_device_exclusions'][] = $sourceId;
            $this->saveConfigurationBatch([$path => $this->dumpConfig($document)], $before);
        });
    }

    public function removeManualDevice(string $map, string $sourceId, array $input): void
    {
        $this->removeManualDevices($map, [$sourceId], $input);
    }

    public function removeManualDevices(string $map, array $sourceIds, array $input): void
    {
        $this->locked(function () use ($map, $sourceIds, $input): void {
            $before = $this->treeFingerprint();
            [$main, $fragments] = $this->rawConfiguration();
            [$relative, $topology] = $this->guidedManualTopology($main, false);
            $this->assertManualFreshness($map, $relative, $input);
            $dependencies = [];
            foreach (['links', 'suppress_links'] as $field) foreach ($topology[$field] as $i => $link) {
                if (in_array($link['a']['device'] ?? '', $sourceIds, true) || in_array($link['b']['device'] ?? '', $sourceIds, true)) $dependencies[] = $field.': '.($link['source_id'] ?? '#'.($i + 1));
            }
            if ($dependencies) throw new RuntimeException('Device is referenced by '.implode(', ', $dependencies).'. Remove those references first.');
            foreach ($sourceIds as $sourceId) if (! collect($topology['devices'])->contains('source_id', $sourceId)) throw new RuntimeException('The manual device no longer exists.');
            $topology['devices'] = array_values(array_filter($topology['devices'], fn ($device) => ! in_array($device['source_id'], $sourceIds, true)));
            $main['maps'] ??= [];
            foreach ($main['maps'] as &$config) {
                foreach (['manual_device_inclusions', 'manual_device_exclusions'] as $field) if (isset($config[$field])) $config[$field] = array_values(array_diff($config[$field], $sourceIds));
            }
            unset($config);
            $changes = [$relative => $this->dumpConfig($topology, $relative), 'libremapper.yaml' => $this->dumpConfig($main)];
            foreach ($fragments as $key => $config) {
                foreach (['manual_device_inclusions', 'manual_device_exclusions'] as $field) if (isset($config[$field])) $config[$field] = array_values(array_diff($config[$field], $sourceIds));
                $changes[$this->mapFragmentPath($main, $key)] = $this->dumpConfig($config);
            }
            $this->saveConfigurationBatch($changes, $before);
        });
    }

    public function saveGraphOverride(string $map, array $input): void
    {
        $this->locked(function () use ($map, $input): void {
            $before = $this->treeFingerprint();
            [$path, $document, $index] = $this->mapDocument($map, (string) ($input['map_hash'] ?? ''));
            if ($index === null) $config = &$document; else $config = &$document['maps'][$index];
            $canonicalId = (string) $input['canonical_id'];
            $portId = ($input['port_id'] ?? '') === '' ? null : filter_var($input['port_id'], FILTER_VALIDATE_INT, ['options' => ['min_range' => 1]]);
            $plan = $this->reviewedPlan($map);
            $edge = collect($plan['operations'])->first(fn ($item) => $item['kind'] === 'edge' && $item['action'] !== 'delete' && $item['canonical_id'] === $canonicalId);
            if ($portId !== null) {
                if (! $portId || ! $edge) throw new RuntimeException('Choose a reviewed connection and current interface.');
                $port = \App\Models\Port::find($portId);
                if (! $port || ! in_array((int) $port->device_id, $edge['graph']['endpoint_device_ids'] ?? [], true)) throw new RuntimeException('The interface belongs to another device.');
                $this->requirePort($portId, (int) $port->device_id);
            }
            $config['link_graph_overrides'] = array_values(array_filter($config['link_graph_overrides'] ?? [], fn ($override) => $override['canonical_id'] !== $canonicalId));
            if (! ($input['remove'] ?? false)) $config['link_graph_overrides'][] = ['canonical_id' => $canonicalId, 'port_id' => $portId];
            $this->saveConfigurationBatch([$path => $this->dumpConfig($document)], $before);
        });
    }

    public function saveLinkSelection(string $map, array $input): void
    {
        $this->locked(function () use ($map, $input): void {
            $before = $this->treeFingerprint();
            [$path, $document, $index] = $this->mapDocument($map, $input['map_hash']);
            $plan = $this->reviewedPlan($map);
            if (! hash_equals($plan['plan_sha256'], $input['plan_sha256'])) throw new RuntimeException('Reviewed plan changed. Review changes again.');
            $choices = collect($plan['link_choices'] ?? [])->keyBy('canonical_id');
            $ids = $input['link_ids'] ?? [];
            $included = $input['included_ids'] ?? [];
            if (count($ids) !== $choices->count() || array_diff($ids, $choices->keys()->all()) || array_diff($included, $ids)) {
                throw new RuntimeException('Connection selection is incomplete. Review changes and try again.');
            }
            if ($index === null) $config = &$document; else $config = &$document['maps'][$index];
            $manualIncluded = $input['manual_included_ids'] ?? [];
            $manualIds = collect($this->manualLinks())->pluck('source_id')->all();
            if (array_diff($manualIncluded, $config['manual_link_exclusions'] ?? []) || array_diff($manualIncluded, $manualIds)) {
                throw new RuntimeException('Choose a current hidden manual connection.');
            }
            if ($manualIncluded) $config['manual_link_exclusions'] = array_values(array_diff($config['manual_link_exclusions'], $manualIncluded));
            foreach (['link_inclusions', 'link_exclusions'] as $field) $config[$field] = array_values(array_diff($config[$field] ?? [], $ids));
            foreach ($ids as $id) {
                $selected = in_array($id, $included, true);
                if ($selected && $choices[$id]['one_sided']) $config['link_inclusions'][] = $id;
                if (! $selected && ! $choices[$id]['one_sided']) $config['link_exclusions'][] = $id;
            }
            $this->saveConfigurationBatch([$path => $this->dumpConfig($document)], $before);
        });
    }

    private function saveConfigurationBatch(array $changes, string $before, bool $structural = false): void
    {
        $candidate = $this->newCandidate();
        $backups = [];
        try {
            $this->copyTree($this->configRoot, $candidate);
            foreach ($changes as $relative => $content) {
                $relative = $this->safeRelativePath($relative);
                $backups[$relative] = is_file($this->configRoot.'/'.$relative) ? file_get_contents($this->configRoot.'/'.$relative) : null;
                $this->validateContents($content);
                $this->atomicWrite($candidate.'/'.$relative, $content);
            }
            $impact = array_keys($changes) === ['libremapper.yaml'] ? $this->parserChangeImpact($candidate) : null;
            if ($impact === null) $this->validatePath($candidate.'/libremapper.yaml', structural: $structural);
            if (! hash_equals($before, $this->treeFingerprint())) {
                throw new RuntimeException('Configuration changed during validation; reload and try again.');
            }
            $this->markDrafts($candidate, $impact);
            $this->invalidatePlans($impact);
            try {
                foreach ($changes as $relative => $content) {
                    $this->atomicWrite($this->configRoot.'/'.$relative, $content);
                }
            } catch (Throwable $error) {
                foreach ($backups as $relative => $content) {
                    if ($content === null) @unlink($this->configRoot.'/'.$relative);
                    else $this->atomicWrite($this->configRoot.'/'.$relative, $content);
                }
                throw $error;
            }
        } finally {
            $this->removeTree($candidate);
        }
    }

    public function removeManualLink(string $map, string $sourceId, array $input): void
    {
        $this->removeManualLinks($map, [$sourceId], $input);
    }

    public function removeManualLinks(string $map, array $sourceIds, array $input): void
    {
        $this->locked(function () use ($map, $sourceIds, $input): void {
            $before = $this->treeFingerprint();
            [$main] = $this->rawConfiguration();
            [$relative, $topology, $mainChanged] = $this->guidedManualTopology($main, false);
            $this->assertManualFreshness($map, $relative, $input);
            $editable = collect($this->manualLinks())->where('editable', true)->pluck('source_id')->all();
            foreach ($sourceIds as $sourceId) {
                if (! in_array($sourceId, $editable, true)) throw new RuntimeException('This advanced manual link is read-only. Edit it in Advanced.');
                if (! collect($topology['links'])->contains('source_id', $sourceId)) throw new RuntimeException('The manual link no longer exists.');
            }
            $topology['links'] = array_values(array_filter(
                $topology['links'],
                fn ($link): bool => ! is_array($link) || ! in_array($link['source_id'] ?? null, $sourceIds, true)
            ));

            $changes = [$relative => $this->dumpConfig($topology, $relative)];
            if ($mainChanged) $changes['libremapper.yaml'] = $this->dumpConfig($main);
            $this->saveConfigurationBatch($changes, $before);
        });
    }

    /** @return array<int, array<string, mixed>> */
    public function manualLinks(?array $inventoryDevices = null): array
    {
        [$main] = $this->rawConfiguration();
        if (! isset($main['manual_provider'])) {
            return [];
        }
        [, $topology] = $this->guidedManualTopology($main, false);
        $devices = collect($topology['devices'])->keyBy('source_id');
        $hasReferences = collect($topology['links'])->contains(fn ($link) =>
            collect(['a', 'b'])->contains(fn ($side) => preg_match('/^librenms:[1-9][0-9]*$/', $link[$side]['device'] ?? '') && ! $devices->has($link[$side]['device'])));
        $inventory = $hasReferences ? collect($inventoryDevices ?? ($this->inventory()['devices'] ?? []))->keyBy('device_id') : collect();

        $usage = [];
        foreach ($this->configuration()['maps'] ?? [] as $map) {
            $key = $this->mapKey($map);
            if ($key === null || ! is_file($this->planPath($key))) {
                continue;
            }
            try {
                $plan = $this->reviewedPlan($key);
                foreach ($plan['operations'] as $operation) {
                    if (($operation['kind'] ?? '') === 'edge' && ($operation['action'] ?? '') !== 'delete') {
                        foreach ($operation['sources'] ?? [] as $source) {
                            if ($source['provider'] === $main['manual_provider']) {
                                $usage[$source['source_id']][] = $map['name'];
                            }
                        }
                    }
                }
                foreach ($plan['link_fallbacks'] ?? [] as $fallback) {
                    if ($fallback['source']['provider'] === $main['manual_provider']) {
                        $usage[$fallback['source']['source_id']][] = $map['name'];
                    }
                }
            } catch (Throwable) {
                // A missing or stale review will be regenerated before publishing.
            }
        }
        return array_map(function (array $link) use ($devices, $inventory, $topology, $usage): array {
            $a = $devices->get($link['a']['device'] ?? '');
            $b = $devices->get($link['b']['device'] ?? '');
            foreach (['a', 'b'] as $side) {
                if ($$side || ! preg_match('/^librenms:([1-9][0-9]*)$/', $link[$side]['device'] ?? '', $matches)) continue;
                $record = $inventory->get((int) $matches[1]);
                if ($record) $$side = ['source_id' => $link[$side]['device'], 'librenms_device_id' => (int) $matches[1],
                    'hostname' => $record['hostname'], 'interfaces' => $topology['inventory_overrides'][$link[$side]['device']]['interfaces'] ?? []];
            }

            return array_replace($link, [
                'used_maps' => array_values(array_unique($usage[$link['source_id']] ?? [])),
                'a_device_id' => $a['librenms_device_id'] ?? null,
                'b_device_id' => $b['librenms_device_id'] ?? null,
                'a_choice' => $a && ! $devices->has($a['source_id']) ? (string) $a['librenms_device_id'] : 'manual:'.($a['source_id'] ?? ''),
                'b_choice' => $b && ! $devices->has($b['source_id']) ? (string) $b['librenms_device_id'] : 'manual:'.($b['source_id'] ?? ''),
                'display_name' => ($a['hostname'] ?? $a['source_id'] ?? '?').' ↔ '.($b['hostname'] ?? $b['source_id'] ?? '?'),
                'editable' => $a !== null && $b !== null,
                'a_endpoint' => $link['a']['interface_definition'] ?? collect($a['interfaces'] ?? [])->firstWhere('source_id', $link['a']['interface'] ?? ''),
                'b_endpoint' => $link['b']['interface_definition'] ?? collect($b['interfaces'] ?? [])->firstWhere('source_id', $link['b']['interface'] ?? ''),
            ]);
        }, $topology['links']);
    }

    public function includeManualLink(string $map, string $sourceId, bool $included, array $input): void
    {
        $this->locked(function () use ($map, $sourceId, $included, $input): void {
            $before = $this->treeFingerprint();
            $this->assertManualFreshness($map, $this->manualCatalog()['path'], $input);
            if (! collect($this->manualLinks())->contains(
                fn ($link): bool => $link['source_id'] === $sourceId && $link['editable']
            )) throw new RuntimeException('Choose an editable global manual link.');
            [$path, $document, $index] = $this->mapDocument($map, $input['map_hash']);
            if ($index === null) $config = &$document; else $config = &$document['maps'][$index];
            $config['manual_link_exclusions'] = array_values(array_diff($config['manual_link_exclusions'] ?? [], [$sourceId]));
            if (! $included) $config['manual_link_exclusions'][] = $sourceId;
            $this->saveConfigurationBatch([$path => $this->dumpConfig($document)], $before);
        });
    }

    public function hasDraft(string $map): bool
    {
        $path = $this->dataRoot.'/drafts/'.hash('sha256', $map).'.pending';
        clearstatcache(true, $path);
        return is_file($path);
    }

    private function markDrafts(string $candidate, ?array $mapKeys = null): void
    {
        [$main, $fragments] = $this->rawConfiguration($candidate);
        $this->ensureDirectory($this->dataRoot.'/drafts');
        foreach (array_merge($main['maps'] ?? [], array_values($fragments)) as $config) {
            $map = $this->mapKey($config);
            if ($map === null || ($mapKeys !== null && ! in_array($map, $mapKeys, true))) continue;
            $this->atomicWrite($this->dataRoot.'/drafts/'.hash('sha256', $map).'.pending', "draft\n");
        }
    }

    private function invalidatePlans(?array $mapKeys = null): void
    {
        foreach (glob($this->dataRoot.'/plans/*.json') ?: [] as $path) {
            if ($mapKeys !== null && ! in_array($path, array_map($this->planPath(...), $mapKeys), true)) continue;
            if (! unlink($path)) {
                throw new RuntimeException('Unable to invalidate reviewed plans.');
            }
        }
    }

    public function planPath(string $map): string
    {
        return $this->dataRoot.'/plans/'.hash('sha256', $map).'.json';
    }

    /** @param array<string, mixed> $main @return array{string, array<string, mixed>, bool} */
    private function guidedManualTopology(array &$main, bool $create): array
    {
        $provider = $main['manual_provider'] ?? null;
        $changed = false;
        if ($provider === null) {
            if (! $create) {
                throw new RuntimeException('Global manual links are not configured.');
            }
            $provider = 'manual';
            for ($suffix = 1; isset($main['providers'][$provider]); $suffix++) {
                $provider = 'manual_links'.($suffix === 1 ? '' : '_'.$suffix);
            }
            $main['providers'][$provider] = [
                'type' => 'file',
                'settings' => ['path' => 'manual-topology.yaml'],
            ];
            $main['manual_provider'] = $provider;
            $changed = true;
        }
        if (! is_string($provider)
            || ($main['providers'][$provider]['type'] ?? null) !== 'file'
            || ! is_string($main['providers'][$provider]['settings']['path'] ?? null)) {
            throw new RuntimeException('manual_provider must use a managed file provider.');
        }
        try {
            $configuredPath = $main['providers'][$provider]['settings']['path'];
            if (str_starts_with($configuredPath, $this->configRoot.'/')) {
                $configuredPath = substr($configuredPath, strlen($this->configRoot) + 1);
                $main['providers'][$provider]['settings']['path'] = $configuredPath;
                $changed = true;
            }
            $relative = $this->safeRelativePath($configuredPath);
        } catch (Throwable $error) {
            throw new RuntimeException('Global manual links require a topology file inside /config.', 0, $error);
        }
        $path = $this->configRoot.'/'.$relative;
        if (! is_file($path) || is_link($path)) {
            throw new RuntimeException("Manual topology file is missing or unsafe: $relative");
        }
        $topology = $this->parseDocument((string) file_get_contents($path), 'manual topology');
        foreach (['devices', 'links', 'suppress_links'] as $field) {
            $topology[$field] ??= [];
            if (! is_array($topology[$field]) || ! array_is_list($topology[$field])) {
                throw new RuntimeException("Manual topology $field must be a YAML list.");
            }
        }

        return [$relative, $topology, $changed];
    }

    public function recordConfigError(Plugin $plugin, ?Throwable $error): void
    {
        $message = $error?->getMessage();
        $this->updatePluginSettings($plugin, function (array $settings) use ($message): array {
            if ($settings['config_error'] !== $message) {
                $settings['config_error'] = $message;
                $settings['config_error_at'] = $message === null ? null : now()->toIso8601String();
            }
            return $settings;
        });
    }

    private function migrate(Plugin $plugin, array $settings): void
    {
        $legacyConfig = is_string($settings['config_yaml'] ?? null)
            ? $settings['config_yaml']
            : self::DEFAULT_CONFIG;
        $legacyTopology = is_string($settings['manual_topology_yaml'] ?? null)
            ? $settings['manual_topology_yaml']
            : self::DEFAULT_TOPOLOGY;
        $config = $this->parseDocument($legacyConfig, 'legacy main configuration');
        $maps = $config['maps'] ?? [];
        if (! is_array($maps) || ! array_is_list($maps)) {
            throw new RuntimeException('Legacy maps must be a YAML list.');
        }
        $scheduled = is_array($settings['scheduled_maps'] ?? null) ? $settings['scheduled_maps'] : [];
        $config['state_path'] = $this->dataRoot.'/state.sqlite3';
        $config['map_directory'] = 'maps';
        $config['maps'] = [];
        $manualProvider = $config['manual_provider'] ?? null;
        if (is_string($manualProvider) && isset($config['providers'][$manualProvider])) {
            $config['providers'][$manualProvider]['settings']['path'] = 'manual-topology.yaml';
        }
        foreach ($config['providers'] ?? [] as $name => $provider) {
            if (($provider['type'] ?? null) === 'regex_artifacts') {
                $config['providers'][$name]['settings']['root'] = '/oxidized';
            }
        }

        $candidate = $this->newCandidate();
        try {
            $this->ensureDirectory($candidate.'/maps');
            $this->atomicWrite($candidate.'/libremapper.yaml', $this->dumpConfig($config));
            $this->atomicWrite($candidate.'/manual-topology.yaml', $legacyTopology);
            $keys = [];
            foreach ($maps as $map) {
                if (! is_array($map)) {
                    throw new RuntimeException('Each legacy map must be a YAML mapping.');
                }
                $key = $this->mapKey($map);
                if ($key === null || preg_match('/^[a-z0-9]+(?:-[a-z0-9]+)*$/', $key) !== 1) {
                    throw new RuntimeException('Legacy map keys must be safe lowercase names.');
                }
                if (isset($keys[$key])) {
                    throw new RuntimeException("Duplicate legacy map key: $key");
                }
                $keys[$key] = true;
                $map['key'] = $key;
                $map['scheduled'] = in_array($key, $scheduled, true);
                $this->atomicWrite($candidate.'/maps/'.$key.'.yaml', $this->dumpConfig($map));
            }
            $this->validatePath($candidate.'/libremapper.yaml');
            if (count(scandir($this->configRoot) ?: []) > 2) {
                throw new RuntimeException('Configuration mount became non-empty during migration.');
            }
            $this->copyTree($candidate, $this->configRoot);
        } finally {
            $this->removeTree($candidate);
        }
        $settings['migration_complete'] = true;
        $settings['config_error'] = null;
        $plugin->settings = $settings;
        $plugin->saveOrFail();
    }

    private function repairOldDefaults(): void
    {
        if (array_values(array_diff(scandir($this->configRoot) ?: [], ['.', '..'])) !== [
            'libremapper.yaml', 'manual-topology.yaml', 'maps',
        ]) {
            return;
        }
        if (! is_dir($this->configRoot.'/maps') || is_link($this->configRoot.'/maps')) {
            return;
        }
        if (! is_file($this->configPath()) || is_link($this->configPath())
            || ! is_file($this->configRoot.'/manual-topology.yaml')
            || is_link($this->configRoot.'/manual-topology.yaml')) {
            return;
        }
        $maps = scandir($this->configRoot.'/maps');
        if ($maps === false || array_values(array_diff($maps, ['.', '..'])) !== []) {
            return;
        }
        $expected = $this->parseDocument(self::OLD_DEFAULT_CONFIG, 'old default configuration');
        $expected['state_path'] = $this->dataRoot.'/state.sqlite3';
        if (file_get_contents($this->configPath()) !== $this->dumpConfig($expected)
            || file_get_contents($this->configRoot.'/manual-topology.yaml') !== self::DEFAULT_TOPOLOGY) {
            return;
        }
        $expected['providers'] = ['librenms' => ['type' => 'librenms', 'settings' => []]];
        $this->atomicWrite($this->configPath(), $this->dumpConfig($expected));
    }

    public static function workspaceRule(array $rule): ?array
    {
        $type = $rule['type'] ?? '';
        if ($type === 'group' && ! is_numeric($rule['value'] ?? null)) {
            return ['field' => 'group', 'value' => (string) $rule['value']];
        }
        if (in_array($type, ['hostname', 'location'], true)) {
            return ['field' => $type, 'value' => (string) $rule['value']];
        }
        if ($type === 'attribute' && in_array($rule['attribute'] ?? '', ['sys_name', 'os', 'hardware', 'type'], true)) {
            return ['field' => $rule['attribute'], 'value' => (string) $rule['value']];
        }
        return null;
    }

    private function workspaceSelectors(mixed $rows, array $existing): array
    {
        if (! is_array($rows) || ! array_is_list($rows)) {
            throw new RuntimeException('Device scope must be a list of rules.');
        }
        $inventory = $this->inventory();
        $rules = [];
        $replacements = [];
        $newRules = [];
        foreach ($rows as $row) {
            if (! is_array($row) || ! is_string($row['field'] ?? null) || ! is_string($row['value'] ?? null)) {
                throw new RuntimeException('Choose a field and value for each device rule.');
            }
            $field = $row['field'];
            $value = trim($row['value']);
            if ($value === '') {
                continue;
            }
            if (! in_array($field, ['group', 'exact_location', 'hostname', 'sys_name', 'location', 'os', 'hardware', 'type'], true)) {
                throw new RuntimeException('Choose a supported device field.');
            }
            if ($field === 'group' && ! in_array($value, array_column($inventory['groups'], 'name'), true)) {
                throw new RuntimeException('The selected group is missing, empty, or stale.');
            }
            if ($field === 'exact_location') {
                if (! in_array($value, array_column($inventory['locations'], 'location'), true)) {
                    throw new RuntimeException('The selected location is missing, empty, or stale.');
                }
                $value = '^'.preg_quote($value, '/').'$';
                $field = 'location';
            }
            $rule = ['type' => in_array($field, ['group', 'hostname', 'location'], true) ? $field : 'attribute', 'value' => $value];
            if ($rule['type'] === 'attribute') {
                $rule['attribute'] = $field;
            }
            $original = $existing[(int) ($row['index'] ?? -1)] ?? null;
            if (is_array($original) && self::workspaceRule($original) === ['field' => $field, 'value' => $value]) {
                $rule = $original;
            }
            if (array_key_exists('layer', $row)) {
                $layer = filter_var($row['layer'], FILTER_VALIDATE_INT);
                if ($layer === false) throw new RuntimeException('Scope layers must be whole numbers.');
                $rule['layer'] = $layer;
            }
            $index = (int) ($row['index'] ?? -1);
            if (isset($existing[$index]) && self::workspaceRule($existing[$index]) !== null) {
                $replacements[$index] = $rule;
            } else {
                $newRules[] = $rule;
            }
        }
        foreach ($existing as $index => $rule) {
            if (self::workspaceRule($rule) === null) {
                $rules[] = $rule;
            } elseif (isset($replacements[$index])) {
                $rules[] = $replacements[$index];
            }
        }
        $rules = [...$rules, ...$newRules];
        if (! $rules) {
            throw new RuntimeException('Choose at least one device scope rule.');
        }
        return $rules;
    }

    /** @return array<int, array<string, mixed>> */
    private function inventorySelectors(array $input): array
    {
        $choices = $input['inventory_choices'] ?? null;
        $layers = $input['inventory_layers'] ?? null;
        if (! is_array($choices) || ! is_array($layers) || count($choices) !== count($layers)) {
            throw new RuntimeException('Select at least one LibreNMS group or location and assign its layer.');
        }
        $available = [];
        $inventory = $this->inventory();
        foreach ($inventory['groups'] as $group) {
            $available['group:'.$group['name']] = true;
        }
        foreach ($inventory['locations'] as $location) {
            $available['location:'.$location['location']] = true;
        }
        $selectors = [];
        $selected = [];
        foreach ($choices as $index => $choice) {
            $layer = $layers[$index] ?? null;
            if (! is_string($choice) || ! isset($available[$choice])) {
                throw new RuntimeException('A selected LibreNMS group or location is missing, empty, or stale. Reload and choose again.');
            }
            if ((! is_string($layer) && ! is_int($layer))
                || preg_match('/^-?\d+$/', (string) $layer) !== 1) {
                throw new RuntimeException('Every selected group or location requires an integer layer.');
            }
            if (isset($selected[$choice])) {
                throw new RuntimeException('Each LibreNMS group or location may be selected only once.');
            }
            $selected[$choice] = true;
            [$type, $value] = explode(':', $choice, 2);
            $selectors[] = [
                'type' => $type,
                'value' => $type === 'location' ? '^'.preg_quote($value, '/').'$' : $value,
                'layer' => (int) $layer,
            ];
        }
        if (! $selectors) {
            throw new RuntimeException('Select at least one LibreNMS group or location and assign its layer.');
        }

        return $selectors;
    }

    /** @param array<string, mixed> $map @param array<string, mixed> $providers */
    private function synchronizeOxidizedSource(array $map, array $providers, bool $include): array
    {
        $configured = (($providers['oxidized']['type'] ?? null) === 'oxidized');
        if ($include && ! $configured) {
            throw new RuntimeException('Configure and test Oxidized Git before enabling it on a map.');
        }
        $librenmsSelectors = null;
        foreach ($map['sources'] ?? [] as $source) {
            if (is_array($source)
                && (($providers[$source['provider']]['type'] ?? null) === 'librenms')) {
                $librenmsSelectors = $source['selectors'] ?? [];
                break;
            }
        }
        if ($include && ! is_array($librenmsSelectors)) {
            throw new RuntimeException('Oxidized connections require a LibreNMS source on the map.');
        }

        $sources = [];
        $found = false;
        foreach ($map['sources'] ?? [] as $source) {
            if (! is_array($source) || ($source['provider'] ?? null) !== 'oxidized') {
                $sources[] = $source;
                continue;
            }
            if ($include && ! $found) {
                $source['selectors'] = $librenmsSelectors;
                $sources[] = $source;
                $found = true;
            }
        }
        if ($include && ! $found) {
            $sources[] = ['provider' => 'oxidized', 'selectors' => $librenmsSelectors];
        }
        $map['sources'] = $sources;

        return $map;
    }

    private function changeFile(
        string $operation,
        string $relative,
        string $content,
        string $sha256,
        bool $force
    ): void {
        $this->locked(function () use ($operation, $relative, $content, $sha256, $force): void {
            $relative = $this->safeRelativePath($relative);
            if ($operation !== 'delete') {
                $this->validateContents($content);
            }
            $path = $this->configRoot.'/'.$relative;
            if ($operation === 'create' && file_exists($path)) {
                throw new RuntimeException("File already exists: $relative");
            }
            if ($operation !== 'create') {
                $this->assertCurrentHash($path, $sha256, $force);
            }
            $before = $this->treeFingerprint();
            $candidate = $this->newCandidate();
            try {
                $this->copyTree($this->configRoot, $candidate);
                $candidatePath = $candidate.'/'.$relative;
                if ($operation === 'delete') {
                    if (! unlink($candidatePath)) {
                        throw new RuntimeException("Unable to delete $relative from the candidate tree.");
                    }
                } else {
                    $this->ensureDirectory(dirname($candidatePath));
                    $this->atomicWrite($candidatePath, $content);
                }
                $impact = $relative === 'libremapper.yaml' && $operation === 'save'
                    ? $this->parserChangeImpact($candidate) : null;
                if ($impact === null) $this->validatePath($candidate.'/libremapper.yaml');
                if (! hash_equals($before, $this->treeFingerprint())) {
                    throw new RuntimeException('Configuration changed during validation; reload and try again.');
                }
                if ($operation !== 'create') {
                    $this->assertCurrentHash($path, $sha256, $force);
                } elseif (file_exists($path) || is_link($path)) {
                    throw new RuntimeException("File appeared during validation: $relative");
                }
                $this->markDrafts($candidate, $impact);
                $this->invalidatePlans($impact);
                if ($operation === 'delete') {
                    if (! unlink($path)) {
                        throw new RuntimeException("Unable to delete $relative.");
                    }
                } else {
                    $this->ensureDirectory(dirname($path));
                    $this->atomicWrite($path, $content);
                }
            } finally {
                $this->removeTree($candidate);
            }
        });
    }

    private function assertCurrentHash(string $path, string $sha256, bool $force): void
    {
        if (! is_file($path) || is_link($path)) {
            throw new RuntimeException('The file no longer exists or is not a regular file.');
        }
        $current = hash_file('sha256', $path) ?: '';
        if (! hash_equals($current, $sha256) && ! $force) {
            throw new RuntimeException('File changed since it was loaded; explicitly confirm overwrite to continue.');
        }
    }

    /** @return array{0: array<string, mixed>, 1: array<string, array<string, mixed>>} */
    private function rawConfiguration(?string $root = null): array
    {
        $root ??= $this->configRoot;
        $config = $this->parseDocument((string) file_get_contents($root.'/libremapper.yaml'), 'main configuration');
        $directoryName = $config['map_directory'] ?? null;
        if ($directoryName === null) {
            return [$config, []];
        }
        if (! is_string($directoryName) || preg_match('/^[a-z0-9][a-z0-9_-]*$/', $directoryName) !== 1) {
            throw new RuntimeException('map_directory must be a relative directory name.');
        }
        $directory = $root.'/'.$directoryName;
        if (! is_dir($directory) || is_link($directory)) {
            throw new RuntimeException('map_directory must be a regular directory.');
        }
        $fragments = [];
        foreach (scandir($directory) ?: [] as $name) {
            if ($name === '.' || $name === '..') {
                continue;
            }
            $path = $directory.'/'.$name;
            if (str_starts_with($name, '.') || is_link($path) || is_dir($path)) {
                throw new RuntimeException("Unsafe or nested map path: $name");
            }
            if (! preg_match('/\.(?:yaml|yml)$/', $name)) {
                continue;
            }
            $map = $this->parseDocument((string) file_get_contents($path), "map fragment $name");
            $key = $this->mapKey($map);
            if ($key === null || preg_match('/^[a-z0-9]+(?:-[a-z0-9]+)*$/', $key) !== 1
                || pathinfo($name, PATHINFO_FILENAME) !== $key) {
                throw new RuntimeException("Map fragment filename must match its safe lowercase key: $name");
            }
            if (isset($fragments[$key])) {
                throw new RuntimeException("Duplicate map key: $key");
            }
            $fragments[$key] = $map;
        }

        return [$config, $fragments];
    }

    private function mapKey(array $map): ?string
    {
        if (isset($map['key']) && is_string($map['key'])) {
            return $map['key'];
        }
        if (! isset($map['name']) || ! is_string($map['name'])) {
            return null;
        }
        $key = trim((string) preg_replace('/[^a-z0-9]+/', '-', strtolower($map['name'])), '-');

        return $key === '' ? null : $key;
    }

    /** @return array<string, mixed> */
    private function parseDocument(string $contents, string $label): array
    {
        $value = Yaml::parse($contents);
        if (! is_array($value) || array_is_list($value)) {
            throw new RuntimeException(ucfirst($label).' must be a YAML mapping.');
        }

        return $value;
    }

    /** @return array<string, mixed> */
    private function validatePath(string $configPath, ?string $map = null, bool $structural = false): array
    {
        if (! is_file($configPath)) {
            throw new RuntimeException("Missing configuration file: $configPath");
        }

        return $this->execute(['operation' => 'validate', 'config' => $configPath,
            ...($map === null ? [] : ['map' => $map]), ...($structural ? ['structural' => true] : [])]);
    }

    /** @return array<string, mixed> */
    private function execute(array $payload): array
    {
        $token = $this->apiToken;
        if ($token === '' && is_readable($this->dataRoot.'/api-token')) {
            $token = trim((string) file_get_contents($this->dataRoot.'/api-token'));
        }
        if ($token === '') {
            throw new RuntimeException('LIBREMAPPER_API_TOKEN is not configured.');
        }
        try {
            $response = Http::acceptJson()
                ->withToken($token)
                ->connectTimeout(5)
                ->timeout(305)
                ->post($this->apiUrl.'/v1/run', $payload);
        } catch (Throwable $error) {
            throw new RuntimeException('Libremapper sidecar is unavailable: '.$error->getMessage(), 0, $error);
        }
        $result = $response->json();
        if (! $response->successful()) {
            $message = is_array($result) && is_string($result['error'] ?? null)
                ? $result['error']
                : trim($response->body());
            throw new RuntimeException($message ?: 'Libremapper sidecar request failed.');
        }
        if (! is_array($result) || ! is_int($result['exit_code'] ?? null)) {
            throw new RuntimeException('Libremapper sidecar returned an invalid response.');
        }
        if ($result['exit_code'] !== 0) {
            $message = is_string($result['stderr'] ?? null) ? trim($result['stderr']) : '';
            $message = $message ?: (is_string($result['stdout'] ?? null) ? trim($result['stdout']) : '');
            throw new RuntimeException($message ?: 'Libremapper process failed.');
        }

        return [
            'stdout' => is_string($result['stdout'] ?? null) ? trim($result['stdout']) : '',
            'stderr' => is_string($result['stderr'] ?? null) ? trim($result['stderr']) : '',
        ];
    }

    private function configPath(): string
    {
        return $this->configRoot.'/libremapper.yaml';
    }

    private function requireConfigRoot(): void
    {
        if (! is_dir($this->configRoot)) {
            throw new RuntimeException("Configuration mount is missing: {$this->configRoot}");
        }
        if (! is_writable($this->configRoot)) {
            throw new RuntimeException("Configuration mount is not writable: {$this->configRoot}");
        }
    }

    private function newCandidate(): string
    {
        $directory = $this->dataRoot.'/runtime/candidate-'.bin2hex(random_bytes(8));
        $this->ensureDirectory($directory);

        return $directory;
    }

    private function writeDefaults(string $root): void
    {
        $this->ensureDirectory($root.'/maps');
        $config = $this->parseDocument(self::DEFAULT_CONFIG, 'default configuration');
        $config['state_path'] = $this->dataRoot.'/state.sqlite3';
        $this->atomicWrite($root.'/libremapper.yaml', $this->dumpConfig($config));
        $this->atomicWrite($root.'/manual-topology.yaml', self::DEFAULT_TOPOLOGY);
    }

    private function safeRelativePath(string $relative): string
    {
        $relative = str_replace('\\', '/', trim($relative));
        $parts = explode('/', $relative);
        if ($relative === '' || str_starts_with($relative, '/')
            || array_filter($parts, fn (string $part): bool => $part === '' || $part === '.'
                || $part === '..' || str_starts_with($part, '.'))) {
            throw new RuntimeException('Unsafe configuration file path.');
        }
        if (! preg_match('/\.(?:yaml|yml|json)$/', $relative)) {
            throw new RuntimeException('Configuration files must use .yaml, .yml, or .json.');
        }
        $current = $this->configRoot;
        foreach (array_slice($parts, 0, -1) as $part) {
            $current .= '/'.$part;
            if (is_link($current)) {
                throw new RuntimeException('Configuration paths cannot contain symlinks.');
            }
        }

        return implode('/', $parts);
    }

    private function validateContents(string $content): void
    {
        if (strlen($content) > self::MAX_FILE_SIZE) {
            throw new RuntimeException('Configuration files may not exceed 5 MiB.');
        }
        if (preg_match('//u', $content) !== 1) {
            throw new RuntimeException('Configuration files must contain valid UTF-8.');
        }
    }

    /** @return array<int, string> */
    private function managedPaths(bool $strict = true): array
    {
        $paths = [];
        $walk = function (string $directory, string $prefix = '') use (&$walk, &$paths, $strict): void {
            foreach (scandir($directory) ?: [] as $name) {
                if ($name === '.' || $name === '..') {
                    continue;
                }
                $relative = ltrim($prefix.'/'.$name, '/');
                $path = $directory.'/'.$name;
                $unsafe = str_starts_with($name, '.') || is_link($path);
                if ($unsafe) {
                    if ($strict) {
                        throw new RuntimeException("Unsafe configuration path: $relative");
                    }
                    continue;
                }
                if (str_ends_with($name, '.bak') && is_file($path) && ! (fileperms($path) & 0111)) {
                    continue;
                }
                if (is_dir($path)) {
                    $walk($path, $relative);
                    continue;
                }
                $invalid = ! is_file($path) || (fileperms($path) & 0111)
                    || ! preg_match('/\.(?:yaml|yml|json)$/', $name)
                    || filesize($path) > self::MAX_FILE_SIZE;
                if ($invalid) {
                    if ($strict) {
                        throw new RuntimeException("Unmanaged configuration file: $relative");
                    }
                    continue;
                }
                $contents = file_get_contents($path);
                if ($contents === false || preg_match('//u', $contents) !== 1) {
                    if ($strict) {
                        throw new RuntimeException("Configuration file is not valid UTF-8: $relative");
                    }
                    continue;
                }
                $paths[] = $relative;
            }
        };
        if (is_dir($this->configRoot)) {
            $walk($this->configRoot);
        }
        sort($paths, SORT_STRING);

        return $paths;
    }

    private function treeFingerprint(): string
    {
        $hash = hash_init('sha256');
        foreach ($this->managedPaths() as $relative) {
            hash_update($hash, $relative."\0".(hash_file('sha256', $this->configRoot.'/'.$relative) ?: '')."\0");
        }

        return hash_final($hash);
    }

    private function copyTree(string $source, string $destination): void
    {
        $this->ensureDirectory($destination);
        $walk = function (string $from, string $to) use (&$walk): void {
            foreach (scandir($from) ?: [] as $name) {
                if ($name === '.' || $name === '..') {
                    continue;
                }
                $source = $from.'/'.$name;
                $target = $to.'/'.$name;
                if (str_starts_with($name, '.') || is_link($source)) {
                    throw new RuntimeException("Unsafe configuration path: $source");
                }
                if (str_ends_with($name, '.bak') && is_file($source) && ! (fileperms($source) & 0111)) {
                    continue;
                }
                if (is_dir($source)) {
                    $this->ensureDirectory($target);
                    $walk($source, $target);
                    continue;
                }
                if (! is_file($source) || (fileperms($source) & 0111)
                    || ! preg_match('/\.(?:yaml|yml|json)$/', $name)
                    || filesize($source) > self::MAX_FILE_SIZE) {
                    throw new RuntimeException("Unmanaged configuration file: $source");
                }
                $contents = file_get_contents($source);
                if ($contents === false) {
                    throw new RuntimeException("Unable to read $source.");
                }
                $this->validateContents($contents);
                $this->atomicWrite($target, $contents);
            }
        };
        $walk($source, $destination);
    }

    private function copyRawTree(string $source, string $destination): void
    {
        $this->ensureDirectory($destination);
        foreach (scandir($source) ?: [] as $name) {
            if ($name === '.' || $name === '..') {
                continue;
            }
            $from = $source.'/'.$name;
            $to = $destination.'/'.$name;
            if (is_link($from)) {
                if (! symlink((string) readlink($from), $to)) {
                    throw new RuntimeException("Unable to back up symlink $from.");
                }
            } elseif (is_dir($from)) {
                $this->copyRawTree($from, $to);
            } elseif (! copy($from, $to)) {
                throw new RuntimeException("Unable to back up $from.");
            }
        }
    }

    private function clearTree(string $root): void
    {
        foreach (scandir($root) ?: [] as $name) {
            if ($name === '.' || $name === '..') {
                continue;
            }
            $path = $root.'/'.$name;
            if (is_link($path)) {
                unlink($path);
            } elseif (is_dir($path)) {
                $this->removeTree($path);
            } else {
                unlink($path);
            }
        }
    }

    private function removeTree(string $root): void
    {
        if (! is_dir($root) || is_link($root)) {
            return;
        }
        $this->clearTree($root);
        rmdir($root);
    }

    private function ensureDirectory(string $path): void
    {
        if (! is_dir($path) && ! mkdir($path, 0770, true) && ! is_dir($path)) {
            throw new RuntimeException("Unable to create directory $path.");
        }
    }

    private function atomicWrite(string $path, string $contents, int $mode = 0660): void
    {
        $temporary = tempnam(dirname($path), '.libremapper-');
        if ($temporary === false) {
            throw new RuntimeException("Unable to create a temporary file for $path.");
        }
        try {
            if (file_put_contents($temporary, $contents) === false
                || ! chmod($temporary, $mode)
                || ! rename($temporary, $path)) {
                throw new RuntimeException("Unable to write $path.");
            }
        } catch (Throwable $error) {
            @unlink($temporary);
            throw $error;
        }
    }

    /** @return array<string, mixed> */
    private function typedFields(array $input, array $types, string $prefix): array
    {
        $output = [];
        foreach ($types as $field => $type) {
            $key = $prefix.$field;
            if (! array_key_exists($key, $input)) {
                continue;
            }
            $value = $input[$key];
            $output[$field] = match ($type) {
                'int' => (int) $value,
                'nullable-int' => $value === '' || $value === null ? null : (int) $value,
                'bool' => (bool) $value,
                default => (string) $value,
            };
        }

        return $output;
    }

    /** @return array<int, mixed> */
    private function yamlList(string $yaml, string $label): array
    {
        $value = trim($yaml) === '' ? [] : Yaml::parse($yaml);
        if (! is_array($value) || ! array_is_list($value)) {
            throw new RuntimeException("$label must be a YAML list.");
        }

        return $value;
    }

    private function dumpConfig(array $config, string $relative = ''): string
    {
        if (str_ends_with($relative, '.json')) {
            foreach ($config['devices'] ?? [] as $index => $device) {
                if (isset($device['attributes'])) $config['devices'][$index]['attributes'] = (object) $device['attributes'];
                foreach ($device['interfaces'] ?? [] as $i => $interface) {
                    if (isset($interface['attributes'])) $config['devices'][$index]['interfaces'][$i]['attributes'] = (object) $interface['attributes'];
                }
            }
            foreach ($config['links'] ?? [] as $index => $link) {
                if (isset($link['attributes'])) $config['links'][$index]['attributes'] = (object) $link['attributes'];
            }
            return json_encode($config, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR)."\n";
        }
        if (isset($config['oxidized_parsers'])) $config['oxidized_parsers'] = (object) $config['oxidized_parsers'];
        $yaml = Yaml::dump($config, 12, 2, Yaml::DUMP_MULTI_LINE_LITERAL_BLOCK | Yaml::DUMP_OBJECT_AS_MAP | Yaml::DUMP_NUMERIC_KEY_AS_STRING);

        return preg_replace(
            '/^(\s*)(maps|sources|selectors|parsers|ipv4_prefix_lengths|ipv6_prefix_lengths|protocols|interface_types|layer_overrides|short_hostname_overrides|device_exclusions|manual_link_exclusions|link_inclusions|link_exclusions|manual_device_inclusions|manual_device_exclusions|link_graph_overrides|keys|rules|flags|link_adapter_command|devices|interfaces|links|suppress_links): \{\s*\}$/m',
            '$1$2: []',
            $yaml
        ) ?? $yaml;
    }

    private function locked(callable $callback): mixed
    {
        if ($this->lockHeld) {
            return $callback();
        }
        $this->ensureDirectory($this->dataRoot);
        $path = $this->dataRoot.'/action.lock';
        $lock = fopen($path, 'c+');
        if ($lock === false) {
            throw new RuntimeException("Unable to open Libremapper action lock: $path");
        }
        chmod($path, 0660);
        if (! flock($lock, LOCK_EX | LOCK_NB)) {
            fclose($lock);
            throw new RuntimeException('Another Libremapper action is already running.');
        }
        $this->lockHeld = true;
        try {
            return $callback();
        } finally {
            $this->lockHeld = false;
            flock($lock, LOCK_UN);
            fclose($lock);
        }
    }

    /** @return array<string, mixed> */
    private function presentPlan(array $plan, array $process): array
    {
        $plan['operations'] = $plan['operations'] ?? [];
        $plan['link_fallbacks'] = $plan['link_fallbacks'] ?? [];
        $plan['warnings'] = $plan['warnings'] ?? [];
        $plan['conflicts'] = $plan['conflicts'] ?? [];
        $plan['changes'] = count(array_filter(
            $plan['operations'],
            fn ($operation): bool => ($operation['action'] ?? null) !== 'noop'
        ));
        $plan['device_count'] = count(array_filter(
            $plan['operations'],
            fn ($operation): bool => ($operation['kind'] ?? null) === 'node'
                && ($operation['action'] ?? null) !== 'delete'
        ));
        $plan['link_count'] = count(array_filter(
            $plan['operations'],
            fn ($operation): bool => ($operation['kind'] ?? null) === 'edge'
                && ($operation['action'] ?? null) !== 'delete'
        ));

        $discoveredLinks = [];
        foreach ($plan['operations'] as $operation) {
            if (($operation['kind'] ?? null) !== 'edge'
                || ($operation['action'] ?? null) === 'delete'
                || ($operation['protocol'] ?? null) === 'manual') {
                continue;
            }
            foreach (['from_node', 'to_node'] as $endpoint) {
                $canonicalId = $operation['generated_values'][$endpoint] ?? null;
                if (is_string($canonicalId)) {
                    $discoveredLinks[$canonicalId] = ($discoveredLinks[$canonicalId] ?? 0) + 1;
                }
            }
        }
        $plan['isolated_device_count'] = 0;
        foreach ($plan['operations'] as &$operation) {
            if (($operation['kind'] ?? null) !== 'node' || ($operation['action'] ?? null) === 'delete') {
                continue;
            }
            $operation['discovered_link_count'] = $discoveredLinks[$operation['canonical_id']] ?? 0;
            if ($operation['discovered_link_count'] === 0) {
                $plan['isolated_device_count']++;
            }
        }
        unset($operation);

        $warningGroups = [];
        foreach ($plan['warnings'] as $warning) {
            $warning = (string) $warning;
            if (str_contains($warning, 'did not resolve uniquely; retaining it as an unbound interface')) {
                $key = 'unresolved-interface';
                $label = 'Interface names did not match a unique LibreNMS port';
            } elseif (preg_match('/(?:local|remote) device .+ did not resolve uniquely/', $warning) === 1) {
                $key = 'unresolved-device';
                $label = 'Device names did not match a unique LibreNMS device';
            } else {
                $key = 'warning:'.$warning;
                $label = $warning;
            }
            $warningGroups[$key] ??= ['label' => $label, 'count' => 0];
            $warningGroups[$key]['count']++;
        }
        $plan['warning_groups'] = array_values($warningGroups);

        return array_replace($plan, $process);
    }
}
