@php
    $placementMode = $placementMode ?? false;
    $placementLayers = $planLayers ?: [0];
    $manualPlanNodes = [];
    foreach ($plan['operations'] ?? [] as $operation) {
        if ($operation['kind'] !== 'node' || $operation['action'] === 'delete') continue;
        foreach ($operation['sources'] ?? [] as $ref) {
            if ($ref['provider'] === $manualProvider) $manualPlanNodes[$ref['source_id']] = $operation;
        }
    }
    $matchRules = static function (array $device) use ($source): array {
        $matches = [];
        foreach ($source['selectors'] ?? [] as $i => $rule) {
            $type = $rule['type'] ?? '';
            $value = (string) ($rule['value'] ?? '');
            $field = $type === 'attribute' ? ($rule['attribute'] ?? '') : $type;
            $matched = $type === 'device_id' ? $value === (string) $device['device_id']
                : ($type === 'group' ? in_array($value, $device['groups'] ?? [], true)
                : (in_array($type, ['hostname', 'location', 'attribute'], true)
                    && @preg_match('~'.str_replace('~', '\\~', $value).'~u', (string) ($device[$field] ?? '')) === 1));
            if ($matched) $matches[] = $type === 'device_id' ? 'Individual inclusion' : 'Rule '.($i + 1);
        }
        return $matches;
    };
@endphp
<div class="lm-section-heading"><h4>{{ $placementMode ? 'Individual device overrides' : 'Matched devices' }}</h4><span class="lm-scope lm-scope-map">This map</span></div>
<p>{{ $plan ? 'The reviewed device list is shown alongside current inventory.' : 'Save your scope, then review changes to confirm the exact device list.' }}</p>
<div class="lm-device-filter">
    <div class="form-group"><label for="device-search">Search devices</label><input type="search" id="device-search" class="form-control device-search" placeholder="Filter the device list…"></div>
    <div class="checkbox"><label><input type="checkbox" id="show-all-devices"> Search all {{ $placementMode ? 'LibreNMS and manual' : 'LibreNMS' }} devices</label> @include('libremapper::help', ['helpLabel' => 'Search all devices', 'helpText' => 'Expands the list beyond the current map. Searching and selecting checkboxes do not change device membership.'])</div>
</div>
<div class="lm-bulk-controls">
<form method="POST" action="{{ route('libremapper.config.update') }}" id="hostname-label-form" class="lm-bulk-lane lm-hostname-lane">
    <fieldset><legend>Hostname labels @include('libremapper::help', ['helpLabel' => 'Hostname overrides', 'helpText' => 'Keep this many hostname components from the left when a short hostname is displayed. This changes labels on this map; it does not rename LibreNMS devices. Blank restores the map default.'])</legend>
    @csrf @method('PUT')
    <input type="hidden" name="original_key" value="{{ $selectedKey }}"><input type="hidden" name="map_hash" value="{{ hash('sha256', json_encode($map, JSON_THROW_ON_ERROR)) }}">
    <input type="hidden" name="tab" value="{{ $activeTab }}">
    <input type="hidden" name="subtab" value="{{ $activeSubtab }}">
    <div class="lm-bulk-value"><label for="hostname-parts">Short hostname components</label>
    <input class="form-control input-sm" type="number" min="1" max="127" step="1" id="hostname-parts" name="hostname_parts" value="{{ old('hostname_parts', '') }}" placeholder="Default" aria-describedby="hostname-selection-help"></div>
    <p class="help-block" id="hostname-selection-help">Use the Hostname labels checkboxes below. Blank restores the map default.</p>
    <div class="lm-selection-meta"><span id="hostname-selected-count"></span> <span class="lm-selection-hidden" id="hostname-hidden-count"></span></div>
    <div class="lm-impact" id="hostname-impact" role="status" aria-live="polite"></div>
    <div class="lm-lane-actions"><button class="btn btn-default btn-sm" type="submit" name="hostname_action" value="apply" aria-describedby="hostname-impact">Apply to selected devices</button>
    <button class="btn btn-link btn-sm" type="button" data-clear-selection="hostname">Clear hostname selection</button></div>
    </fieldset>
