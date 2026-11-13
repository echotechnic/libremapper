<div class="form-group col-md-6">
    <label for="node_label">Node labels</label> @include('libremapper::help', ['helpLabel' => 'Node labels', 'helpText' => 'Chooses the map-wide label format. sysName uses the LibreNMS name when available and otherwise falls back to a short hostname.'])
    <select class="form-control" id="node_label" name="node_label">
        @foreach (['sys_name' => 'LibreNMS device name (sysName)', 'full' => 'Full hostname', 'short_hostname' => 'Short hostname'] as $value => $label)
            <option value="{{ $value }}" @selected(old('node_label', data_get($map, 'node_label', 'full')) === $value)>{{ $label }}</option>
        @endforeach
    </select>
    <p class="help-block">sysName falls back to the short hostname when unavailable.</p>
</div>
<div class="form-group col-md-6">
    <label for="short_hostname_parts">Short hostname components</label> @include('libremapper::help', ['helpLabel' => 'Map hostname default', 'helpText' => 'The default component count for short hostnames on this map. Individual hostname overrides take priority. Components are counted from the left; IP addresses stay intact.'])
    <input class="form-control" type="number" min="1" max="127" step="1" id="short_hostname_parts" name="short_hostname_parts" value="{{ old('short_hostname_parts', data_get($map, 'short_hostname_parts', 2)) }}" required aria-describedby="short-hostname-help">
    <p class="help-block" id="short-hostname-help">Keep this many components from the left. With 2, router1.site1.edge.example.test becomes router1.site1. IP addresses stay intact.</p>
</div>
