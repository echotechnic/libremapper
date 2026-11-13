@php
    $parserItems = collect($parserCatalog['items']);
    $parserEntry = $parserItems->firstWhere('id', '===', $parserEditorId);
    $cloneEntry = $parserItems->firstWhere('id', '===', $parserCloneId);
    $editingParser = $parserEntry !== null;
    $showParserEditor = $editingParser || $parserEditorId === '__new__' || $cloneEntry !== null;
    $parserDefinition = $parserEntry['definition'] ?? $cloneEntry['definition'] ?? [
        'type' => 'regex', 'label' => '', 'file_globs' => ['topology/*'],
        'path_regex' => '^topology/(?P<device_name>[^/]+)$', 'protocol' => 'lldp', 'zero_output_guard' => true,
        'rules' => [['id' => 'neighbors', 'label' => '', 'regex' => '(?P<remote_hostname>\\S+)', 'flags' => ['MULTILINE']]],
    ];
    if (is_string(old('definition'))) {
        $oldDefinition = json_decode(old('definition'), true);
        if (is_array($oldDefinition) && !array_is_list($oldDefinition)) $parserDefinition = $oldDefinition;
    }
    $parserUrl = static fn (array $extra = []) => route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'advanced', 'subtab' => 'parsers'] + $extra);
@endphp
<div class="lm-section-heading"><h3>Global parser catalog @include('libremapper::help', ['helpLabel' => 'Parser definitions', 'helpText' => 'A parser definition describes how repository files become connections. Definitions are shared; each map independently chooses which parsers to enable. Changing a definition makes maps using it drafts and pauses their schedules until publication.'])</h3><span class="lm-scope lm-scope-shared">Shared across maps</span></div>
<p>Edit how connections are extracted here. Enable each definition separately in a map’s <a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'links', 'subtab' => 'parsers']) }}">Links → Parsers</a>.</p>
<p><a class="btn btn-default" href="{{ $parserUrl(['edit' => '__new__']) }}">New parser</a>
<a href="{{ route('plugin.settings', ['plugin' => 'libremapper', 'map' => $selectedKey, 'tab' => 'advanced', 'subtab' => 'files', 'file' => 'libremapper.yaml']) }}">Edit underlying file in Advanced</a></p>
@if ($parserCatalogError)<div class="alert alert-danger">{{ $parserCatalogError }}</div>@endif
@if (!$showParserEditor)
<div class="table-responsive"><table class="table table-condensed" id="parser-catalog"><thead><tr><th>Parser</th><th>Engine @include('libremapper::help', ['helpLabel' => 'Parser engine', 'helpText' => 'Regex extracts named captures from text. SAOS 10 NNI interprets interface addressing and matches existing inventory peers.'])</th><th>Definition @include('libremapper::help', ['helpLabel' => 'Definition origin', 'helpText' => 'Packaged definitions ship with Libremapper. Customized built-ins override those defaults and can be reset. Custom definitions can be deleted when unused.'])</th><th>Maps using it</th><th>Actions</th></tr></thead><tbody>
@foreach ($parserItems as $entry)
    <tr data-parser-id="{{ $entry['id'] }}"><td><a href="{{ $parserUrl(['edit' => $entry['id']]) }}">{{ $entry['definition']['label'] }}</a><br><code>{{ $entry['id'] }}</code></td>
        <td>{{ $entry['definition']['type'] === 'regex' ? 'Regex' : 'SAOS 10 NNI' }}</td><td>{{ $entry['builtin'] ? ($entry['overridden'] ? 'Built-in · customized' : 'Built-in · packaged') : 'Custom' }}</td>
        <td>{{ implode(', ', $entry['maps']) ?: 'None' }}@if ($entry['legacy_defaults'])<br><small>Source defaults: {{ implode(', ', $entry['legacy_defaults']) }}</small>@endif</td>
        <td><a href="{{ $parserUrl(['edit' => $entry['id']]) }}">Edit</a> · <a href="{{ $parserUrl(['clone' => $entry['id']]) }}">Clone</a></td></tr>
@endforeach
</tbody></table></div>
@endif
@if ($showParserEditor)
<div class="panel panel-default" id="parser-editor"><div class="panel-heading lm-section-heading"><strong>{{ $editingParser ? 'Edit parser' : 'Create parser' }}</strong><span class="lm-scope lm-scope-shared">Shared across maps</span></div><div class="panel-body">
<ul class="nav nav-pills lm-editor-tabs" role="tablist" aria-label="Parser editor sections">
    <li class="active"><a href="#parser-definition-panel" data-toggle="tab" role="tab" aria-controls="parser-definition-panel" aria-expanded="true">Definition</a></li>
    <li><a href="#parser-preview-panel" data-toggle="tab" role="tab" aria-controls="parser-preview-panel" aria-expanded="false">Preview</a></li>
