@if (!$newMap)
    <div class="lm-section-heading"><h3>Shared manual devices @include('libremapper::help', ['helpLabel' => 'Manual devices', 'helpText' => 'External nodes and references to existing LibreNMS devices. These records are reused across maps; saving one does not create a device in LibreNMS.'])</h3><span class="lm-scope lm-scope-shared">Shared across maps</span></div>
    <p>Manage external nodes and references to existing LibreNMS devices. These records are shared across maps. New devices are included in this map; changes remain drafts until published.</p>
    <p>External devices use a shared default layer, or <a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'layout', 'subtab' => 'overrides']) }}">set device layers for this map in Layout → Overrides</a>. Automatic layout and hierarchy inference ignore saved layers.</p>
    @if ($manualLinksError)<div class="alert alert-warning">{{ $manualLinksError }}</div>@endif
    <p><a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'advanced', 'subtab' => 'files', 'file' => $manualCatalog['path']]) }}">Edit underlying file in Advanced</a></p>
    @php
        $editingDevice = collect($manualCatalog['devices'])->firstWhere('source_id', (string) request()->query('edit'));
        $deviceEditorOpen = $editingDevice || ($errors->any() && session()->hasOldInput('hostname'));
        $deviceValue = static fn ($field, $default = '') => old($field, $editingDevice[$field] ?? $default);
    @endphp
    <ul class="nav nav-pills lm-editor-tabs" role="tablist" aria-label="Manual device sections">
        <li class="{{ $deviceEditorOpen ? '' : 'active' }}"><a href="#manual-device-records" data-toggle="tab" role="tab" aria-controls="manual-device-records" aria-expanded="{{ $deviceEditorOpen ? 'false' : 'true' }}">Devices</a></li>
        <li class="{{ $deviceEditorOpen ? 'active' : '' }}"><a href="#manual-device-edit" data-toggle="tab" role="tab" aria-controls="manual-device-edit" aria-expanded="{{ $deviceEditorOpen ? 'true' : 'false' }}">{{ $editingDevice ? 'Edit device' : 'Add device' }}</a></li>
    </ul>
    <div class="tab-content">
    <div class="tab-pane {{ $deviceEditorOpen ? 'active' : '' }}" id="manual-device-edit" role="tabpanel">
    <form method="POST" action="{{ route('libremapper.manual-devices.save', ['map' => $selectedKey]) }}" id="manual-device-editor" class="lm-section lm-shared-section">
        @csrf @include('libremapper::manual-freshness')
        <input type="hidden" name="source_id" value="{{ $editingDevice['source_id'] ?? '' }}">
        <h4>{{ $editingDevice ? 'Edit device' : 'Add manual device' }}</h4>
        <div class="row">
            <div class="form-group col-md-4"><label for="manual-hostname">Name / hostname</label><input class="form-control" id="manual-hostname" name="hostname" value="{{ $deviceValue('hostname') }}" maxlength="255" required></div>
            @if ($editingDevice)<div class="form-group col-md-4"><label for="manual-association">LibreNMS association</label> @include('libremapper::help', ['helpLabel' => 'LibreNMS association', 'helpText' => 'Existing associations are retained until this record is migrated.'])<select class="form-control" id="manual-association" name="librenms_device_id">
                <option value="">External device</option>
                @foreach ($inventoryDevices as $device)<option value="{{ $device['device_id'] }}" @selected((string) $deviceValue('librenms_device_id') === (string) $device['device_id'])>{{ $device['sys_name'] ?: $device['hostname'] }} — {{ $device['hostname'] }} ({{ $device['device_id'] }})</option>@endforeach
            </select></div>@endif
            <div class="form-group col-md-4"><label for="manual-image">Image</label><input class="form-control" id="manual-image" name="image" value="{{ old('image', $editingDevice['attributes']['map_image'] ?? '') }}" list="manual-images"><datalist id="manual-images">@foreach ($nodeImages as $image)<option value="{{ $image }}">@endforeach</datalist></div>
            <div class="form-group col-md-4"><label for="manual-layer">Default layer (all maps)</label> @include('libremapper::help', ['helpLabel' => 'Shared default layer', 'helpText' => 'The default placement layer wherever this manual device is used. A map-specific layer override takes priority. Automatic layout and hierarchy inference ignore saved layers.'])<input class="form-control" type="number" step="any" id="manual-layer" name="layer" value="{{ is_scalar($deviceValue('layer', 0)) ? $deviceValue('layer', 0) : '' }}" required></div>
            <div class="form-group col-md-4"><label for="manual-parts">Short hostname components</label> @include('libremapper::help', ['helpLabel' => 'Shared hostname default', 'helpText' => 'A shared component count for this manual device when shortened names are displayed. Blank uses each map’s hostname default.'])<input class="form-control" type="number" id="manual-parts" name="short_hostname_parts" value="{{ $deviceValue('short_hostname_parts') }}" min="1" max="127" placeholder="Map default"></div>
        </div>
        <button class="btn btn-primary" type="submit" @disabled($configError)>{{ $editingDevice ? 'Save manual device' : 'Add manual device' }}</button>
        @if ($editingDevice)<a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'devices', 'subtab' => 'manual']) }}">Add another device</a>@endif
    </form>
    </div>
    <div class="tab-pane {{ $deviceEditorOpen ? '' : 'active' }}" id="manual-device-records" role="tabpanel">
    <p class="help-block">Other maps can acquire devices through scope or shared connections. Review affected maps before publishing shared edits.</p>
    <form method="POST" action="{{ route('libremapper.manual-devices.remove', ['map' => $selectedKey]) }}" id="remove-manual-devices">@csrf @method('DELETE') @include('libremapper::manual-freshness')
        <button class="btn btn-default btn-sm" type="submit">Delete selected devices from all maps</button>
    </form>
    <div class="table-responsive"><table class="table table-striped table-condensed lm-catalog-table" id="manual-devices">
        <thead><tr><th>Select</th><th>Device</th><th>Default layer</th><th>Membership</th><th>Usage and dependencies</th><th><span class="lm-scope lm-scope-map">This map</span></th><th><span class="lm-scope lm-scope-shared">Shared across maps</span></th></tr></thead><tbody>
        @foreach ($manualCatalog['devices'] as $device)
            @php
                $id = $device['source_id'];
                $included = in_array($id, $map['manual_device_inclusions'] ?? [], true);
                $excluded = in_array($id, $map['manual_device_exclusions'] ?? [], true);
                $usedMaps = collect($maps)->filter(fn ($item) => in_array($id, $item['manual_device_inclusions'] ?? [], true))->pluck('name')->all();
                $dependencies = collect($manualLinks)->filter(fn ($link) => $link['a']['device'] === $id || $link['b']['device'] === $id)->pluck('source_id')->all();
            @endphp
            <tr><td><input type="checkbox" name="source_ids[]" value="{{ $id }}" form="remove-manual-devices" aria-label="Select {{ $device['hostname'] ?? $id }} for deletion"></td><td>{{ $device['hostname'] ?? $id }}<br><small>{{ isset($device['librenms_device_id']) ? 'LibreNMS #'.$device['librenms_device_id'] : (($device['_association_id'] ?? null) ? 'Matches LibreNMS #'.$device['_association_id'] : (($device['_association_count'] ?? 0) > 1 ? 'Multiple LibreNMS matches' : 'External device')) }}</small></td>
                <td>{{ $device['layer'] ?? 'Automatic' }}</td>
                <td>{{ $excluded ? 'Excluded' : ($included ? 'Explicitly included' : 'Automatic through connections / scope') }}</td>
                <td>Explicit maps: {{ implode(', ', $usedMaps) ?: 'None' }}<br>Connections: {{ implode(', ', $dependencies) ?: 'None' }}</td>
                <td><div class="lm-action-group">
                    <form method="POST" action="{{ route('libremapper.manual-devices.inclusion', ['map' => $selectedKey]) }}">@csrf @method('PUT') @include('libremapper::manual-freshness')
                        <input type="hidden" name="source_id" value="{{ $id }}"><input type="hidden" name="included" value="{{ $excluded || !$included ? 1 : 0 }}">
                        <button class="btn btn-default btn-xs" type="submit">{{ $excluded || !$included ? 'Include in this map' : 'Exclude from this map' }}</button>
                    </form>
                    @if (!$included && !$excluded)<form method="POST" action="{{ route('libremapper.manual-devices.inclusion', ['map' => $selectedKey]) }}">@csrf @method('PUT') @include('libremapper::manual-freshness')<input type="hidden" name="source_id" value="{{ $id }}"><input type="hidden" name="included" value="0"><button class="btn btn-default btn-xs" type="submit">Exclude from this map</button></form>@endif
                    </div></td><td><div class="lm-action-group"><a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'devices', 'subtab' => 'manual', 'edit' => $id]) }}">Edit device</a>
                    <form method="POST" action="{{ route('libremapper.manual-devices.remove', ['map' => $selectedKey]) }}">@csrf @method('DELETE') @include('libremapper::manual-freshness')
                        <input type="hidden" name="source_id" value="{{ $id }}"><button class="btn btn-link btn-xs" type="submit">Delete device from all maps</button>
                    </form>
                    </div>
                </td>
            </tr>
        @endforeach
        </tbody>
    </table></div>
    </div></div>
    <script>$(function () { $('#manual-association').select2({theme:'bootstrap', width:'100%'}); });</script>
@else
    <p>Create or select a map in Devices → Scope first.</p>
@endif
