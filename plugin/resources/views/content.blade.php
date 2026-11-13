@php
    $advancedDisplayOpen = $errors->any();
    $advancedLayoutOpen = $errors->any();
    $advancedAppearanceOpen = $errors->any();
    $kindLabels = ['map' => 'Map', 'node' => 'Device', 'edge' => 'Link'];
    $actionLabels = ['create' => 'Add', 'adopt' => 'Take over', 'update' => 'Update', 'delete' => 'Remove', 'noop' => 'Unchanged'];
    $statusLabels = ['active' => 'Manual fallback active', 'corroborated' => 'Discovered with manual fallback available', 'parallel-covered' => 'Parallel discovered links covered by one fallback'];
    $planNodes = collect($plan['operations'] ?? [])->where('kind', 'node')->where('action', '!=', 'delete')->keyBy('generated_values.device_id');
    $planLayers = collect($plan['operations'] ?? [])->where('kind', 'node')->where('action', '!=', 'delete')->pluck('layer')->filter(fn ($layer) => is_int($layer))->unique()->sort()->values()->all();
    $ruleRows = [];
    $advancedRules = [];
    foreach ($source['selectors'] ?? [] as $index => $rule) {
        $row = \Libremapper\Plugin\Engine::workspaceRule($rule);
        if ($row) { $ruleRows[] = $row + ['index' => $index, 'layer' => $rule['layer'] ?? 0]; }
        else { $advancedRules[] = $rule; }
    }
    $ruleRows = old('rules', $ruleRows ?: [['field' => 'group', 'value' => '', 'index' => -1]]);
    $attention = count($plan['warnings'] ?? []) + count($plan['conflicts'] ?? []) + ($plan['isolated_device_count'] ?? 0);
    $current = $plan && $plan['changes'] === 0 && !$plan['conflicts'] && !$draft;
