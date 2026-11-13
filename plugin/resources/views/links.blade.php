@if (!$newMap)
    @if ($activeSubtab === 'manual')
    @php
        $editingLink = collect($manualLinks)->firstWhere('source_id', (string) request()->query('edit'));
        $linkEditorOpen = $editingLink || ($errors->any() && session()->hasOldInput('a_device_id'));
    @endphp
    <ul class="nav nav-pills lm-editor-tabs" role="tablist" aria-label="Manual connection sections">
        <li class="{{ $linkEditorOpen ? '' : 'active' }}"><a href="#manual-link-records" data-toggle="tab" role="tab" aria-controls="manual-link-records" aria-expanded="{{ $linkEditorOpen ? 'false' : 'true' }}">Connections</a></li>
        <li class="{{ $linkEditorOpen ? 'active' : '' }}"><a href="#manual-link-edit" data-toggle="tab" role="tab" aria-controls="manual-link-edit" aria-expanded="{{ $linkEditorOpen ? 'true' : 'false' }}">{{ $editingLink ? 'Edit connection' : 'Add connection' }}</a></li>
    </ul>
    <div class="tab-content"><div class="tab-pane {{ $linkEditorOpen ? 'active' : '' }}" id="manual-link-edit" role="tabpanel">
    <div class="lm-section-heading"><h3>{{ request()->query('edit') ? 'Edit connection' : 'Add missing connection' }}</h3><span class="lm-scope lm-scope-shared">Shared across maps</span></div>
    <p>Manual connections describe network-wide relationships. Each map can hide them. Save drafts here, then review changes before publishing.</p>
    @include('libremapper::link-form')
    <p><a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'advanced', 'subtab' => 'files', 'file' => $manualCatalog['path']]) }}">Edit underlying file in Advanced</a></p>
    </div><div class="tab-pane {{ $linkEditorOpen ? '' : 'active' }}" id="manual-link-records" role="tabpanel">
    @endif
    @if ($activeSubtab === 'connections')<div class="lm-section-heading"><h4>Connections @include('libremapper::help', ['helpLabel' => 'Reviewed connections', 'helpText' => 'The merged topology for the selected map. Discovered connections and manual fallbacks can describe the same relationship. Traffic-interface choices apply to this map only.'])</h4><span class="lm-scope lm-scope-map">This map</span></div>@endif
    @if (!$plan)<p>Review changes to inspect discovered relationships.</p>@endif
    @if ($manualLinksError)<div class="alert alert-warning">{{ $manualLinksError }}</div>@endif
    @php
        $fallbacks = collect($plan['link_fallbacks'] ?? []);
        $sourceLabel = static fn (string $name): string => $name === $manualProvider ? 'Manual' : (($providers[$name]['type'] ?? '') === 'librenms' ? 'LibreNMS' : (($providers[$name]['type'] ?? '') === 'oxidized' ? 'Oxidized' : 'Advanced'));
        $advancedUrl = route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'advanced']);
        $linksUrl = route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'links', 'subtab' => 'manual']);
        $manualRecordId = static fn (string $id): string => 'manual-record-'.hash('sha256', $id);
        $hiddenManualLinks = collect($manualLinks)->filter(fn ($link) => in_array($link['source_id'], $map['manual_link_exclusions'] ?? [], true));
    @endphp
    <p><a href="{{ $linksUrl }}#manual-connections">View manual records ({{ count($manualLinks) }})</a>. These shared records describe network-wide connections.</p>
    @if ($activeSubtab === 'connections' && $plan && array_key_exists('link_choices', $plan) && (!empty($plan['link_choices']) || $hiddenManualLinks->isNotEmpty()))
    <p>Select the connections to include in this map. Discovered connections with one missing endpoint interface are excluded by default. Save your selection, then review changes before publishing.</p>
    <form method="POST" action="{{ route('libremapper.links.selection', ['map' => $selectedKey]) }}" id="link-selection">@csrf @method('PUT')
        <input type="hidden" name="map_hash" value="{{ hash('sha256', json_encode($map, JSON_THROW_ON_ERROR)) }}">
        <input type="hidden" name="plan_sha256" value="{{ $plan['plan_sha256'] }}">
        <button class="btn btn-primary btn-sm" type="submit">Save connection selection</button>
    </form>
    @elseif ($activeSubtab === 'connections' && $plan && !array_key_exists('link_choices', $plan))
        <p>Review changes again to select connections.</p>
    @endif
    @if ($activeSubtab === 'manual')
    <form method="POST" action="{{ route('libremapper.manual-links.remove', ['map' => $selectedKey]) }}" id="remove-manual-links">@csrf @method('DELETE') @include('libremapper::manual-freshness')
        <button class="btn btn-default btn-sm" type="submit">Remove selected connections from all maps</button>
    </form>
    @endif
    <div class="table-responsive" id="plan-links"><table class="table table-condensed table-striped lm-catalog-table">
        @if ($activeSubtab === 'connections')
        <thead><tr><th>Include</th><th>Endpoints</th><th>Source</th><th>Status @include('libremapper::help', ['helpLabel' => 'Connection status', 'helpText' => 'An active manual fallback supplies a missing connection. Corroborated means discovery already supplies the relationship. Parallel coverage uses one manual fallback for multiple discovered links.'])</th><th>Traffic interface @include('libremapper::help', ['helpLabel' => 'Traffic counters', 'helpText' => 'The real LibreNMS interface supplying the graph for this map. Automatic uses a valid endpoint interface; an override selects a port explicitly.'])</th><th>Action</th></tr></thead><tbody>
        @foreach ($plan['link_choices'] ?? [] as $choice)
                @php
                    $operation = collect($plan['operations'])->first(fn ($item) => $item['kind'] === 'edge' && $item['action'] !== 'delete' && $item['canonical_id'] === $choice['canonical_id']);
                    $coverage = $fallbacks->first(fn ($fallback) => in_array($choice['canonical_id'], $fallback['canonical_ids'], true));
                    $discovered = collect($choice['sources'])->contains(fn ($ref) => in_array($sourceLabel($ref['provider']), ['LibreNMS', 'Oxidized'], true));
                @endphp
                <tr>
                    <td><input type="hidden" name="link_ids[]" value="{{ $choice['canonical_id'] }}" form="link-selection"><input type="checkbox" name="included_ids[]" value="{{ $choice['canonical_id'] }}" form="link-selection" aria-label="Include {{ $choice['display_name'] }} in this map" @checked($choice['included'])></td>
                    <td><strong>{{ $choice['display_name'] }}</strong><br>{{ $choice['a_interface'] ?? 'No specific interface' }} ↔ {{ $choice['b_interface'] ?? 'No specific interface' }}</td>
                    <td>
                        @foreach (collect($choice['sources'])->unique(fn ($ref) => $sourceLabel($ref['provider']) === 'Manual' ? $ref['provider'].':'.$ref['source_id'] : $ref['provider']) as $ref)
                            @if (!$loop->first)<br>@endif
                            @if ($sourceLabel($ref['provider']) === 'Oxidized')
                                <a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'links', 'subtab' => 'parsers']) }}">Links → Parsers</a>
                            @elseif ($sourceLabel($ref['provider']) === 'Manual')
                                <a href="{{ $linksUrl }}#{{ $manualRecordId($ref['source_id']) }}">Manual · view record</a>
                            @else
                                {{ $sourceLabel($ref['provider']) }}
                            @endif
                        @endforeach
                    </td>
                    <td>{{ !$choice['included'] ? ($choice['one_sided'] ? 'Excluded · missing endpoint interface' : 'Excluded from this map') : ($coverage ? $statusLabels[$coverage['status']] : (collect($choice['sources'])->contains(fn ($ref) => $sourceLabel($ref['provider']) === 'Manual') ? 'Manual connection' : ($discovered ? 'Discovered' : 'Incomplete or unresolved'))) }}</td>
                    <td>
                        @if ($operation)
                        {{ ucfirst($operation['graph']['mode'] ?? 'automatic') }} · {{ $operation['graph']['device'] ?? '' }} {{ $operation['graph']['interface'] ?? 'No counters' }}
                            @if (request()->query('graph') === $operation['canonical_id'])
                                @include('libremapper::graph-form')
                            @else
                                <a class="btn btn-link btn-xs" href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'links', 'graph' => $operation['canonical_id']]) }}">Traffic interface</a>
                            @endif
                        @endif
                    </td>
                    <td>{{ $operation ? $actionLabels[$operation['action']] : 'Excluded' }}
                        @if (collect($choice['sources'])->contains(fn ($ref) => $sourceLabel($ref['provider']) === 'Advanced'))<br><a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'advanced']) }}">Edit read-only source in Advanced</a>@endif
                    </td>
                </tr>
        @endforeach
        @foreach (($plan && array_key_exists('link_choices', $plan)) ? $hiddenManualLinks : [] as $hiddenLink)
            <tr><td><input type="checkbox" name="manual_included_ids[]" value="{{ $hiddenLink['source_id'] }}" form="link-selection" aria-label="Include {{ $hiddenLink['display_name'] }} in this map"></td>
                <td><strong>{{ $hiddenLink['display_name'] }}</strong><br>{{ $hiddenLink['a_endpoint']['name'] ?? 'No specific interface' }} ↔ {{ $hiddenLink['b_endpoint']['name'] ?? 'No specific interface' }}</td>
                <td><a href="{{ $linksUrl }}#{{ $manualRecordId($hiddenLink['source_id']) }}">Manual · view record</a></td><td>Excluded from this map</td><td></td><td>Excluded</td></tr>
        @endforeach
        </tbody>
        @else
        <thead><tr><th>Select</th><th>Endpoints</th><th>Status and usage</th><th><span class="lm-scope lm-scope-map">This map</span></th><th><span class="lm-scope lm-scope-shared">Shared across maps</span></th></tr></thead>
        <tbody id="manual-connections">
        <tr><th colspan="5" scope="colgroup">Manual records · {{ count($manualLinks) }} network-wide connections</th></tr>
        @if (!$manualLinks)<tr><td colspan="5">No manual records. Use Add missing connection to create one.</td></tr>@endif
        @foreach ($manualLinks as $manualLink)
            @php
                $included = !in_array($manualLink['source_id'], $map['manual_link_exclusions'] ?? [], true);
                $coverage = $fallbacks->first(fn ($fallback) => $fallback['source']['provider'] === $manualProvider && $fallback['source']['source_id'] === $manualLink['source_id']);
                $first = collect($inventoryDevices)->firstWhere('device_id', $manualLink['a_device_id']);
                $second = collect($inventoryDevices)->firstWhere('device_id', $manualLink['b_device_id']);
            @endphp
            <tr id="{{ $manualRecordId($manualLink['source_id']) }}"><td>@if ($manualLink['editable'])<input type="checkbox" name="source_ids[]" value="{{ $manualLink['source_id'] }}" form="remove-manual-links" aria-label="Select {{ $manualLink['display_name'] }} for removal">@endif</td><td>{{ $first && $second ? ($first['sys_name'] ?: $first['hostname']).' ↔ '.($second['sys_name'] ?: $second['hostname']) : $manualLink['display_name'] }}<br>{{ $manualLink['a_endpoint']['name'] ?? 'No specific interface' }} ↔ {{ $manualLink['b_endpoint']['name'] ?? 'No specific interface' }}</td>
                <td>{{ !$included ? 'Hidden in this map' : ($coverage ? $statusLabels[$coverage['status']] : 'Incomplete or unresolved / review required') }}<br><small>Used by reviewed maps: {{ implode(', ', $manualLink['used_maps'] ?? []) ?: 'Review required' }}</small></td>
                <td>
                @if ($manualLink['editable'])
                    <div class="lm-action-group">
                    <form method="POST" action="{{ route('libremapper.manual-links.inclusion', ['map' => $selectedKey]) }}">
                        @csrf @include('libremapper::manual-freshness') @method('PUT') <input type="hidden" name="source_id" value="{{ $manualLink['source_id'] }}"><input type="hidden" name="included" value="{{ $included ? 0 : 1 }}"><button class="btn btn-default btn-xs" type="submit">{{ $included ? 'Hide in this map' : 'Show in this map' }}</button>
                    </form>
                    </div></td><td><div class="lm-action-group">
                    <a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'links', 'subtab' => 'manual', 'edit' => $manualLink['source_id']]) }}">Edit connection</a>
                    <form method="POST" action="{{ route('libremapper.manual-links.remove', ['map' => $selectedKey]) }}">
                        @csrf @include('libremapper::manual-freshness') @method('DELETE') <input type="hidden" name="source_id" value="{{ $manualLink['source_id'] }}"><button class="btn btn-link btn-xs" type="submit">Remove from all maps</button>
                    </form>
                    </div>
                @else
                    —</td><td>
                    <a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'advanced']) }}">Read-only · edit in Advanced</a>
                @endif
                </td>
            </tr>
        @endforeach
    </tbody>
    @endif
    </table></div>
    @if ($activeSubtab === 'manual')</div></div>@endif
    <script>
    $(function () {
        var interfaceUrl = @json(route('libremapper.interfaces', ['device' => 0]));
        $('.connection-endpoint').each(function () {
            var endpoint = $(this), device = endpoint.find('.endpoint-device'), ports = endpoint.find('.endpoint-interface'), choices = [], request = null;
            var deviceChoices = device.find('option').clone();
            function devices() {
                var value = device.val(), all = endpoint.find('.endpoint-all-devices').prop('checked');
                if (device.hasClass('select2-hidden-accessible')) device.select2('destroy');
                device.empty(); deviceChoices.each(function () { if (!this.value || all || $(this).data('in-map') === 1 || this.value === 'unknown' || this.value === value) device.append($(this).clone()); }); device.val(value);
                device.select2({theme:'bootstrap', width:'100%'});
            }
            function updateSubmit() { endpoint.closest('form').find('button[type="submit"]').prop('disabled', Boolean(endpoint.closest('form').data('blocked')) || endpoint.closest('form').find('.connection-endpoint').toArray().some(function (item) { return $(item).data('loading') || $(item).data('failed'); })); }
            function custom() { endpoint.find('.custom-label').toggle(ports.val() === 'custom'); }
            function renderPorts(selected) {
                var all = endpoint.find('.endpoint-all-interfaces').prop('checked');
                ports.empty().append(new Option('No specific interface', '')).append(new Option('Custom label…', 'custom'));
                var fileInterfaces = device.find(':selected').data('interfaces') || [];
                fileInterfaces.forEach(function (item) { ports.append(new Option(item.name || item.source_id, 'file:' + item.source_id)); });
                choices.forEach(function (port) { if (!port.deleted && (port.usable || all || String(port.port_id) === selected)) ports.append(new Option(port.text + (port.usable ? '' : ' — show all'), port.port_id)); });
                var selectedFile = fileInterfaces.find(function (item) { return 'file:' + item.source_id === selected; });
                var selectedPort = selectedFile ? (selectedFile.attributes && selectedFile.attributes.manual_label_only ? '' : String(selectedFile.librenms_port_id || '')) : selected;
                if (selectedPort && selectedPort !== 'custom' && !selectedPort.startsWith('file:') && !choices.some(function (port) { return String(port.port_id) === selectedPort && !port.deleted; })) {
                    if (!ports.find('option').toArray().some(function (option) { return option.value === selected; })) ports.append(new Option('Unavailable port ID ' + selected + ' — choose a replacement', selected));
                    endpoint.data('missing-port', selected);
                    endpoint.data('failed', true);
                    endpoint.find('.endpoint-error').text('The selected port is unavailable. Choose a replacement or explicitly select no specific interface.');
                }
                ports.val(selected || ''); custom();
            }
            function loadPorts(selected) {
                if (request) request.abort(); choices = []; endpoint.find('.endpoint-error').text(''); endpoint.data('failed', false); endpoint.data('loading', false); endpoint.removeData('missing-port');
                if (!selected || selected === 'custom') renderPorts(selected);
                endpoint.find('.remote-label').toggle(device.val() === 'unknown');
                if (!device.find(':selected').data('device-id')) { ports.prop('disabled', false); renderPorts(selected); updateSubmit(); return; }
                var deviceValue = device.val(), associatedId = device.find(':selected').data('device-id'); ports.prop('disabled', true); endpoint.data('loading', true); updateSubmit();
                request = $.getJSON(interfaceUrl.replace('/0/interfaces', '/' + associatedId + '/interfaces')).done(function (data) { if (device.val() !== deviceValue) return; choices = data.interfaces; renderPorts(selected); }).fail(function (xhr, status) { if (status !== 'abort') { endpoint.data('failed', true); endpoint.find('.endpoint-error').text('Unable to load interfaces. Reload before saving this connection.'); } }).always(function () { if (device.val() === deviceValue) { ports.prop('disabled', false); endpoint.data('loading', false); updateSubmit(); } });
            }
            endpoint.find('.endpoint-all-devices').on('change', devices); devices();
            ports.select2({theme:'bootstrap', width:'100%'}).on('change', function () {
                custom();
                if (endpoint.data('missing-port') && String(ports.val()) !== endpoint.data('missing-port')) {
                    endpoint.removeData('missing-port'); endpoint.data('failed', false); endpoint.find('.endpoint-error').text(''); updateSubmit();
                }
            });
            device.on('change', function () { loadPorts(''); });
            endpoint.find('.endpoint-all-interfaces').on('change', function () { renderPorts(ports.val()); });
            loadPorts(String(ports.data('selected') || ''));
        });
    });
    </script>
@else
    <p>Create or select a map in Devices first.</p>
@endif
