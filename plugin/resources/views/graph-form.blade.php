<form method="POST" action="{{ route('libremapper.graph.save', ['map' => $selectedKey]) }}" id="traffic-interface-editor" class="lm-section lm-map-section">
    @csrf @method('PUT')
    <input type="hidden" name="map_hash" value="{{ hash('sha256', json_encode($map, JSON_THROW_ON_ERROR)) }}">
    <input type="hidden" name="canonical_id" value="{{ $operation['canonical_id'] }}">
    <span class="lm-scope lm-scope-map">This map</span><br>
    <label for="traffic-interface">Traffic interface</label> @include('libremapper::help', ['helpLabel' => 'Traffic interface', 'helpText' => 'Chooses the real LibreNMS port supplying this connection’s traffic graph on this map. Automatic uses a valid endpoint interface when available. This does not change the shared connection endpoints. Custom labels cannot supply counters.'])
    <select class="form-control" id="traffic-interface" name="port_id" disabled><option value="">Automatic</option></select>
    <p class="graph-error text-danger" role="alert"></p>
    <button class="btn btn-primary btn-xs" type="submit" disabled>Save traffic interface</button>
</form>
<script>
$(function () {
    var form = $('#traffic-interface-editor'), select = form.find('select'), url = @json(route('libremapper.interfaces', ['device' => 0]));
    var owners = @json($operation['graph']['endpoint_device_ids'] ?? []), selected = @json(($operation['graph']['mode'] ?? '') === 'overridden' ? (string) ($operation['graph']['requested_port_id'] ?? '') : '');
    Promise.all(owners.map(function (owner) {
        return $.getJSON(url.replace('/0/interfaces', '/' + owner + '/interfaces')).then(function (data) {
            return data.interfaces.filter(function (port) { return !port.deleted; }).map(function (port) { return {id:port.port_id, text:'Device ' + owner + ' · ' + port.text}; });
        });
    })).then(function (groups) {
        groups.flat().forEach(function (port) { select.append(new Option(port.text, port.id)); });
        if (selected && !groups.flat().some(function (port) { return String(port.id) === selected; })) select.append(new Option('Unavailable port ID ' + selected + ' — choose a replacement or Automatic', selected));
        select.val(selected).prop('disabled', false).select2({theme:'bootstrap', width:'100%'});
        form.find('button').prop('disabled', false);
    }).catch(function () { form.find('.graph-error').text('Unable to load interfaces. Reload to try again.'); });
});
</script>
