@if ($plan)
    @php
        $previewNodes = collect($plan['operations'])->where('kind', 'node')->where('action', '!=', 'delete')->keyBy('canonical_id');
        $previewLayers = $planLayers;
        $summary = $plan['layout_summary'] ?? [];
        $previewWidth = max(200, $summary['width'] ?? data_get($map, 'layout.width', 1600));
        $previewHeight = max(200, $summary['height'] ?? data_get($map, 'layout.height', 800));
    @endphp
    <p>Reviewed layout: {{ ['force' => 'Organic', 'top' => 'Top-down', 'left' => 'Left-to-right', 'radial' => 'Radial'][$summary['resolved'] ?? ''] ?? 'Automatic' }} · {{ $previewWidth }} × {{ $previewHeight }}{{ ($summary['hierarchy_inferred'] ?? false) ? ' · hierarchy inferred' : '' }}</p>
    <label for="preview-zoom">Zoom</label><input id="preview-zoom" type="range" min="50" max="250" value="100">
    <label><input id="preview-labels" type="checkbox" checked> Show labels</label>
    <label><input id="preview-tiers" type="checkbox" checked> Show tiers</label>
    <div style="overflow:auto;max-height:600px;border:1px solid #ddd" id="topology-preview">
        <svg role="img" aria-labelledby="preview-title" viewBox="-60 -60 {{ $previewWidth + 120 }} {{ $previewHeight + 120 }}" style="width:100%;height:560px" xmlns="http://www.w3.org/2000/svg">
            <title id="preview-title">Reviewed map topology. Device coordinates are also available in the table below.</title>
            @foreach ($plan['operations'] as $edge)
                @if ($edge['kind'] === 'edge' && $edge['action'] !== 'delete')
                    @php $first = $previewNodes->get($edge['generated_values']['from_node'] ?? ''); $second = $previewNodes->get($edge['generated_values']['to_node'] ?? ''); @endphp
                    @if ($first && $second)<line x1="{{ $first['generated_values']['x_pos'] }}" y1="{{ $first['generated_values']['y_pos'] }}" x2="{{ $second['generated_values']['x_pos'] }}" y2="{{ $second['generated_values']['y_pos'] }}" stroke="#8a99a8" stroke-width="2" />@endif
                @endif
            @endforeach
            @foreach ($previewNodes as $node)
                @php $tier = array_search($node['layer'] ?? null, $previewLayers, true); $tierLabel = $tier === false ? 'Automatic' : ($tier === 0 ? 'Core / first tier' : 'Tier '.($tier + 1)); @endphp
                <g data-device-id="{{ $node['generated_values']['device_id'] ?? '' }}"><title>{{ $node['display_name'] }} · {{ $tierLabel }}</title><circle cx="{{ $node['generated_values']['x_pos'] }}" cy="{{ $node['generated_values']['y_pos'] }}" r="12" fill="#d2e5ff" stroke="#2b7ce9" /><text class="preview-label" x="{{ $node['generated_values']['x_pos'] }}" y="{{ $node['generated_values']['y_pos'] + 30 }}" text-anchor="middle" font-size="14">{{ $node['generated_values']['label'] ?? $node['display_name'] }}</text><text class="preview-tier" x="{{ $node['generated_values']['x_pos'] }}" y="{{ $node['generated_values']['y_pos'] + 47 }}" text-anchor="middle" font-size="11" fill="#52616b">{{ $tierLabel }}</text></g>
            @endforeach
        </svg>
    </div>
    <details><summary>Accessible topology table</summary><table class="table table-condensed"><caption>Reviewed device tiers, positions, and connections</caption><thead><tr><th>Device</th><th>Placement</th><th>X</th><th>Y</th><th>Connected devices</th></tr></thead><tbody>
        @foreach ($previewNodes as $id => $node)
            @php $tier = array_search($node['layer'] ?? null, $previewLayers, true); @endphp
            <tr><td>{{ $node['display_name'] }}</td><td>{{ $tier === false ? 'Automatic' : ($tier === 0 ? 'Core / first tier' : 'Tier '.($tier + 1)) }}</td><td>{{ $node['generated_values']['x_pos'] }}</td><td>{{ $node['generated_values']['y_pos'] }}</td><td>
            @foreach ($plan['operations'] as $edge)
                @if ($edge['kind'] === 'edge' && $edge['action'] !== 'delete' && in_array($id, [$edge['generated_values']['from_node'] ?? '', $edge['generated_values']['to_node'] ?? ''], true))
                    {{ $previewNodes->get($edge['generated_values']['from_node'] === $id ? $edge['generated_values']['to_node'] : $edge['generated_values']['from_node'])['display_name'] ?? 'Unresolved' }};
                @endif
            @endforeach
            </td></tr>
        @endforeach
    </tbody></table></details>
    <script>$(function () { $('#preview-zoom').on('input', function () { $('#topology-preview svg').css({width: this.value + '%', height: (5.6 * this.value) + 'px'}); }); $('#preview-labels').on('change', function () { $('.preview-label').toggle(this.checked); }); $('#preview-tiers').on('change', function () { $('.preview-tier').toggle(this.checked); }); });</script>
@else
    <p>Review changes to generate a topology preview from current inventory.</p>
@endif
