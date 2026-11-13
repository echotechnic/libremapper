<?php

declare(strict_types=1);

use Illuminate\Http\Request;
use Libremapper\Plugin\Engine;
use Libremapper\Plugin\Http\LibremapperController;
use Symfony\Component\Yaml\Yaml;

// Included by run.php so these checks share its isolated synthetic stack and cleanup.
$parserRoot = '/data/libremapper/runtime/candidate-'.bin2hex(random_bytes(8));
$cleanupPaths[] = $parserRoot;
mkdir($parserRoot.'/maps', 0770, true);
$parserSuffix = bin2hex(random_bytes(4));
$parserMapA = 'php-parser-a-'.$parserSuffix;
$parserMapB = 'php-parser-b-'.$parserSuffix;
$parserMapC = 'php-parser-c-'.$parserSuffix;
$parserMapKeys = [$parserMapA, $parserMapB, $parserMapC];
$parserMapPath = static fn (string $key): string => $parserRoot.'/maps/'.$key.($key === $parserMapC ? '.yml' : '.yaml');
$parserMain = ['version' => 1, 'state_path' => '/data/libremapper/state.sqlite3',
    'map_directory' => 'maps', 'librenms' => [],
    'providers' => ['native' => ['type' => 'librenms', 'settings' => []]], 'maps' => []];
file_put_contents($parserRoot.'/libremapper.yaml', dumpTestConfig($parserMain));
foreach ($parserMapKeys as $parserMapKey) {
    file_put_contents($parserMapPath($parserMapKey), dumpTestConfig([
        'key' => $parserMapKey, 'name' => 'PHP Freshness Parser '.$parserMapKey,
        'sources' => [['provider' => 'native', 'selectors' => [['type' => 'device_id', 'value' => 1001]]]],
    ]));
}
$parserEngine = new Engine(null, '/data/libremapper', $parserRoot);
$parserSha = static fn (): string => hash_file('sha256', $parserRoot.'/libremapper.yaml');
$parserFresh = static fn (string $key): array => ['sha256' => $parserSha(),
    'map_hash' => hash('sha256', json_encode($parserEngine->mapConfiguration($key), JSON_THROW_ON_ERROR))];
$clearParserDrafts = static function () use ($parserMapKeys): void {
    foreach ($parserMapKeys as $key) @unlink('/data/libremapper/drafts/'.hash('sha256', $key).'.pending');
};
register_shutdown_function(static function () use ($clearParserDrafts, $parserMapKeys, $parserEngine): void {
    $clearParserDrafts();
    foreach ($parserMapKeys as $key) @unlink($parserEngine->planPath($key));
});
$seedParserReviews = static function () use ($parserEngine, $parserMapA, $parserMapC): void {
    foreach ([$parserMapA, $parserMapC] as $key) file_put_contents($parserEngine->planPath($key), 'review-'.$key);
};

$parserCatalog = $parserEngine->parserCatalog();
check(count($parserCatalog['items']) === 2 && ! isset($parserEngine->configuration()['providers']['oxidized']),
    'Built-in parser definitions must be available before configuring Git.');
$lldpEntry = collect($parserCatalog['items'])->firstWhere('id', 'saos10-lldp-neighbors');
$parserEngine->saveParser('0', $lldpEntry['definition'], $parserSha(), true);
check(collect($parserEngine->parserCatalog()['items'])->contains('id', '0'), 'Numeric string IDs must retain a YAML mapping and string keys.');
$parserEngine->saveParser('0e1', $lldpEntry['definition'], $parserSha(), true);
$parserEngine->removeParser('0e1', $parserSha(), false);
check(collect($parserEngine->parserCatalog()['items'])->contains('id', '===', '0'), 'Parser IDs must use exact string identity.');
$parserEngine->removeParser('0', $parserSha(), false);
$unicodeDefinition = $lldpEntry['definition'];
$unicodeDefinition['rules'][0]['regex'] = '(?P<remote_hostname>'.str_repeat('界', 11000).')';
$parserEngine->saveParser('unicode-format', $unicodeDefinition, $parserSha(), true);
check(collect($parserEngine->parserCatalog()['items'])->contains('id', 'unicode-format'),
    'Definition size checks must count UTF-8 bytes consistently with the underlying file.');