</form>
@if ($placementMode)
<form method="POST" action="{{ route('libremapper.config.update') }}" id="device-placement-form" class="lm-bulk-lane lm-layer-lane">
    <fieldset><legend>Device layers @include('libremapper::help', ['helpLabel' => 'Individual device layers', 'helpText' => 'A whole-number placement override for selected devices on this map. Lower numbers form earlier tiers. Blank restores scope or shared manual defaults. This selection is independent of hostname labels.'])</legend>
    @csrf @method('PUT')
    <input type="hidden" name="original_key" value="{{ $selectedKey }}"><input type="hidden" name="map_hash" value="{{ hash('sha256', json_encode($map, JSON_THROW_ON_ERROR)) }}">
    <input type="hidden" name="tab" value="layout">
    <input type="hidden" name="subtab" value="overrides">
    <input type="hidden" name="manual_sha256" value="{{ $manualCatalog['sha256'] }}">
    <div class="lm-bulk-value"><label for="device-layer">Layer value</label>
    {{-- Keep rejected decimals from becoming the browser's integer step base. --}}
    <input class="form-control input-sm" type="number" step="any" id="device-layer" name="placement" value="{{ is_scalar(old('placement', '')) ? old('placement', '') : '' }}" placeholder="Default" aria-describedby="placement-selection-help"></div>
    <p class="help-block" id="placement-selection-help">Use the Device layers checkboxes below. Blank restores scope or shared defaults.</p>
    <div class="lm-selection-meta"><span id="placement-selected-count"></span> <span class="lm-selection-hidden" id="placement-hidden-count"></span></div>
    <div class="lm-impact" id="placement-impact" role="status" aria-live="polite"></div>
    <div class="lm-lane-actions"><button class="btn btn-default btn-sm" type="submit" name="placement_action" value="apply" aria-describedby="placement-impact">Apply layer to selected devices</button>
    <button class="btn btn-link btn-sm" type="button" data-clear-selection="placement">Clear layer selection</button></div>
    <p class="lm-lane-note" id="placement-mode-note">@if (data_get($map, 'layout.type') === 'auto' || data_get($map, 'layout.infer_layers', false))<strong>Saved layers are currently ignored.</strong> Automatic layout or hierarchy inference is enabled. Your values are retained.@else<strong>Saved layers are in use.</strong> Individual overrides take priority over scope and shared defaults.@endif</p>
    </fieldset>
</form>
@endif
</div>
<p class="help-block">Check all applies only to visible results. Hidden selections stay selected and are included when you apply a value.</p>
<div class="table-responsive" id="device-list"><table class="table table-striped table-condensed">
<thead><tr>
@foreach (['Device', 'Location', 'Matching rule', 'Discovered links', 'Placement'] as $column => $label)
    <th scope="col" aria-sort="none"><button type="button" class="btn btn-link btn-xs device-sort" data-column="{{ $column }}" data-numeric="{{ $column >= 3 ? 1 : 0 }}">{{ $label }} <span class="sort-direction" aria-hidden="true"></span></button>@if ($column === 4)@include('libremapper::help', ['helpLabel' => 'Reviewed placement', 'helpText' => 'The tier from the last review, ordered by layer value. Saved scope and individual layers can differ from this tier when Automatic layout or hierarchy inference is enabled.'])@endif</th>
@endforeach
<th scope="col" class="lm-hostname-cell"><strong>Hostname labels</strong><br><label><input type="checkbox" id="hostname-check-all" data-selection="hostname"> Check all</label> @include('libremapper::help', ['helpLabel' => 'Check all hostname labels', 'helpText' => 'Select or clear only visible hostname checkboxes. Hidden selections are retained. This does not select device layers.'])</th>
<th scope="col" @if ($placementMode) class="lm-placement-cell" @endif>@if ($placementMode)<strong>Device layers</strong><br><label><input type="checkbox" id="placement-check-all" data-selection="placement"> Check all layers</label> @include('libremapper::help', ['helpLabel' => 'Check all device layers', 'helpText' => 'Select or clear only visible layer checkboxes. Hidden selections are retained. This does not select hostname labels.']) @else Membership @include('libremapper::help', ['helpLabel' => 'Device membership', 'helpText' => 'Include explicitly adds a device to this map; Exclude removes it and its connections. Automatic returns membership to scope and connection rules.']) @endif</th>
</tr></thead>
<tbody>
@foreach ($inventoryDevices as $device)
    @php
        $node = $planNodes->get($device['device_id']);
        $matching = $matchRules($device);
        $excluded = in_array($device['device_id'], $map['device_exclusions'] ?? [], true);
        $visible = $node || $matching || $excluded;
        $override = collect($map['layer_overrides'] ?? [])->first(fn ($rule) => ($rule['type'] ?? '') === 'device_id' && (int) $rule['value'] === $device['device_id']);
        $hostnameOverride = collect($map['short_hostname_overrides'] ?? [])->firstWhere('device_id', $device['device_id']);
    @endphp
    <tr data-device-id="{{ $device['device_id'] }}" data-in-scope="{{ $visible ? 1 : 0 }}" @if (!$visible) style="display:none" @endif>
        <td data-sort-value="{{ $device['sys_name'] ?: $device['hostname'] }}"><strong>{{ $device['sys_name'] ?: $device['hostname'] }}</strong><br><small>{{ $device['hostname'] }}</small></td>
        <td data-sort-value="{{ $device['location'] ?? '' }}">{{ $device['location'] ?: '—' }}</td>
        <td>{{ $excluded ? 'Excluded individually' : ($matching ? implode(', ', $matching) : ($node ? 'Manual connection or Advanced rule' : 'Outside scope')) }}</td>
        <td>{{ $node['discovered_link_count'] ?? $device['discovered_link_count'] ?? 0 }}</td>
        <td data-sort-value="{{ $node ? ($node['layer'] ?? 0) : '' }}">{{ $node ? (($rank = array_search($node['layer'] ?? 0, $placementLayers, true)) === 0 ? 'Core / first tier' : 'Tier '.($rank + 1)) : 'Automatic' }}</td>
        <td class="lm-hostname-cell"><label><input type="checkbox" class="hostname-device" name="hostname_device_ids[]" value="{{ $device['device_id'] }}" form="hostname-label-form" @checked(in_array((string) $device['device_id'], array_map('strval', (array) old('hostname_device_ids', [])), true))><span class="sr-only"> Select hostname label for {{ $device['hostname'] }}</span></label>
            {{ $hostnameOverride ? $hostnameOverride['parts'].' components' : 'Default' }}</td>
        <td class="{{ $placementMode ? 'lm-placement-cell' : '' }}">
            @if ($placementMode)
                <label><input type="checkbox" class="placement-device" name="placement_device_ids[]" value="{{ $device['device_id'] }}" form="device-placement-form" @checked(in_array((string) $device['device_id'], array_map('strval', array_filter((array) old('placement_device_ids', []), 'is_scalar')), true))><span class="sr-only"> Select layer for {{ $device['hostname'] }}</span></label>
                {{ $override ? 'Layer '.$override['layer'] : 'Scope / default' }}
            @else
                <form method="POST" action="{{ route('libremapper.config.update') }}" class="form-inline">
                @csrf @method('PUT') <input type="hidden" name="original_key" value="{{ $selectedKey }}"><input type="hidden" name="map_hash" value="{{ hash('sha256', json_encode($map, JSON_THROW_ON_ERROR)) }}">
                <input type="hidden" name="subtab" value="matched">
                <input type="hidden" name="device_id" value="{{ $device['device_id'] }}">
                <button class="btn btn-default btn-xs" name="device_action" value="include">Include</button>
                <button class="btn btn-default btn-xs" name="device_action" value="exclude">Exclude</button>
                <button class="btn btn-link btn-xs" name="device_action" value="automatic">Automatic</button>
                </form>
            @endif
        </td>
    </tr>
@endforeach
@if ($placementMode)
@foreach ($manualCatalog['devices'] as $device)
    @continue(($device['_association_id'] ?? null) !== null)
    @php
        $id = $device['source_id'];
        $node = $manualPlanNodes[$id] ?? null;
        $included = in_array($id, $map['manual_device_inclusions'] ?? [], true);
        $excluded = in_array($id, $map['manual_device_exclusions'] ?? [], true);
        $visible = $node || $included || $excluded;
        $override = collect($map['layer_overrides'] ?? [])->first(fn ($rule) => ($rule['type'] ?? '') === 'canonical' && $rule['value'] === $manualProvider.':'.$id);
        $editUrl = route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'devices', 'subtab' => 'manual', 'edit' => $id]);
    @endphp
    <tr data-device-id="manual:{{ $id }}" data-manual-device-id="{{ $id }}" data-in-scope="{{ $visible ? 1 : 0 }}" @if (!$visible) style="display:none" @endif>
        <td data-sort-value="{{ $device['hostname'] ?? $id }}"><strong>{{ $device['hostname'] ?? $id }}</strong><br><small>Manual device</small></td>
        <td data-sort-value="{{ $device['attributes']['location'] ?? '' }}">{{ $device['attributes']['location'] ?? '—' }}</td>
        <td>{{ $excluded ? 'Excluded individually' : ($included ? 'Manual inclusion' : ($node ? 'Manual connection' : 'Outside scope')) }}</td>
        <td>{{ $node['discovered_link_count'] ?? 0 }}</td>
        <td data-sort-value="{{ $node ? ($node['layer'] ?? 0) : '' }}">{{ $node ? (($rank = array_search($node['layer'] ?? 0, $placementLayers, true)) === 0 ? 'Core / first tier' : 'Tier '.($rank + 1)) : 'Automatic' }}</td>
        <td class="lm-hostname-cell"><a href="{{ $editUrl }}">Edit shared hostname default</a></td>
        <td class="lm-placement-cell"><label><input type="checkbox" class="placement-device" name="placement_manual_device_ids[]" value="{{ $id }}" form="device-placement-form" @checked(in_array($id, (array) old('placement_manual_device_ids', []), true))><span class="sr-only"> Select layer for {{ $device['hostname'] ?? $id }}</span></label>
            {{ $override ? 'Layer '.$override['layer'] : 'Shared default: '.($device['layer'] ?? 'Automatic') }} · <a href="{{ $editUrl }}">Edit shared default</a></td>
    </tr>