</ul>
<div class="tab-content"><div class="tab-pane active" id="parser-definition-panel" role="tabpanel">
<form method="POST" action="{{ route('libremapper.parsers.save') }}" id="parser-form">
    @csrf <input type="hidden" name="map" value="{{ $selectedKey }}"><input type="hidden" name="sha256" value="{{ $mainSha256 }}">
    <input type="hidden" name="original_id" value="{{ $editingParser ? $parserEntry['id'] : '' }}"><input type="hidden" name="definition" id="parser-definition">
    <fieldset class="lm-form-section"><legend>Identity</legend><div class="row">
        <div class="form-group col-md-4"><label for="parser-id">Stable ID</label> @include('libremapper::help', ['helpLabel' => 'Stable parser ID', 'helpText' => 'Used to identify parser output and keep connection identities stable. An existing ID cannot change; clone the parser to create a new identity.'])<input id="parser-id" name="id" class="form-control" pattern="[a-z0-9][a-z0-9._-]{0,63}" maxlength="64" value="{{ old('id', $editingParser ? $parserEntry['id'] : '') }}" @readonly($editingParser) required><p class="help-block">IDs preserve connection identity. Clone to use a different ID.</p></div>
        <div class="form-group col-md-4"><label for="parser-label">Label</label><input id="parser-label" class="form-control" maxlength="255" required></div>
        <div class="form-group col-md-4"><label for="parser-type">Engine</label><select id="parser-type" class="form-control" @disabled($editingParser)><option value="regex">Regex extraction</option><option value="saos10_nni">SAOS 10 NNI configuration</option></select></div>
    </div></fieldset>
    <fieldset class="lm-form-section"><legend>Choose artifacts</legend>
    <div class="form-group"><label for="parser-globs">Artifact patterns (one per line)</label> @include('libremapper::help', ['helpLabel' => 'Artifact patterns', 'helpText' => 'An artifact is a backed-up configuration or command-output file. Relative glob patterns choose files to inspect; for example, topology/* selects files in that directory.'])<textarea id="parser-globs" class="form-control" rows="2" required spellcheck="false"></textarea><p class="help-block">Paths are relative to the repository. Absolute paths, parent traversal, and symbolic links are refused.</p></div>
    <div class="form-group"><label for="parser-path">Filename-to-device regex</label> @include('libremapper::help', ['helpLabel' => 'Filename matching', 'helpText' => 'Matches the relative file path and captures the local device name. The captured name is resolved against LibreNMS inventory; it does not create a LibreNMS device.'])<textarea id="parser-path" class="form-control" rows="2" required spellcheck="false"></textarea><p class="help-block">Use a named <code>(?P&lt;device_name&gt;...)</code> capture, or capture <code>local_hostname</code> in a regex rule.</p></div>
    </fieldset>
    <fieldset class="lm-form-section"><legend>Extract connections</legend>
    <div class="checkbox"><label><input id="parser-guard" type="checkbox"> Block when matching artifacts produce no valid connections</label> @include('libremapper::help', ['helpLabel' => 'Zero-output guard', 'helpText' => 'When files match but produce no valid connections, block this parser result and publication for maps enabling it. Turning this off allows an empty result.'])</div>
    <div id="regex-parser-fields">
        <div class="form-group"><label for="parser-protocol">Protocol</label> @include('libremapper::help', ['helpLabel' => 'Protocol label', 'helpText' => 'Labels extracted connections, for example lldp. Map protocol filters can use this value; changing it does not change how the regex matches text.'])<input id="parser-protocol" class="form-control" maxlength="64"></div>
        <h4>Ordered fallback formats</h4><p>For each artifact, the first rule producing valid connections wins. Outputs from different enabled parsers are combined.</p>
        <p>Each rule needs <code>remote_hostname</code>. Optional captures: <code>local_hostname</code>, <code>local_interface</code>, <code>remote_interface</code>, <code>local_management_ip</code>, <code>remote_management_ip</code>, <code>remote_chassis_id</code>, and <code>site</code>.</p>
        <div id="parser-rules"></div><button type="button" class="btn btn-default" id="add-parser-rule">Add fallback format</button>
    </div>
    <div id="nni-parser-fields" hidden>
        <p>The SAOS 10 grammar matches point-to-point interface addresses to unique existing inventory peers in the same VRF/context. Missing inventory interfaces remain named endpoints without local counters.</p>
        <div class="form-group"><label for="parser-peer">Peer hostname regex</label> @include('libremapper::help', ['helpLabel' => 'Peer matching', 'helpText' => 'Limits which existing LibreNMS device names can be remote peers. The address match must identify a unique peer in the same routing context.'])<textarea id="parser-peer" class="form-control" rows="2" spellcheck="false"></textarea></div>
        <div class="form-group"><label for="parser-interface-types">Interface types (one per line)</label> @include('libremapper::help', ['helpLabel' => 'Interface types', 'helpText' => 'SAOS configuration interface types eligible for point-to-point address matching, such as point-to-point. These are configuration keywords, not LibreNMS port names.'])<textarea id="parser-interface-types" class="form-control" rows="2"></textarea></div>
        <div class="row"><div class="form-group col-md-6"><label for="parser-ipv4">IPv4 prefix lengths (comma separated)</label> @include('libremapper::help', ['helpLabel' => 'IPv4 prefixes', 'helpText' => 'Inspect only configured IPv4 networks with these prefix lengths. Blank disables IPv4 matching; at least one address family must remain enabled.'])<input id="parser-ipv4" class="form-control"></div><div class="form-group col-md-6"><label for="parser-ipv6">IPv6 prefix lengths (comma separated)</label> @include('libremapper::help', ['helpLabel' => 'IPv6 prefixes', 'helpText' => 'Inspect only configured IPv6 networks with these prefix lengths. Blank disables IPv6 matching; at least one address family must remain enabled.'])<input id="parser-ipv6" class="form-control"></div></div>
    </div>
    </fieldset>
    <div class="lm-save-bar">
    <p id="parser-definition-error" class="text-danger" role="alert" hidden></p>
    <p><strong>Saving affects:</strong> {{ $editingParser ? (implode(', ', $parserEntry['maps']) ?: 'No maps currently enable this parser') : 'No maps until this new parser is enabled' }}.</p>
    <p><button class="btn btn-primary" type="submit" @disabled($configError || $parserCatalogError)>Save global parser</button> <a href="{{ $parserUrl() }}">Close editor</a></p>
    <p class="help-block">A successful preview is optional. Saving validates the definition; Review reads live artifacts before publication.</p>
    </div>