$parserEngine->removeParser('unicode-format', $parserSha(), false);
$customParserId = 'custom-neighbors-'.$parserSuffix;
$customDefinition = $lldpEntry['definition'];
$customDefinition['label'] = 'Custom neighbor format';
$beforeCreation = $parserSha();
$parserEngine->saveParser($customParserId, $customDefinition, $beforeCreation, true);
$customEntry = collect($parserEngine->parserCatalog()['items'])->firstWhere('id', $customParserId);
check(! $customEntry['builtin'] && $customEntry['maps'] === []
    && $customEntry['definition']['rules'][0]['id'] === $lldpEntry['definition']['rules'][0]['id'],
    'Cloning must preserve stable format IDs while leaving the new parser disabled.');
check(! $parserEngine->hasDraft($parserMapA) && ! $parserEngine->hasDraft($parserMapB),
    'Creating an unused parser must not mark unrelated maps as drafts.');
fails(fn () => $parserEngine->saveParser($customParserId, $customDefinition, $beforeCreation, false), 'File changed since it was loaded');
fails(fn () => $parserEngine->saveParser($customParserId, $customDefinition, $parserSha(), true), 'already exists');
$badDefinition = $customDefinition;
$badDefinition['rules'][0]['regex'] = '(?P<wrong_capture>.*)';
$beforeInvalid = file_get_contents($parserRoot.'/libremapper.yaml');
fails(fn () => $parserEngine->saveParser($customParserId, $badDefinition, $parserSha(), false), 'remote_hostname');
check(file_get_contents($parserRoot.'/libremapper.yaml') === $beforeInvalid,
    'Invalid parser definitions must leave the shared configuration unchanged.');
fails(fn () => $parserEngine->removeParser('saos10-lldp-neighbors', $parserSha(), false), 'can be reset');
fails(fn () => $parserEngine->removeParser($customParserId, $parserSha(), true), 'Only built-in');

$localHostname = collect($parserEngine->inventory()['devices'])->firstWhere('device_id', 1001)['hostname'];
$previewDefinition = $customDefinition;
$previewDefinition['protocol'] = 'custom-preview';
$preview = $parserEngine->previewParser([
    'parser_id' => $customParserId, 'definition' => $previewDefinition,
    'filename' => 'topology/'.$localHostname.'--lldp-neighbors',
    'sample' => "| interface | missing-local-interface |\n| chassis-id | 00aabbccddee |\n"
        ."| port-id | external-port |\n| system-name | parser-external.example.test |\ncredential secret-not-for-output\n",
]);
check($preview['connection_count'] === 1 && $preview['links'][0]['protocol'] === 'custom-preview'
    && ! str_contains(json_encode($preview), 'secret-not-for-output')
    && file_get_contents($parserRoot.'/libremapper.yaml') === $beforeInvalid
    && ! $parserEngine->hasDraft($parserMapA),
    'Unsaved pasted preview must resolve endpoints without Git, raw credential output, or draft mutations.');
$parserViewData = (new LibremapperController($parserEngine))->viewData(Request::create(
    '/plugin/settings/libremapper?map='.$parserMapB.'&tab=advanced&subtab=parsers&edit='.$customParserId
));
$parserHtml = view('libremapper::settings', $parserViewData)->render();
foreach (['Global parser catalog', 'Edit underlying file in Advanced', 'id="parser-form"', 'id="parser-rules"',
    'Pasted text', 'Repository artifact', 'id="parser-preview-form"'] as $copy) {
    check(str_contains($parserHtml, $copy), 'Parser editor must render without Git: '.$copy);
}

