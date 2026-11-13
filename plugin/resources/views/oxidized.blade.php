                <div id="oxidized">
    <ul class="nav nav-pills" aria-label="Parser sections">
        @foreach (['source' => 'Selection', 'report' => 'Report'] as $section => $label)
            <li class="{{ $oxidizedSection === $section ? 'active' : '' }}"><a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'links', 'subtab' => 'parsers', 'section' => $section]) }}">{{ $label }}</a></li>
        @endforeach
    </ul>
    @if ($parserCatalogError) <div class="alert alert-danger">{{ $parserCatalogError }}</div> @endif
    @if ($oxidizedSection === 'source')
                    @if (!$newMap)
                        <form method="POST" action="{{ route('libremapper.parsers.selection', ['map' => $selectedKey]) }}" class="lm-section lm-map-section" id="oxidized-map-source">
                            @csrf
                            <input type="hidden" name="map_hash" value="{{ hash('sha256', json_encode($map, JSON_THROW_ON_ERROR)) }}">
                            <input type="hidden" name="sha256" value="{{ $mainSha256 }}"><input type="hidden" name="parsers_present" value="1">
                            <div class="lm-section-heading"><h4>Parsers enabled for {{ $map['name'] }} @include('libremapper::help', ['helpLabel' => 'Parser selection', 'helpText' => 'These checkboxes enable existing definitions on the selected map only. Enabled parsers contribute connections together, then map scope is applied. Editing a global definition affects all maps using it.'])</h4><span class="lm-scope lm-scope-map">This map</span></div>
                            <p>Uncheck every parser to turn parsing off for this map. New parsers stay disabled until selected here.</p>
                            @php
                                $selectedMapParsers = old('parsers_present') ? old('parsers', []) : $mapParsers;
                                if (!is_array($selectedMapParsers)) $selectedMapParsers = [];
                            @endphp
                            @foreach ($parserCatalog['items'] as $parser)
                                <div class="checkbox lm-parser-choice"><label><input type="checkbox" name="parsers[]" value="{{ $parser['id'] }}" @checked(in_array($parser['id'], $selectedMapParsers, true))> {{ $parser['definition']['label'] }}</label>
                                    <a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'advanced', 'subtab' => 'parsers', 'edit' => $parser['id']]) }}">Edit global definition</a>
                                </div>
                            @endforeach
                            <button class="btn btn-primary" type="submit" @disabled($oxidizedProvider === null || $configError || $parserCatalogError)>Save parser selection</button>
                            @if (!$oxidizedProvider)<p><a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'advanced', 'subtab' => 'oxidized']) }}">Configure the shared Git source in Advanced → Oxidized connection</a> first.</p>@endif
                        </form>
                    @else
                        <p>Select or create a map to enable parsers.</p>
                    @endif
                    <p><a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'advanced', 'subtab' => 'parsers']) }}">Manage global parser catalog</a></p>
                    @else
                    <div class="panel panel-default">
                        <div class="panel-heading lm-section-heading"><strong>Parser report</strong><span class="lm-scope lm-scope-preview">Preview only</span></div>
                        <div class="panel-body">
                            @if (!$oxidizedProvider)
                                <div class="alert alert-info">Configure the shared source in Advanced → Oxidized connection to inspect this map’s selected parsers.</div>
                            @else
                                <p><strong>Map:</strong> {{ $map['name'] ?? 'Select a map' }}</p>
                                <p><strong>Enabled parsers:</strong> {{ implode(', ', array_map(fn ($id) => collect($parserCatalog['items'])->firstWhere('id', '===', $id)['definition']['label'] ?? $id, $mapParsers)) ?: 'None' }}</p>
                                @if (!$mapParsers)<div class="alert alert-info">Parsing is off for this map. No repository artifacts are read.</div>@endif
                                @if ($oxidizedInspectionError)
                                    <div class="alert alert-danger"><strong>Inspection failed:</strong> {{ $oxidizedInspectionError }}</div>
                                @elseif ($oxidizedInspection)
                                    @php
                                        $oxidizedTopology = $oxidizedInspection['topology'];
                                        $oxidizedDevices = $oxidizedTopology['devices'];
                                        $oxidizedLinks = $oxidizedTopology['links'];
                                        $oxidizedFailures = $oxidizedInspection['resolution_failures'];
                                        $oxidizedSkipped = $oxidizedInspection['skipped_files'];
                                        $oxidizedArtifactCount = count($oxidizedInspection['matched_files']);
                                        $oxidizedDeviceCount = count($oxidizedDevices);
                                        $oxidizedLinkCount = count($oxidizedLinks);
                                    @endphp
                                    <div class="alert {{ $oxidizedInspection['blocked'] ? 'alert-danger' : 'alert-success' }}">
                                        <strong>{{ $oxidizedInspection['blocked'] ? 'Parsing is blocked.' : 'Parsing succeeded.' }}</strong>
                                        {{ $oxidizedArtifactCount }} {{ $oxidizedArtifactCount === 1 ? 'artifact' : 'artifacts' }} matched,
                                        {{ $oxidizedDeviceCount }} {{ $oxidizedDeviceCount === 1 ? 'device' : 'devices' }} and
                                        {{ $oxidizedLinkCount }} {{ $oxidizedLinkCount === 1 ? 'connection' : 'connections' }} produced.
                                    </div>
                                    <p>
                                        <strong>Repository:</strong> <code>{{ $oxidizedRepository }}</code><br>
                                        <strong>Revision:</strong> <code>{{ $oxidizedInspection['revision'] ?? 'Unavailable' }}</code><br>

                                    </p>
                                    @if ($oxidizedInspection['parser_reports'] ?? [])
                                    <table class="table table-condensed" id="oxidized-parser-reports"><thead><tr><th>Parser</th><th>Artifacts</th><th>Connections</th><th>Named interfaces without counters</th><th>Status</th></tr></thead><tbody>
                                        @foreach ($oxidizedInspection['parser_reports'] as $report)<tr><td>{{ $report['label'] }}@if(isset($report['source_index']))<br><small>Source scope {{ $report['source_index'] }}</small>@endif</td><td>{{ $report['matched_files'] }}</td><td>{{ $report['links'] }}</td><td>{{ $report['unbound_interfaces'] }}</td><td>{{ $report['blocked'] ? 'Blocked' : 'Completed' }}</td></tr>@endforeach
                                    </tbody></table>
                                    @endif
                                    <a class="btn btn-default btn-sm" href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'links', 'subtab' => 'parsers', 'section' => 'report']) }}">Refresh parser report</a>

                                    <h4>Parsed connections</h4>
                                    @if ($oxidizedLinks)
                                        <div class="table-responsive">
                                            <table class="table table-condensed table-striped" id="oxidized-neighbors">
                                                <thead><tr><th>Local endpoint</th><th>Remote endpoint</th><th>Protocol</th><th>Parser / artifact</th></tr></thead>
                                                <tbody>
                                                    @foreach ($oxidizedLinks as $link)
                                                        @php
                                                            $attributes = is_array($link['attributes'] ?? null) ? $link['attributes'] : [];
                                                        @endphp
                                                        <tr>
                                                            <td>{{ $attributes['local_hostname'] ?? $link['a']['device'] ?? '—' }}<br><small>{{ $attributes['local_interface'] ?? 'Unknown interface' }}</small>@if($attributes['local_interface_unbound'] ?? false)<br><small>Named interface · no local counters</small>@endif</td>
                                                            <td>{{ $attributes['remote_hostname'] ?? $link['b']['device'] ?? '—' }}<br><small>{{ $attributes['remote_interface'] ?? 'Unknown interface' }}</small></td>
                                                            <td>{{ strtoupper((string) ($link['protocol'] ?? '—')) }}</td>
                                                            <td>{{ $attributes['rule'] ?? '—' }}<br><code>{{ $attributes['artifact_path'] ?? '—' }}</code></td>
                                                        </tr>
                                                    @endforeach
                                                </tbody>
                                            </table>
                                        </div>
                                    @else
                                        <p class="text-muted">No connections matched the selected parsers and inventory.</p>
                                    @endif

                                    <details>
                                        <summary><strong>Matched artifacts ({{ count($oxidizedInspection['matched_files']) }})</strong></summary>
                                        <div class="table-responsive" style="margin-top: 8px">
                                            <table class="table table-condensed">
                                                <thead><tr><th>Artifact</th><th>Matched parser rule</th></tr></thead>
                                                <tbody>
                                                    @foreach ($oxidizedInspection['matched_files'] as $path)
                                                        <tr><td><code>{{ $path }}</code></td><td>{{ $oxidizedInspection['matched_rules'][$path] ?? 'No matching connection' }}</td></tr>
                                                    @endforeach
                                                </tbody>
                                            </table>
                                        </div>
                                    </details>

                                    @if ($oxidizedFailures)
                                        <details>
                                            <summary><strong>Resolution issues ({{ count($oxidizedFailures) }})</strong></summary>
                                            <div class="table-responsive" style="margin-top: 8px">
                                                <table class="table table-condensed">
                                                    <thead><tr><th>Artifact</th><th>Rule/block</th><th>Reason</th></tr></thead>
                                                    <tbody>
                                                        @foreach ($oxidizedFailures as $failure)
                                                            <tr>
                                                                <td><code>{{ $failure['path'] ?? '—' }}</code></td>
                                                                <td>{{ $failure['rule'] ?? '—' }} / {{ $failure['block'] ?? '—' }}</td>
                                                                <td>{{ $failure['reason'] ?? '—' }}</td>
                                                            </tr>
                                                        @endforeach
                                                    </tbody>
                                                </table>
                                            </div>
                                        </details>
                                    @endif

                                    @if ($oxidizedSkipped)
                                        <details>
                                            <summary><strong>Skipped artifacts ({{ count($oxidizedSkipped) }})</strong></summary>
                                            <ul>
                                                @foreach ($oxidizedSkipped as $skipped)
                                                    <li><code>{{ $skipped['path'] ?? '—' }}</code>: {{ $skipped['reason'] ?? 'Unknown reason' }}</li>
                                                @endforeach
                                            </ul>
                                        </details>
                                    @endif

                                    @if ($oxidizedInspection['warnings'])
                                        <details>
                                            <summary><strong>Parser warnings ({{ count($oxidizedInspection['warnings']) }})</strong></summary>
                                            <ul>
                                                @foreach ($oxidizedInspection['warnings'] as $warning)
                                                    <li>{{ $warning }}</li>
                                                @endforeach
                                            </ul>
                                        </details>
                                    @endif
                                @endif
                            @endif
                        </div>
                    </div>
                    @endif
                </div>