</form>
@if ($editingParser)
    <form method="POST" action="{{ route('libremapper.parsers.action') }}" id="parser-action-form">
        @csrf <input type="hidden" name="id" value="{{ $parserEntry['id'] }}"><input type="hidden" name="sha256" value="{{ $mainSha256 }}"><input type="hidden" name="map" value="{{ $selectedKey }}">
        <input type="hidden" name="action" value="{{ $parserEntry['builtin'] ? 'reset' : 'delete' }}">
        <button class="btn btn-warning" type="submit" @disabled($parserEntry['builtin'] ? !$parserEntry['overridden'] : (bool)($parserEntry['maps'] || $parserEntry['legacy_defaults']))>{{ $parserEntry['builtin'] ? 'Reset to packaged definition' : 'Delete unused parser' }}</button>
        @if (!$parserEntry['builtin'] && ($parserEntry['maps'] || $parserEntry['legacy_defaults']))<p class="help-block">Disable this parser in its maps and source defaults before deleting it.</p>@endif
    </form>
@endif
</div>
<section class="tab-pane" id="parser-preview-panel" role="tabpanel" aria-labelledby="parser-preview-heading">
<div class="lm-section-heading"><h4 id="parser-preview-heading">Preview unsaved definition @include('libremapper::help', ['helpLabel' => 'Unsaved preview', 'helpText' => 'Runs the current editor values against a sample or repository file. It does not save the definition, change map selections, or invalidate reviews.'])</h4><span class="lm-scope lm-scope-preview">Preview only</span></div><p>Pasted text is limited to 256 KiB; results show up to 100 matches. Parsing stops after 10 seconds, in addition to the Git fetch timeout.</p>
<form id="parser-preview-form">
    <div class="form-group"><label for="preview-mode">Artifact source</label><select id="preview-mode" class="form-control"><option value="paste">Pasted text</option><option value="repository" @disabled(!$oxidizedProvider)>Repository artifact</option></select></div>
    <div id="preview-paste"><div class="form-group"><label for="preview-filename">Relative filename</label><input id="preview-filename" class="form-control" placeholder="topology/device.example.net--lldp-neighbors"></div><div class="form-group"><label for="preview-sample">Sample artifact</label><textarea id="preview-sample" class="form-control" rows="8" spellcheck="false" autocomplete="off"></textarea></div></div>
    <div id="preview-repository" hidden><div class="form-group"><label for="preview-artifact">Repository artifact</label><select id="preview-artifact" class="form-control" style="width:100%"></select><p class="help-block">Search files matching the unsaved artifact patterns. Files load only when searched.</p></div></div>
    <button class="btn btn-default" type="submit" id="run-parser-preview">Run preview</button>
