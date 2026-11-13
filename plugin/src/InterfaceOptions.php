<?php

namespace Libremapper\Plugin;

use App\Models\Port;
use App\Models\User;

class InterfaceOptions
{
    /** @return array<int, array<string, mixed>> */
    public static function options(int $deviceId, User $user): array
    {
        $rows = Port::hasAccess($user)->where('ports.device_id', $deviceId)->get([
            'port_id', 'ifName', 'ifDescr', 'ifAlias', 'ifType', 'ifSpeed',
            'ifAdminStatus', 'ifOperStatus', 'ifIndex', 'disabled', 'deleted',
        ])->map(fn (Port $port): array => $port->getAttributes())->all();

        return self::classify($rows);
    }

    /** @param array<int, array<string, mixed>> $rows @return array<int, array<string, mixed>> */
    public static function classify(array $rows): array
    {
        $options = [];
        $nameCounts = [];
        foreach ($rows as $row) {
            $name = trim((string) ($row['ifName'] ?? ''));
            if ($name === '') {
                $name = trim((string) ($row['ifDescr'] ?? '')) ?: 'Unnamed interface';
            }
            $nameKey = strtolower($name);
            $nameCounts[$nameKey] = ($nameCounts[$nameKey] ?? 0) + 1;
            $options[] = [
                'port_id' => (int) $row['port_id'],
                'name' => $name,
                'alias' => (string) ($row['ifAlias'] ?? ''),
                'type' => (string) ($row['ifType'] ?? ''),
                'speed' => (int) ($row['ifSpeed'] ?? 0),
                'admin_status' => (string) ($row['ifAdminStatus'] ?? ''),
                'oper_status' => (string) ($row['ifOperStatus'] ?? ''),
                'if_index' => (int) ($row['ifIndex'] ?? 0),
                'disabled' => (bool) ($row['disabled'] ?? false),
                'deleted' => (bool) ($row['deleted'] ?? false),
            ];
        }
        foreach ($options as &$option) {
            $option['usable'] = ! $option['disabled'] && ! $option['deleted']
                && $option['speed'] > 0 && strtolower($option['admin_status']) === 'up'
                && in_array(strtolower($option['type']), [
                    'ethernetcsmacd', 'iso88023csmacd', 'fastether', 'fastetherfx', 'gigabitethernet',
                ], true)
                && $nameCounts[strtolower($option['name'])] === 1;
            $option['text'] = $option['name']
                .($option['alias'] === '' ? '' : ' — '.$option['alias'])
                .' (ifIndex '.$option['if_index'].'; port ID '.$option['port_id'].')';
        }
        unset($option);
        usort($options, fn (array $a, array $b): int => ($b['usable'] <=> $a['usable'])
            ?: strnatcasecmp($a['name'], $b['name'])
            ?: ($a['if_index'] <=> $b['if_index'])
            ?: ($a['port_id'] <=> $b['port_id']));

        return $options;
    }
}