@endphp
@include('libremapper::workspace-style')
<div class="container-fluid" id="libremapper-settings">
    <h2>Libremapper</h2>
    <p>Choose devices, complete connections, and review your map before publishing to LibreNMS.</p>
    @if (session('status')) <div class="alert alert-success" role="status">{{ session('status') }}</div> @endif
    @if ($errors->any()) <div class="alert alert-danger" role="alert">{{ $errors->first() }}</div> @endif
    @if ($configError) <div class="alert alert-danger" role="alert">Configuration needs attention: {{ $configError }}. Open Advanced to repair it.</div> @endif
    <div class="well well-sm" id="map-header" style="position:sticky;top:50px;z-index:10">
        <form method="GET" action="{{ route('plugin.settings', ['plugin' => 'libremapper']) }}" class="form-inline">
            <input type="hidden" name="tab" value="{{ $activeTab }}"><input type="hidden" name="subtab" value="{{ $activeSubtab }}"><input type="hidden" name="section" value="{{ $oxidizedSection }}">
            <label for="map">Map</label>
            <select id="map" name="map" class="form-control">
                @foreach ($maps as $item) <option value="{{ $item['key'] }}" @selected($item['key'] === $selectedKey)>{{ $item['name'] }}</option> @endforeach
                <option value="__new__" @selected($newMap)>Create a map…</option>
            </select>
            <button class="btn btn-default" type="submit">Load</button>
            @if (!$newMap)
                <span>{{ $plan['device_count'] ?? '—' }} devices · {{ $plan['link_count'] ?? '—' }} links · {{ $attention }} need attention</span>
                <strong class="label {{ $current ? 'label-success' : 'label-warning' }}">{{ $current ? 'Published · current' : ($plan ? 'Draft · reviewed' : 'Draft · review required') }}</strong>
            @endif
        </form>
        @if (!$newMap)
            <form method="POST" action="{{ route('libremapper.plan', ['map' => $selectedKey]) }}" style="margin-top:8px">
                @csrf <button class="btn btn-primary" type="submit" @disabled($configError)>Review changes</button>
            </form>
        @endif
    </div>
    <div class="lm-scope-key" aria-label="Change scope">
        <span><span class="lm-scope lm-scope-map">This map</span> Selected map only</span>
        <span><span class="lm-scope lm-scope-shared">Shared across maps</span> Maps using the record</span>
        <span><span class="lm-scope lm-scope-preview">Preview only</span> Nothing saved</span>
        <span>Map edits are drafts until reviewed and published.</span>
    </div>
    <ul class="nav nav-tabs">
        @foreach (['devices' => 'Devices', 'links' => 'Links', 'layout' => 'Layout', 'review' => 'Review', 'advanced' => 'Advanced'] as $tab => $label)
            <li class="{{ $activeTab === $tab ? 'active' : '' }}"><a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => $tab]) }}">{{ $label }}</a></li>
        @endforeach
    </ul>
    @if (in_array($activeTab, ['devices', 'links', 'layout', 'advanced'], true))
    <ul class="nav nav-pills workspace-subtabs" style="margin-top:12px">
        @foreach (match ($activeTab) {
            'advanced' => ['configuration' => 'Map configuration', 'appearance' => 'Appearance', 'oxidized' => 'Oxidized connection', 'parsers' => 'Parser catalog', 'agent-access' => 'Agent access', 'files' => 'Files', 'maintenance' => 'Maintenance'],
            'links' => ['connections' => 'Connections', 'parsers' => 'Parsers', 'manual' => 'Manual'],
            'layout' => ['topology' => 'Topology', 'overrides' => 'Overrides'],
            default => ['inventory' => 'Scope', 'matched' => 'Matched devices', 'manual' => 'Manual'],
        } as $subtab => $label)
            <li class="{{ $activeSubtab === $subtab ? 'active' : '' }}"><a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => $activeTab, 'subtab' => $subtab]) }}">{{ $label }}</a></li>
        @endforeach
    </ul>
    @endif
    <div style="padding-top:16px">
    @if (!$configError && !$inventoryError && ($activeTab === 'devices' || ($activeTab === 'links' && $activeSubtab !== 'parsers') || ($activeTab === 'layout' && $activeSubtab === 'overrides' && !$newMap)))
        <p class="text-muted">Inventory updates may take up to 15 seconds to appear.</p>
    @endif
    @if ($activeTab === 'devices' && $activeSubtab === 'manual')
        @include('libremapper::manual-devices')
    @elseif ($activeTab === 'devices' && $activeSubtab === 'matched' && !$newMap)
        @if ($inventoryError) <div class="alert alert-warning">{{ $inventoryError }}</div> @endif
        @include('libremapper::devices')
    @elseif ($activeTab === 'devices')
        @if ($newMap) <h3>{{ $maps ? 'Create a map' : 'Create your first map' }}</h3> @endif
        @if ($inventoryError) <div class="alert alert-warning">{{ $inventoryError }}</div> @endif
        @if ($newMap && !$canCreateGuidedMap)
            <p>Configure a map directory in Advanced to create maps.</p>
        @else
            <form method="POST" action="{{ route('libremapper.config.update') }}" id="device-scope-form" class="lm-section lm-map-section">
                @csrf @method('PUT')
                <input type="hidden" name="original_key" value="{{ $selectedKey }}"><input type="hidden" name="map_hash" value="{{ hash('sha256', json_encode($map, JSON_THROW_ON_ERROR)) }}">
                @if ($newMap)
                    <div class="form-group"><label for="name">Map name</label><input id="name" name="name" class="form-control" value="{{ old('name') }}" required></div>
                    @if (!in_array('librenms', $librenmsProviders, true) && count($librenmsProviders) !== 1)
                        <details open><summary>Advanced: discovery source</summary><label for="provider">LibreNMS source</label><select id="provider" name="provider" class="form-control" required>
                        <option value="">Choose a source</option>@foreach ($librenmsProviders as $provider)<option value="{{ $provider }}">{{ $provider }}</option>@endforeach
                        </select></details>
                    @endif
                @endif
                <div class="lm-section-heading"><h4>Device scope @include('libremapper::help', ['helpLabel' => 'Device scope', 'helpText' => 'Scope rules choose which LibreNMS devices belong to this map. Matching any rule includes a device; an individual exclusion takes priority.'])</h4><span class="lm-scope lm-scope-map">This map</span></div>
                <p>Match any rule. The first matching rule supplies its layer when saved layers are in use.</p>
                <div id="device-rules">
                    @foreach ($ruleRows as $i => $row)
                        <div class="row device-rule">
                            <input class="rule-index" type="hidden" name="rules[{{ $i }}][index]" value="{{ $row['index'] ?? -1 }}">
                            <div class="form-group col-md-4"><label>Match</label><select class="form-control rule-field" aria-label="Device rule field" name="rules[{{ $i }}][field]">
                                @foreach (['group' => 'Exact group', 'exact_location' => 'Exact location', 'hostname' => 'Hostname matches regex', 'sys_name' => 'sysName matches regex', 'location' => 'Location matches regex', 'os' => 'OS matches regex', 'hardware' => 'Hardware matches regex', 'type' => 'Device type matches regex'] as $field => $label)
                                    <option value="{{ $field }}" @selected(($row['field'] ?? '') === $field)>{{ $label }}</option>
                                @endforeach
                            </select></div>
                            <div class="form-group col-md-4"><label>Value</label><input class="form-control rule-value" aria-label="Device rule value" name="rules[{{ $i }}][value]" value="{{ $row['value'] ?? '' }}"><select class="form-control rule-choice" aria-label="Group or location"></select></div>
                            {{-- Integer validation runs on save; step="1" would use rejected decimals as a new step base. --}}
                            <div class="form-group col-md-2"><label>Layer</label> @include('libremapper::help', ['helpLabel' => 'Scope layer', 'helpText' => 'A saved placement value for devices matching this rule. Lower numbers form earlier tiers. Automatic layout and hierarchy inference ignore this value.'])<input class="form-control rule-layer" type="number" step="any" aria-label="Device rule layer" name="rules[{{ $i }}][layer]" value="{{ is_scalar($row['layer'] ?? 0) ? ($row['layer'] ?? 0) : '' }}" required></div>
                            <div class="col-md-2"><button class="btn btn-link remove-rule" type="button">Remove rule</button></div>
                        </div>
                    @endforeach
                </div>
                <button class="btn btn-default" id="add-rule" type="button">Add rule</button>
                <button class="btn btn-primary" type="submit" @disabled($configError || $inventoryError)>{{ $newMap ? 'Create map' : 'Save device scope' }}</button>
                @if ($advancedRules)<p>{{ count($advancedRules) }} Advanced rules or exact inclusions are preserved. <a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'advanced']) }}">Inspect in Advanced</a></p>@endif
            </form>
        @endif
    @elseif ($activeTab === 'links' && $activeSubtab === 'parsers')
        @include('libremapper::oxidized')
    @elseif ($activeTab === 'links')
        @include('libremapper::links')
    @elseif ($activeTab === 'layout' && !$newMap)
        @if ($activeSubtab === 'overrides')
        @include('libremapper::devices', ['placementMode' => true])
        @else
        <form method="POST" action="{{ route('libremapper.config.update') }}" class="lm-section lm-map-section" id="map-layout-form">
            @csrf @method('PUT') <input type="hidden" name="original_key" value="{{ $selectedKey }}"><input type="hidden" name="map_hash" value="{{ hash('sha256', json_encode($map, JSON_THROW_ON_ERROR)) }}"><input type="hidden" name="tab" value="layout"><input type="hidden" name="subtab" value="topology">
            <div class="lm-section-heading"><h4>Map layout and label defaults</h4><span class="lm-scope lm-scope-map">This map</span></div>
            <div class="row"><div class="form-group col-md-6"><label for="layout_type">Layout</label> @include('libremapper::help', ['helpLabel' => 'Layout', 'helpText' => 'Automatic calculates placement from connections. To use saved scope and device layers, choose another layout and turn hierarchy inference off.'])<select class="form-control" id="layout_type" name="layout_type">
                @foreach (['auto' => 'Automatic', 'top' => 'Top-down', 'left' => 'Left-to-right', 'radial' => 'Radial', 'force' => 'Organic'] as $value => $label)<option value="{{ $value }}" @selected(data_get($map, 'layout.type', 'force') === $value)>{{ $label }}</option>@endforeach
            </select></div><div class="form-group col-md-6">
            <input type="hidden" name="layout_infer_layers" value="0"><div class="checkbox"><label><input type="checkbox" name="layout_infer_layers" value="1" @checked(data_get($map, 'layout.infer_layers', false))> Infer hierarchy from connections</label> @include('libremapper::help', ['helpLabel' => 'Hierarchy inference', 'helpText' => 'When enabled, connection-based tiers take priority over all saved scope, individual-device, and shared manual layers. Saved values are retained.'])</div>
            </div></div>
            <div class="row">@include('libremapper::node-labels')</div>
            <button class="btn btn-primary" type="submit">Save layout</button>
        </form>
        <p>Automatic layout and hierarchy inference calculate placement from connections, ignoring saved scope, device, and manual-catalog layers. To use saved layers, choose a manual layered layout and turn inference off. Use Review options to reposition nodes edited in LibreNMS.</p>
        @include('libremapper::preview')
        @endif
    @elseif ($activeTab === 'review' && !$newMap)
        @include('libremapper::review')
    @elseif ($activeTab === 'advanced' && $activeSubtab === 'parsers')
        @include('libremapper::parsers')
    @elseif ($activeTab === 'advanced' && $activeSubtab === 'oxidized')
        @include('libremapper::oxidized-connection')
    @elseif ($activeTab === 'advanced' && $activeSubtab === 'agent-access')
        @include('libremapper::agent-access')
    @elseif ($activeTab === 'advanced')
        @include('libremapper::advanced')
    @else
        <p>Create or select a map in Devices first.</p>
    @endif
    </div>
