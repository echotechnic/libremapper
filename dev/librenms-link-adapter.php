<?php

declare(strict_types=1);

ob_start();
$init_modules = ['discovery', 'laravel'];
require '/opt/librenms/includes/init.php';
ob_clean();

function fail_adapter(string $message): never
{
    fwrite(STDERR, $message . PHP_EOL);
    exit(1);
}

$payload = json_decode(stream_get_contents(STDIN), true);
if (! is_array($payload) || ! isset($payload['operations']) || ! is_array($payload['operations'])) {
    fail_adapter('Expected a JSON object containing an operations array.');
}
if (($payload['protocol'] ?? null) !== 'lm-artifact') {
    fail_adapter('Only the lm-artifact protocol is supported.');
}

$results = [];

try {
    \Illuminate\Support\Facades\DB::transaction(function () use ($payload, &$results): void {
        foreach ($payload['operations'] as $operation) {
            $action = (string) ($operation['action'] ?? '');
            $sourceId = (string) ($operation['source_id'] ?? '');
            $linkId = isset($operation['link_id']) ? (int) $operation['link_id'] : 0;

            if ($action === 'delete') {
                if ($linkId > 0) {
                    $row = dbFetchRow('SELECT `id`, `protocol` FROM `links` WHERE `id` = ?', [$linkId]);
                    if ($row && $row['protocol'] !== 'lm-artifact') {
                        throw new RuntimeException("Refusing to delete non-owned link ID $linkId.");
                    }
                    if ($row) {
                        dbDelete('links', '`id` = ? AND `protocol` = ?', [$linkId, 'lm-artifact']);
                    }
                }
                continue;
            }

            if (! in_array($action, ['create', 'update'], true)) {
                throw new RuntimeException("Unsupported operation action: $action");
            }
            $values = $operation['values'] ?? null;
            if (! is_array($values)) {
                throw new RuntimeException("$sourceId is missing link values.");
            }

            $desiredCollision = dbFetchRow(
                'SELECT `id`, `protocol` FROM `links` WHERE `remote_hostname` = ? '
                . 'AND `local_port_id` = ? AND `protocol` = ? AND `remote_port` = ?',
                [
                    (string) ($values['remote_hostname'] ?? ''),
                    (int) ($values['local_port_id'] ?? 0),
                    'lm-artifact',
                    (string) ($values['remote_port'] ?? ''),
                ]
            );
            if ($desiredCollision && (int) $desiredCollision['id'] !== $linkId) {
                $collisionId = (int) $desiredCollision['id'];
                throw new RuntimeException(
                    "$sourceId natural key collides with unowned link ID $collisionId."
                );
            }

            if ($action === 'update' && $linkId > 0) {
                $row = dbFetchRow('SELECT `id`, `protocol` FROM `links` WHERE `id` = ?', [$linkId]);
                if ($row && $row['protocol'] !== 'lm-artifact') {
                    throw new RuntimeException("Refusing to replace non-owned link ID $linkId.");
                }
                if ($row) {
                    dbDelete('links', '`id` = ? AND `protocol` = ?', [$linkId, 'lm-artifact']);
                }
            }

            $localDeviceId = find_device_id(
                (string) ($values['local_hostname'] ?? ''),
                (string) ($values['local_management_ip'] ?? '')
            );
            if ($localDeviceId <= 0) {
                throw new RuntimeException("$sourceId local device did not resolve.");
            }
            $localPortId = find_port_id(
                (string) ($values['local_interface'] ?? ''),
                '',
                $localDeviceId
            );
            if ($localPortId <= 0) {
                throw new RuntimeException("$sourceId local interface did not resolve.");
            }

            $remoteHostname = (string) ($values['remote_hostname'] ?? '');
            $remoteDeviceId = find_device_id(
                $remoteHostname,
                (string) ($values['remote_management_ip'] ?? '')
            );
            $remotePort = (string) ($values['remote_port'] ?? '');
            $remotePortId = $remoteDeviceId > 0
                ? find_port_id($remotePort, '', $remoteDeviceId)
                : 0;

            ob_start();
            discover_link(
                $localPortId,
                'lm-artifact',
                $remotePortId,
                $remoteHostname,
                $remotePort,
                (string) ($values['remote_platform'] ?? ''),
                (string) ($values['remote_version'] ?? ''),
                $localDeviceId,
                $remoteDeviceId
            );
            ob_end_clean();

            $newId = (int) dbFetchCell(
                'SELECT `id` FROM `links` WHERE `remote_hostname` = ? '
                . 'AND `local_port_id` = ? AND `protocol` = ? AND `remote_port` = ?',
                [$remoteHostname, $localPortId, 'lm-artifact', $remotePort]
            );
            if ($newId <= 0) {
                throw new RuntimeException("$sourceId was not materialized.");
            }
            dbUpdate(['active' => 1], 'links', '`id` = ? AND `protocol` = ?', [$newId, 'lm-artifact']);
            $results[] = ['source_id' => $sourceId, 'link_id' => $newId];
        }
    });
} catch (Throwable $error) {
    fail_adapter($error->getMessage());
}

echo json_encode(['results' => $results], JSON_THROW_ON_ERROR) . PHP_EOL;