</form>
<pre id="parser-preview-result" role="status" aria-live="polite" style="margin-top:12px;white-space:pre-wrap" hidden></pre>
</section>
</div></div></div>
<script>
$(function () {
    var initial = @json($parserDefinition), existing = @json($editingParser), ruleIndex = 0;
    var flags = ['ASCII', 'DOTALL', 'IGNORECASE', 'MULTILINE', 'VERBOSE'];
    var flagHelp = {ASCII:'Use ASCII character classes for expressions such as \\w and \\d.', DOTALL:'Allow a dot to match newline characters.', IGNORECASE:'Match letters without distinguishing uppercase and lowercase.', MULTILINE:'Let ^ and $ match the beginning and end of each line.', VERBOSE:'Ignore unescaped whitespace and allow # comments in the expression.'};
    function lines(value) { return value.split(/\r?\n/).map(function (v) { return v.trim(); }).filter(Boolean); }
    function addRule(rule, stored) {
        var row = $('<fieldset class="well well-sm parser-rule">'), index = ++ruleIndex;
        function input(label, cls, value, textarea) {
            var id = 'parser-rule-' + index + '-' + cls, group = $('<div class="form-group">');
            group.append($('<label>').attr('for', id).text(label));
            var field = $(textarea ? '<textarea rows="5" spellcheck="false">' : '<input>');
            field.addClass('form-control ' + cls).attr('id', id).val(value || ''); group.append(field); row.append(group); return field;
        }
        input('Stable rule ID', 'rule-id', rule.id).prop('readOnly', stored).attr('maxlength', 64);
        input('Format label (optional)', 'rule-label', rule.label).attr('maxlength', 255);
        input('Extraction regex', 'rule-regex', rule.regex, true);
        var choices = $('<div>').text('Regex flags: ');
        flags.forEach(function (flag) { var item = $('<span style="display:inline-block;margin-right:12px">'), label = $('<label>').append($('<input type="checkbox" class="rule-flag">').val(flag).prop('checked', (rule.flags || []).indexOf(flag) !== -1)); label.append(document.createTextNode(' ' + flag)); item.append(label).append($('<button type="button" class="lm-help" data-toggle="tooltip">').attr({title:flagHelp[flag], 'aria-label':flag + ' flag: ' + flagHelp[flag]}).append('<span class="fa fa-info-circle" aria-hidden="true"></span>')); choices.append(item); });
        row.append(choices);
        ['up', 'down', 'remove'].forEach(function (action) { row.append($('<button type="button" class="btn btn-link">').addClass('rule-' + action).text(action === 'remove' ? 'Remove format' : 'Move ' + action)); });
        $('#parser-rules').append(row);
    }
    $('#parser-label').val(initial.label); $('#parser-type').val(initial.type); $('#parser-globs').val((initial.file_globs || []).join('\n'));
    $('#parser-path').val(initial.path_regex); $('#parser-guard').prop('checked', initial.zero_output_guard); $('#parser-protocol').val(initial.protocol || 'lldp');
    $('#parser-peer').val(initial.peer_hostname_regex || '^car\\d+(?:\\.|$)'); $('#parser-interface-types').val((initial.interface_types || ['point-to-point']).join('\n'));
    $('#parser-ipv4').val((initial.ipv4_prefix_lengths || [30,31]).join(', ')); $('#parser-ipv6').val((initial.ipv6_prefix_lengths || [126,127]).join(', '));
    (initial.rules || []).forEach(function (rule) { addRule(rule, existing); });
    function engineFields() { var regex = $('#parser-type').val() === 'regex'; $('#regex-parser-fields').prop('hidden', !regex); $('#nni-parser-fields').prop('hidden', regex); if (regex && !$('.parser-rule').length) addRule({id:'neighbors', regex:'(?P<remote_hostname>\\S+)', flags:['MULTILINE']}, false); }
    $('#parser-type').on('change', engineFields); engineFields();
    $('#add-parser-rule').on('click', function () { addRule({id:'format-' + Date.now(), flags:['MULTILINE']}, false); });
    $('#parser-rules').on('click', '.rule-remove', function () { $(this).closest('.parser-rule').remove(); }).on('click', '.rule-up', function () { var row = $(this).closest('.parser-rule'); row.insertBefore(row.prev('.parser-rule')); }).on('click', '.rule-down', function () { var row = $(this).closest('.parser-rule'); row.insertAfter(row.next('.parser-rule')); });
    function definition() {
        var result = {type:$('#parser-type').val(), label:$('#parser-label').val(), file_globs:lines($('#parser-globs').val()), path_regex:$('#parser-path').val(), zero_output_guard:$('#parser-guard').prop('checked')};
        if (result.type === 'regex') { result.protocol = $('#parser-protocol').val(); result.rules = $('.parser-rule').map(function () { var row = $(this); return {id:row.find('.rule-id').val(), label:row.find('.rule-label').val(), regex:row.find('.rule-regex').val(), flags:row.find('.rule-flag:checked').map(function () { return this.value; }).get()}; }).get(); }
        else { result.peer_hostname_regex = $('#parser-peer').val(); result.interface_types = lines($('#parser-interface-types').val()); ['ipv4','ipv6'].forEach(function (family) { result[family + '_prefix_lengths'] = $('#parser-' + family).val().split(/[\s,]+/).filter(Boolean).map(function (v) { if (!/^\d+$/.test(v)) throw new Error('Prefix lengths must be whole numbers.'); return Number(v); }); }); }
        return result;
    }
    $('#parser-form').on('submit', function (event) { $('#parser-definition-error').prop('hidden', true); try { $('#parser-definition').val(JSON.stringify(definition())); } catch (error) { event.preventDefault(); $('#parser-definition-error').prop('hidden', false).text(error.message); } });
    $('#parser-action-form').on('submit', function () { return window.confirm(@json($editingParser && $parserEntry['builtin'] ? 'Reset this shared parser? Maps using it will require review.' : 'Delete this unused shared parser?')); });
    $('#preview-mode').on('change', function () { var paste = this.value === 'paste'; $('#preview-paste').prop('hidden', !paste); $('#preview-repository').prop('hidden', paste); });
    function payload() { return {parser_id:$('#parser-id').val() || 'preview', definition:definition(), provider:'oxidized', filename:'sample'}; }
    var csrf = $('#parser-form input[name=_token]').val();
    $('#preview-artifact').select2({placeholder:'Search matching artifacts…', minimumInputLength:0, ajax:{url:@json(route('libremapper.parsers.files')), type:'POST', contentType:'application/json', headers:{'X-CSRF-TOKEN':csrf, 'Accept':'application/json'}, delay:300,
        data:function (params) { var request = payload(); request.query = params.term || ''; return JSON.stringify(request); },
        processResults:function (data) { return {results:data.files.map(function (path) { return {id:path, text:path}; })}; },
        transport:function (params, success, failure) { var request = $.ajax(params); request.then(success); request.fail(function (xhr) { $('#parser-preview-result').prop('hidden', false).text((xhr.responseJSON || {}).error || 'Unable to list artifacts.'); failure(); }); return request; }
    }});
    $('#parser-preview-form').on('submit', function (event) {
        event.preventDefault(); var result = $('#parser-preview-result').prop('hidden', false), request;
        try { request = payload(); if ($('#preview-mode').val() === 'paste') { request.filename = $('#preview-filename').val(); request.sample = $('#preview-sample').val(); } else request.filename = $('#preview-artifact').val() || ''; }
        catch (error) { result.text(error.message); return; }
        result.text('Preview running…'); $('#run-parser-preview').prop('disabled', true);
        $.ajax({url:@json(route('libremapper.parsers.preview')), type:'POST', contentType:'application/json', headers:{'X-CSRF-TOKEN':csrf, 'Accept':'application/json'}, data:JSON.stringify(request)})
            .done(function (data) { result.text(JSON.stringify(data, null, 2)); })
            .fail(function (xhr) { result.text((xhr.responseJSON || {}).error || 'Preview failed.'); })
            .always(function () { $('#run-parser-preview').prop('disabled', false); });
    });
});
</script>
@endif