// The source is deliberately unavailable: saving definitions and disabled-map reviews must stay offline.
$parserMain = Yaml::parseFile($parserRoot.'/libremapper.yaml');
$parserMain['providers']['oxidized'] = ['type' => 'oxidized', 'settings' => ['repository' => '/missing-parser-test-repository']];
file_put_contents($parserRoot.'/libremapper.yaml', dumpTestConfig($parserMain));
foreach ([$parserMapA => [$customParserId], $parserMapB => [], $parserMapC => null] as $key => $selection) {
    $document = Yaml::parseFile($parserMapPath($key));
    $source = ['provider' => 'oxidized', 'selectors' => $document['sources'][0]['selectors']];
    if ($selection !== null) $source['parsers'] = $selection;
    $document['sources'][] = $source;
    file_put_contents($parserMapPath($key), dumpTestConfig($document));
}
$parserEngine->plan($parserMapB);
$unaffectedReview = file_get_contents($parserEngine->planPath($parserMapB));
$clearParserDrafts();
$seedParserReviews();
$customDefinition['label'] = 'Edited neighbor format';
$parserEngine->saveParser($customParserId, $customDefinition, $parserSha(), false);
check($parserEngine->hasDraft($parserMapA) && ! $parserEngine->hasDraft($parserMapB) && ! $parserEngine->hasDraft($parserMapC)
    && ! is_file($parserEngine->planPath($parserMapA)) && is_file($parserEngine->planPath($parserMapC))
    && file_get_contents($parserEngine->planPath($parserMapB)) === $unaffectedReview,
    'Shared parser edits must invalidate only effectively enabled maps and preserve unrelated review bytes.');
$parserEngine->apply($parserMapB);
check(! $parserEngine->hasDraft($parserMapB),
    'An unaffected saved review must publish despite another map having an unavailable parser source.');
$parserEngine->plan($parserMapB);
$unaffectedReview = file_get_contents($parserEngine->planPath($parserMapB));

$clearParserDrafts();
$seedParserReviews();
$lldpOverride = $lldpEntry['definition'];
$lldpOverride['label'] = 'Locally edited LLDP format';
$parserEngine->saveParser('saos10-lldp-neighbors', $lldpOverride, $parserSha(), false);
$overridden = collect($parserEngine->parserCatalog()['items'])->firstWhere('id', 'saos10-lldp-neighbors');
check($overridden['overridden'] && $parserEngine->hasDraft($parserMapC) && ! $parserEngine->hasDraft($parserMapA)
    && ! $parserEngine->hasDraft($parserMapB) && ! is_file($parserEngine->planPath($parserMapC))
    && is_file($parserEngine->planPath($parserMapA)),
    'Built-in overrides must retain their identity and invalidate maps inheriting legacy activation.');
$clearParserDrafts();
$seedParserReviews();
$parserEngine->removeParser('saos10-lldp-neighbors', $parserSha(), true);
$resetEntry = collect($parserEngine->parserCatalog()['items'])->firstWhere('id', 'saos10-lldp-neighbors');
check(! $resetEntry['overridden'] && $resetEntry['definition'] === $lldpEntry['definition']
    && $parserEngine->hasDraft($parserMapC) && ! $parserEngine->hasDraft($parserMapB),
    'Reset must restore shipped defaults while invalidating only maps using that parser.');

$clearParserDrafts();
$seedParserReviews();
$advancedParserMain = Yaml::parseFile($parserRoot.'/libremapper.yaml');
$advancedParserMain['oxidized_parsers'][$customParserId]['label'] = 'Advanced neighbor format';
$advancedParserMain = array_reverse($advancedParserMain, true);
$parserEngine->saveFile('libremapper.yaml', dumpTestConfig($advancedParserMain), $parserSha());
$advancedEntry = collect($parserEngine->parserCatalog()['items'])->firstWhere('id', $customParserId);
check($advancedEntry['definition']['label'] === 'Advanced neighbor format'
    && $parserEngine->hasDraft($parserMapA) && ! $parserEngine->hasDraft($parserMapB) && ! $parserEngine->hasDraft($parserMapC)
    && file_get_contents($parserEngine->planPath($parserMapB)) === $unaffectedReview,
    'Advanced parser-only edits must reload in the catalog with the same selective impact.');