</div>
<script>
$(function () {
    if ($.fn.tooltip) $('#libremapper-settings').tooltip({selector:'.lm-help', container:'body', trigger:'hover focus', html:false});
    var groups = @json(array_column($inventoryGroups, 'name'));
    var locations = @json(array_column($inventoryLocations, 'location'));
    function ruleChoice(row) {
        var field = row.find('.rule-field').val(), input = row.find('.rule-value'), choice = row.find('.rule-choice');
        var exact = field === 'group' || field === 'exact_location';
        input.toggle(!exact); choice.toggle(exact).empty();
        if (exact) {
            choice.append(new Option('Choose…', ''));
            (field === 'group' ? groups : locations).forEach(function (value) { choice.append(new Option(value, value)); });
            choice.val(input.val());
        }
    }
    $('.device-rule').each(function () { ruleChoice($(this)); });
    $('#device-rules').on('change', '.rule-field', function () { ruleChoice($(this).closest('.device-rule')); });
    $('#device-rules').on('change', '.rule-choice', function () { $(this).siblings('.rule-value').val($(this).val()); });
    function renameRules() { $('.device-rule').each(function (i) { $(this).find('[name]').each(function () { this.name = this.name.replace(/rules\[\d+\]/, 'rules[' + i + ']'); }); }); }
    $('#add-rule').on('click', function () { var row = $('.device-rule').first().clone(); row.find('input').val(''); row.find('.rule-index').val('-1'); row.find('.rule-layer').val('0'); $('#device-rules').append(row); renameRules(); ruleChoice(row); });
    $('#device-rules').on('click', '.remove-rule', function () { if ($('.device-rule').length > 1) $(this).closest('.device-rule').remove(); else $(this).closest('.device-rule').find('.rule-value').val(''); renameRules(); });
    function showOxidizedFields() { var mode = $('#oxidized_mode').val(); $('#oxidized_remote_fields, #oxidized_remote_help').prop('hidden', mode !== 'gitea'); $('#oxidized_local_help').prop('hidden', mode !== 'local'); $('#oxidized_repository').prop('required', mode === 'gitea'); }
    $('#oxidized_mode').on('change', showOxidizedFields); showOxidizedFields();
});
</script>
