@php
    $editingLink = $editingLink ?? null;
    $builderId = $editingLink ? 'edit-'.substr(hash('sha256', $editingLink['source_id']), 0, 12) : 'add';
    $useOld = session()->hasOldInput('a_device_id') && (string) old('source_id', '') === (string) ($editingLink['source_id'] ?? '');
    $inputValue = static fn (string $key, mixed $default): mixed => $useOld ? old($key, $default) : $default;
    $mapDeviceIds = $planNodes->keys()->all();
    $orderedDevices = collect($inventoryDevices)->sortBy(fn ($device) => (in_array($device['device_id'], $mapDeviceIds, true) ? '0' : '1').strtolower($device['sys_name'] ?: $device['hostname']));
@endphp
<form method="POST" action="{{ route('libremapper.connections.save', ['map' => $selectedKey]) }}" class="connection-form lm-section lm-shared-section" data-blocked="{{ $configError || $inventoryError ? 1 : 0 }}" id="connection-{{ $builderId }}">
    @csrf @include('libremapper::manual-freshness')
    @if ($editingLink)@method('PUT')<input type="hidden" name="source_id" value="{{ $editingLink['source_id'] }}">@endif
    <div class="row">
    @foreach (['a' => 'First endpoint', 'b' => 'Second endpoint'] as $side => $label)
        @php
            $endpoint = $editingLink[$side.'_endpoint'] ?? null;
            $deviceId = $inputValue($side.'_device_id', $editingLink[$side.'_choice'] ?? '');
            $portValue = $inputValue($side.'_interface', $endpoint ? (($endpoint['attributes']['manual_label_only'] ?? false) ? 'custom' : 'file:'.$endpoint['source_id']) : '');
        @endphp
        <fieldset class="col-md-6 connection-endpoint" data-side="{{ $side }}">
            <legend>{{ $label }}</legend>
            <label for="{{ $builderId }}-{{ $side }}-device">Device</label>
            <select class="form-control endpoint-device" id="{{ $builderId }}-{{ $side }}-device" name="{{ $side }}_device_id" required data-selected="{{ $deviceId }}">
                <option value="">Choose a device</option>
                @foreach ($orderedDevices as $device)<option value="{{ $device['device_id'] }}" data-device-id="{{ $device['device_id'] }}" data-interfaces="{{ json_encode((string) $deviceId === (string) $device['device_id'] && $endpoint ? [$endpoint] : []) }}" data-in-map="{{ in_array($device['device_id'], $mapDeviceIds, true) ? 1 : 0 }}" @selected((int) $deviceId === $device['device_id'])>{{ $device['sys_name'] ?: $device['hostname'] }} — {{ $device['hostname'] }}</option>@endforeach
                @foreach ($manualCatalog['devices'] as $manualDevice)<option value="manual:{{ $manualDevice['source_id'] }}" data-in-map="1" data-device-id="{{ $manualDevice['_association_id'] ?? $manualDevice['librenms_device_id'] ?? '' }}" data-interfaces="{{ json_encode($manualDevice['interfaces'] ?? []) }}" @selected($deviceId === 'manual:'.$manualDevice['source_id'])>Manual · {{ $manualDevice['hostname'] ?? $manualDevice['source_id'] }}</option>@endforeach
                <option value="unknown" @selected($deviceId === 'unknown')>Unknown remote device…</option>
            </select>
            <label class="remote-label" for="{{ $builderId }}-{{ $side }}-remote">Remote device name</label><input class="form-control remote-label" id="{{ $builderId }}-{{ $side }}-remote" name="{{ $side }}_remote_label" value="{{ $inputValue($side.'_remote_label', '') }}" maxlength="255">
            <label><input type="checkbox" class="endpoint-all-devices"> Search all LibreNMS devices</label><br>
            <label for="{{ $builderId }}-{{ $side }}-interface">Interface</label> @include('libremapper::help', ['helpLabel' => 'Endpoint interface', 'helpText' => 'Describes this endpoint in the shared connection. Real LibreNMS ports can provide counters. File-defined interfaces can carry names without counters; a custom label supplies text only.'])
            <select class="form-control endpoint-interface" id="{{ $builderId }}-{{ $side }}-interface" name="{{ $side }}_interface" data-selected="{{ $portValue }}">
                <option value="">No specific interface</option><option value="custom">Custom label…</option>
                @if ($portValue !== '' && $portValue !== 'custom')<option value="{{ $portValue }}" selected>{{ $endpoint['name'] ?? 'Selected interface' }}</option>@endif
            </select>
            <label><input type="checkbox" class="endpoint-all-interfaces" checked> Show all interfaces</label>
            <label class="custom-label" for="{{ $builderId }}-{{ $side }}-label">Custom label (without counters)</label>
            <input class="form-control custom-label" id="{{ $builderId }}-{{ $side }}-label" name="{{ $side }}_label" value="{{ $inputValue($side.'_label', ($endpoint['attributes']['manual_label_only'] ?? false) ? ($endpoint['name'] ?? '') : '') }}" maxlength="255">
            <p class="endpoint-error text-danger" role="alert"></p>
        </fieldset>
    @endforeach
    </div>
    <p>Real interfaces can supply traffic counters. Custom labels and unspecified interfaces document the endpoint only.</p>
    <button class="btn btn-primary" type="submit" @disabled($configError || $inventoryError)>{{ $editingLink ? 'Save connection' : 'Add missing connection' }}</button>
</form>