@endforeach
@endif
</tbody></table></div>
<script>
$(function () {
    function updateSelections() {
        ['hostname', 'placement'].forEach(function (kind) {
            var visible = $('#device-list tbody tr:visible .' + kind + '-device'), checked = visible.filter(':checked').length, selected = $('#device-list .' + kind + '-device:checked').length;
            $('#' + kind + '-check-all').prop('checked', visible.length > 0 && checked === visible.length).prop('indeterminate', checked > 0 && checked < visible.length).prop('disabled', visible.length === 0);
            var form = $(kind === 'hostname' ? '#hostname-label-form' : '#device-placement-form'), hidden = selected - checked;
            $('#' + kind + '-selected-count').text(selected + ' selected');
            $('#' + kind + '-hidden-count').text(hidden ? '· ' + hidden + ' hidden by current filters' : '');
            var value = String(form.find('input[type="number"]').val() || '').trim();
            var action = kind === 'hostname' ? (value ? 'Set hostname components to ' + value : 'Restore map label defaults') : (value ? 'Save layer ' + value : 'Restore scope or shared layer defaults');
            $('#' + kind + '-impact').text(selected ? action + ' for ' + selected + (selected === 1 ? ' device' : ' devices') + (hidden ? ', including ' + hidden + ' hidden' : '') + '.' : 'Select devices in the matching column below.');
            form.find('button[type="submit"], [data-clear-selection]').prop('disabled', selected === 0);
            $('#device-list .' + kind + '-device').each(function () { $(this).closest('td').toggleClass('lm-selected', this.checked); });
        });
    }
    function filterDevices() { var term = $('#device-search').val().toLowerCase(), all = $('#show-all-devices').prop('checked'); $('#device-list tbody tr').each(function () { $(this).toggle((all || $(this).data('in-scope') === 1) && $(this).text().toLowerCase().includes(term)); }); updateSelections(); }
    $('#show-all-devices, #device-search').on('change input', filterDevices);
    $('#hostname-check-all, #placement-check-all').on('change', function () { $('#device-list tbody tr:visible .' + this.dataset.selection + '-device').prop('checked', this.checked); updateSelections(); });
    $('#device-list').on('change', '.hostname-device, .placement-device', updateSelections);
    $('#hostname-parts, #device-layer').on('input', updateSelections);
    $('[data-clear-selection]').on('click', function () { $('#device-list .' + this.dataset.clearSelection + '-device').prop('checked', false); updateSelections(); });
    filterDevices();
    var collator = new Intl.Collator('en', {numeric: true, sensitivity: 'base'});
    $('#device-list .device-sort').on('click', function () {
        var header = $(this).closest('th'), descending = header.attr('aria-sort') === 'ascending', column = Number(this.dataset.column), numeric = this.dataset.numeric === '1';
        var rows = $('#device-list tbody tr').get();
        rows.sort(function (a, b) {
            var first = a.cells[column], second = b.cells[column];
            var left = first.dataset.sortValue ?? first.textContent.trim(), right = second.dataset.sortValue ?? second.textContent.trim();
            var comparison = numeric ? Number(left) - Number(right) : collator.compare(left, right);
            if (numeric && (left === '' || right === '')) comparison = left === right ? 0 : (left === '' ? 1 : -1);
            else comparison *= descending ? -1 : 1;
            return comparison || collator.compare(a.dataset.deviceId, b.dataset.deviceId);
        });
        $('#device-list thead th[aria-sort]').attr('aria-sort', 'none').find('.sort-direction').text('');
        header.attr('aria-sort', descending ? 'descending' : 'ascending').find('.sort-direction').text(descending ? '▼' : '▲');
        $('#device-list tbody').append(rows);
        filterDevices();
    });
});
</script>
