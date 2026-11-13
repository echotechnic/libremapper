<?php

namespace Libremapper\Plugin;

use App\Models\Device;
use App\Models\User;
use Illuminate\Support\Facades\DB;

class NetworkInspection
{
    public static function devices(Engine $engine, User $user): array
    {
        $status = Device::hasAccess($user)->get(['device_id', 'status', 'status_reason', 'disabled', 'ignore', 'last_polled', 'last_discovered'])->keyBy('device_id');
        $rows = [];
        foreach ($engine->inventory()['devices'] as $device) {
            if (! isset($status[$device['device_id']])) continue;
            $rows[] = $device + $status[$device['device_id']]->only(['status', 'status_reason', 'disabled', 'ignore', 'last_polled', 'last_discovered']);
        }
        return $rows;
    }

    public static function neighbors(User $user, ?int $device): array
    {
        $accessible = Device::hasAccess($user)->pluck('device_id')->all();
        $query = DB::table('links')->whereIn('local_device_id', $accessible)
            ->where(fn ($q) => $q->whereIn('remote_device_id', $accessible)->orWhereNull('remote_device_id')->orWhere('remote_device_id', 0));
        if ($device !== null) $query->where(fn ($q) => $q->where('local_device_id', $device)->orWhere('remote_device_id', $device));
        return $query->orderBy('id')->get(['id', 'local_device_id', 'local_port_id', 'remote_device_id', 'remote_port_id', 'remote_hostname', 'remote_port', 'protocol', 'active'])->map(fn ($row) => (array) $row)->all();
    }
}