$bPath = $parserRoot.'/maps/'.$parserMapB.'.yaml';
$beforeUnrelated = file_get_contents($bPath);
$changedUnrelated = Yaml::parse($beforeUnrelated);
$changedUnrelated['node_defaults']['size'] = 42;
file_put_contents($bPath, dumpTestConfig($changedUnrelated));
try {
    fails(fn () => $parserEngine->apply($parserMapB), 'configuration changed after this plan was generated');
} finally {
    file_put_contents($bPath, $beforeUnrelated);
}
fails(fn () => $parserEngine->removeParser($customParserId, $parserSha(), false), $parserMapA);
fails(fn () => $parserEngine->saveParserSelection($parserMapB, ['unknown-parser'], $parserFresh($parserMapB)), 'current parsers');
fails(fn () => $parserEngine->saveParserSelection($parserMapB, [$customParserId, $customParserId], $parserFresh($parserMapB)), 'without duplicates');
$staleSelection = $parserFresh($parserMapB);
$unusedParserId = 'unused-neighbors-'.$parserSuffix;
$parserEngine->saveParser($unusedParserId, $customDefinition, $parserSha(), true);
fails(fn () => $parserEngine->saveParserSelection($parserMapB, [$customParserId], $staleSelection), 'File changed since it was loaded');
$parserEngine->saveParserSelection($parserMapB, [$customParserId], $parserFresh($parserMapB));
check($parserEngine->parserCatalog()['selections'][$parserMapB]['oxidized'] === [$customParserId],
    'Map activation must save independently without requiring repository access.');
$staleMapSelection = $parserFresh($parserMapB);
$staleMapSelection['map_hash'] = str_repeat('0', 64);
fails(fn () => $parserEngine->saveParserSelection($parserMapB, [], $staleMapSelection), 'Draft changed since this page loaded');
$scopePath = $parserRoot.'/maps/'.$parserMapA.'.yaml';
$scopedMap = Yaml::parseFile($scopePath);
$scopedMap['sources'][1]['selectors'] = [['type' => 'hostname', 'value' => '^core']];
$scopedMap['sources'][] = ['provider' => 'oxidized', 'parsers' => [],
    'selectors' => [['type' => 'hostname', 'value' => '^pop']], 'protocols' => ['ip-p2p']];
file_put_contents($scopePath, dumpTestConfig($scopedMap));
$parserEngine->saveParserSelection($parserMapA, [$customParserId], $parserFresh($parserMapA));
$savedScopes = $parserEngine->mapConfiguration($parserMapA)['sources'];
check(count($savedScopes) === 3 && $savedScopes[1]['selectors'] === $scopedMap['sources'][1]['selectors']
    && $savedScopes[2]['selectors'] === $scopedMap['sources'][2]['selectors']
    && $savedScopes[2]['protocols'] === ['ip-p2p'], 'Parser selection must preserve every advanced source scope and protocol filter.');
$nativeMap = file_get_contents($bPath);
$parserOnly = Yaml::parse($nativeMap);
$parserOnly['sources'] = [['provider' => 'oxidized', 'parsers' => []]];
file_put_contents($bPath, dumpTestConfig($parserOnly));
try {
    $parserEngine->saveParserSelection($parserMapB, [], $parserFresh($parserMapB));
    check($parserEngine->mapConfiguration($parserMapB)['sources'][0]['parsers'] === [],
        'Parser-only maps must support disabling parsing without a LibreNMS source.');
} finally {
    file_put_contents($bPath, $nativeMap);
}
foreach ($parserMapKeys as $key) $parserEngine->saveParserSelection($key, [], $parserFresh($key));
check(collect($parserEngine->parserCatalog()['selections'])->every(fn ($sources) => $sources['oxidized'] === []),
    'Unchecking every parser must persist an explicit empty selection.');
$emptyReport = $parserEngine->providerInspection('oxidized', $parserMapB);
check(! $emptyReport['blocked'] && $emptyReport['matched_files'] === [] && $emptyReport['topology']['links'] === [],
    'A disabled map report must return no results without trying the unavailable Git source.');
$parserEngine->removeParser($customParserId, $parserSha(), false);
check(! collect($parserEngine->parserCatalog()['items'])->contains('id', $customParserId),
    'An unused custom parser must be deletable after all map references are removed.');
$parserEngine->configureOxidized('disabled', '', '', sha256: $parserSha());
check(! isset($parserEngine->configuration()['providers']['oxidized'])
    && collect($parserEngine->parserCatalog()['items'])->contains('id', $unusedParserId)
    && collect($parserEngine->configuration()['maps'])->every(fn ($map) => count($map['sources']) === 1),
    'Disconnecting Git must remove disabled sources while preserving the global parser catalog.');
$parserEngine->removeParser($unusedParserId, $parserSha(), false);
$clearParserDrafts();
foreach ($parserMapKeys as $key) @unlink($parserEngine->planPath($key));

echo "PHP parser checks passed.\n";
