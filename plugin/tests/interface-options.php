<?php

declare(strict_types=1);

use Libremapper\Plugin\InterfaceOptions;

require __DIR__.'/../src/InterfaceOptions.php';

$base = [
    'ifType' => 'ethernetCsmacd', 'ifSpeed' => 1000000000,
    'ifAdminStatus' => 'up', 'ifOperStatus' => 'up', 'ifIndex' => 1,
    'disabled' => 0, 'deleted' => 0, 'ifAlias' => 'Uplink',
];
$rows = [];
foreach ([
    [1, 'Ethernet10', []],
    [2, 'Ethernet2', ['ifOperStatus' => 'down']],
    [3, 'Vlan1', ['ifType' => 'l2vlan']],
    [4, 'Ethernet4', ['disabled' => '1']],
    [5, 'Ethernet5', ['ifSpeed' => 0]],
    [6, 'Ethernet6', ['ifAdminStatus' => 'down']],
    [7, 'Ethernet7', ['deleted' => '1']],
    [8, 'Ethernet8', []],
    [9, 'ethernet8', ['ifIndex' => 2]],
    [10, '', ['ifDescr' => 'Backup', 'ifSpeed' => '100000000']],
] as [$id, $name, $fields]) {
    $rows[] = array_replace($base, ['port_id' => $id, 'ifName' => $name], $fields);
}
$options = InterfaceOptions::classify($rows);
$byId = array_column($options, null, 'port_id');
$check = static function (bool $condition, string $message): void {
    if (! $condition) {
        throw new RuntimeException($message);
    }
};
$check(array_column(array_filter($options, fn (array $row): bool => $row['usable']), 'port_id') === [10, 2, 1], 'Usable physical ports must rank first in natural name order, including oper-down ports.');
foreach ([3, 4, 5, 6, 7, 8, 9] as $id) {
    $check(! $byId[$id]['usable'], "Port $id must require Show all interfaces.");
}
$check($byId[2]['oper_status'] === 'down' && $byId[6]['admin_status'] === 'down', 'Both interface statuses must be returned.');
$check($byId[7]['deleted'] && $byId[4]['disabled'], 'Deleted and disabled flags must survive classification.');
$check($byId[10]['name'] === 'Backup' && $byId[10]['speed'] === 100000000, 'Names must fall back to descriptions and speeds must be numeric.');
foreach ($options as $option) {
    $check(str_contains($option['text'], 'ifIndex '.$option['if_index']) && str_contains($option['text'], 'port ID '.$option['port_id']), 'Every choice must disambiguate interface and port IDs.');
}
$check(InterfaceOptions::classify(array_reverse($rows)) === $options, 'Interface order must be independent of database row order.');
$check(InterfaceOptions::classify([]) === [], 'Devices without interfaces must return no choices.');
echo "Interface option checks passed.\n";
