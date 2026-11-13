@if (!$newMap && in_array($activeSubtab, ['configuration', 'appearance'], true))
<form method="POST" action="{{ route('libremapper.config.update') }}" id="advanced-map-form">
@csrf @method('PUT')
<input type="hidden" name="original_key" value="{{ $selectedKey }}"><input type="hidden" name="map_hash" value="{{ hash('sha256', json_encode($map, JSON_THROW_ON_ERROR)) }}">
<input type="hidden" name="tab" value="advanced"><input type="hidden" name="subtab" value="{{ $activeSubtab }}"><input type="hidden" name="map" value="{{ $selectedKey }}">
@foreach ($activeSubtab === 'configuration' ? ['scheduled'] : ['layout_auto_grow', 'edge_showpct', 'edge_showbps'] as $checkbox)
<input type="hidden" name="{{ $checkbox }}" value="0">
@endforeach
@if ($activeSubtab === 'configuration')
                            <div class="panel panel-default">
                                <div class="panel-heading lm-section-heading"><strong>Map and discovery</strong><span class="lm-scope lm-scope-map">This map</span></div>
                                <div class="panel-body">
                                    <p>Layer overrides run after automatic and manual topologies are merged; their first match wins.</p>
                                    <div class="row">
                                        <div class="form-group col-md-3">
                                            <label for="key">Key</label>
                                            @include('libremapper::help', ['helpLabel' => 'Map key help', 'helpText' => 'The key is the stable identifier used by commands and configuration files.'])
                                            @if ($newMap)
                                                <input class="form-control" id="key" name="key" value="{{ old('key') }}"
                                                       placeholder="Example: campus-core" pattern="[a-z0-9]+(?:-[a-z0-9]+)*" required>
                                            @else
                                                <input class="form-control" id="key" value="{{ $selectedKey }}" readonly>
                                            @endif
                                        </div>
                                        <div class="form-group col-md-3">
                                            <label for="name">Name</label>
                                            <input class="form-control" id="name" name="name"
                                                   value="{{ old('name', data_get($map, 'name', '')) }}"
                                                   @if ($newMap) placeholder="Example: Campus Core" @endif required>
                                        </div>
                                        @if ($newMap)
                                            <div class="form-group col-md-3">
                                                <label for="provider">LibreNMS provider</label>
                                                <select class="form-control" id="provider" name="provider" required>
                                                    <option value="" disabled @selected(old('provider', count($librenmsProviders) === 1 ? $librenmsProviders[0] : '') === '')>Choose a provider</option>
                                                    @foreach ($librenmsProviders as $provider)
                                                        <option value="{{ $provider }}" @selected(old('provider', count($librenmsProviders) === 1 ? $librenmsProviders[0] : '') === $provider)>{{ $provider }}</option>
                                                    @endforeach
                                                </select>
                                            </div>
                                        @endif
                                        <div class="form-group col-md-3">
                                            <label for="menu_group">Menu group</label>
                                            @include('libremapper::help', ['helpLabel' => 'Menu group help', 'helpText' => 'Maps with the same menu group are listed together in LibreNMS.'])
                                            <input class="form-control" id="menu_group" name="menu_group"
                                                   value="{{ old('menu_group', data_get($map, 'menu_group', '')) }}">
                                        </div>
                                        @include('libremapper::node-labels')
                                    </div>
                                    <div class="row">
                                        <div class="form-group col-md-6">
                                            <label for="selectors_yaml">Devices to include (YAML)</label>
                                            @include('libremapper::help', ['helpLabel' => 'Device selectors help', 'helpText' => 'Each selector adds matching LibreNMS devices. An optional layer number controls placement.'])
                                            <textarea class="form-control" rows="8" id="selectors_yaml" name="selectors_yaml"
                                                      @if ($newMap) placeholder="Example:&#10;- {type: group, value: Core, layer: 0}" required @endif>{{ old('selectors_yaml', $selectorsYaml) }}</textarea>
                                            <p class="help-block">Each selector adds devices. The first matching rule sets its layer in manual layouts. Automatic and hierarchy inference ignore saved layers.</p>
                                        </div>
                                        <div class="form-group col-md-6">
                                            <label for="layer_overrides_yaml">Adjust layers after discovery (YAML)</label>
                                            @include('libremapper::help', ['helpLabel' => 'Layer overrides help', 'helpText' => 'Layer overrides run after topology merging. The first matching rule sets the device layer.'])
                                            <textarea class="form-control" rows="8" id="layer_overrides_yaml" name="layer_overrides_yaml">{{ old('layer_overrides_yaml', $layerOverridesYaml) }}</textarea>
                                            <p class="help-block">In manual layouts, overrides run after topology merging and scope layers; the first match wins. Automatic and hierarchy inference ignore these saved values.</p>
                                        </div>
                                        <div class="col-md-12" style="margin-bottom: 15px">
                                            <button class="btn btn-default btn-xs" type="button" data-toggle="collapse"
                                                    data-target="#selector-examples" aria-expanded="false" aria-controls="selector-examples">
                                                Show YAML examples
                                            </button>
                                            <div class="collapse" id="selector-examples" style="margin-top: 8px">
                                                <div class="row">
                                                    <div class="col-md-6">
                                                        <strong>All selector types and initial layers</strong>
                                                        <pre># Paste the list items into Devices to include.
selectors:
  - {type: hostname, value: "^core[0-9]+\\.", layer: 0}
  - {type: location, value: "^Datacenter West$", layer: 0}
  - {type: group, value: Core, layer: 0}
  - {type: device_id, value: 42, layer: 10}
  - {type: canonical, value: "192.0.2.42", layer: 10}
  - {type: attribute, attribute: os, value: "^(ios|junos)$", layer: 20}

# Empty allows every discovered protocol name.
protocols: []
# Or filter explicitly:
protocols: [lldp, cdp, ip-p2p, mac, bgp, ospf]</pre>
                                                        <p class="help-block">Hostname, location, and attribute values are regular expressions. Group, device ID, and canonical selectors are exact matches. Selectors are unioned and the first match supplies the initial layer.</p>
                                                    </div>
                                                    <div class="col-md-6">
                                                        <strong>Post-merge layer overrides</strong>
                                                        <pre># Paste the list items into Adjust layers.
layer_overrides:
  - {type: hostname, value: "^border", layer: -10}
  - {type: group, value: Core, layer: 0}
  - {type: location, value: "^Branch", layer: 10}
  - {type: attribute, attribute: role, value: "customer", layer: 20}</pre>
                                                        <p class="help-block">Overrides run after topology merging and the first matching rule wins. They change placement, not membership.</p>
                                                        <p class="help-block">Common protocols include <code>lldp</code>, <code>cdp</code>, <code>ip-p2p</code>, <code>mac</code>, <code>bgp</code>, and <code>ospf</code>. LibreNMS may return other names; leave the protocol list empty to allow all discovered names.</p>
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                        <div class="form-group col-md-12">
                                            <label for="protocols">Protocols (comma separated)</label>
                                            @include('libremapper::help', ['helpLabel' => 'Protocols help', 'helpText' => 'Leave empty to allow every discovered protocol name, including names beyond the common LLDP, CDP, IP point-to-point, MAC, BGP, and OSPF values.'])
                                            <input class="form-control" id="protocols" name="protocols"
                                                   value="{{ old('protocols', implode(', ', data_get($source, 'protocols', []))) }}">
                                            <p class="help-block">Empty allows all discovered protocol names; otherwise only the comma-separated names are included.</p>
                                        </div>
                                    </div>
                                </div>
                            </div>

@else
                            <div class="panel panel-default">
                                <div class="panel-heading lm-section-heading"><strong>Layout and display</strong><span class="lm-scope lm-scope-map">This map</span></div>
                                <div class="panel-body">
                                    <p>The example starting values fit a 1080p display. Advanced tuning is usually unnecessary.</p>
                                    <div class="well well-sm">
                                        <strong>Recommended map options</strong>
                                        <div class="checkbox">
                                            <label><input type="checkbox" name="layout_auto_grow" value="1"
                                                @checked(old('layout_auto_grow', data_get($map, 'layout.auto_grow', true)))> Keep devices spaced by growing the canvas</label>
                                        </div>
                                    </div>
                                    <div class="row">
                                        <div class="form-group col-md-4">
                                            <label for="layout_type">Layout style</label>
                                            <select class="form-control" id="layout_type" name="layout_type">
                                                @foreach ([
                                                    'auto' => 'Automatic',
                                                    'force' => 'Organic',
                                                    'top' => 'Top to bottom',
                                                    'left' => 'Left to right',
                                                    'radial' => 'Radial rings',
                                                ] as $value => $label)
                                                    <option value="{{ $value }}" @selected(old('layout_type', data_get($map, 'layout.type', 'force')) === $value)>{{ $label }}</option>
                                                @endforeach
                                            </select>
                                        </div>
                                        <div class="checkbox col-md-4">
                                            <label><input type="checkbox" name="edge_showpct" value="1"
                                                @checked(old('edge_showpct', data_get($map, 'edge_defaults.showpct', false)))> Show traffic percentage on links</label>
                                        </div>
                                        <div class="checkbox col-md-4">
                                            <label><input type="checkbox" name="edge_showbps" value="1"
                                                @checked(old('edge_showbps', data_get($map, 'edge_defaults.showbps', false)))> Show traffic rate on links</label>
                                        </div>
                                    </div>
                                    <div class="panel panel-default">
                                        <div class="panel-heading">
                                            <button class="btn btn-link" type="button" data-toggle="collapse"
                                                    data-target="#advanced-map-display" aria-controls="advanced-map-display"
                                                    aria-expanded="{{ $advancedDisplayOpen ? 'true' : 'false' }}">
                                                Advanced map display
                                            </button>
                                        </div>
                                        <div class="panel-collapse collapse {{ $advancedDisplayOpen ? 'in' : '' }}" id="advanced-map-display">
                                            <div class="panel-body">
                                                <div class="row">
                                                    @foreach ([
                                                        'width' => ['Display width', '1600px', 'Display dimensions help', 'Display dimensions control the LibreNMS viewer; layout dimensions control generated coordinates.'],
                                                        'height' => ['Display height', '800px', 'Display height help', 'Display dimensions control the LibreNMS viewer; layout dimensions control generated coordinates.'],
                                                        'node_align' => ['Manual node snap grid', 10, 'Manual node snap grid help', 'Node alignment is the manual LibreNMS drag grid and does not alter initial generation.'],
                                                        'edge_separation' => ['Parallel-link separation', 24, 'Parallel-link separation help', 'Edge separation offsets parallel curves and same-layer labels.'],
                                                    ] as $field => [$label, $default, $helpLabel, $helpText])
                                                        <div class="form-group col-md-3">
                                                            <label for="{{ $field }}">{{ $label }}</label>
                                                            @include('libremapper::help', ['helpLabel' => $helpLabel, 'helpText' => $helpText])
                                                            <input class="form-control" id="{{ $field }}" name="{{ $field }}"
                                                                   value="{{ old($field, data_get($map, $field, $default)) }}">
                                                        </div>
                                                    @endforeach
                                                </div>
                                            </div>
                                        </div>
                                    </div>

                                    <div class="panel panel-default">
                                        <div class="panel-heading">
                                            <button class="btn btn-link" type="button" data-toggle="collapse"
                                                    data-target="#advanced-layout-tuning" aria-controls="advanced-layout-tuning"
                                                    aria-expanded="{{ $advancedLayoutOpen ? 'true' : 'false' }}">
                                                Advanced layout tuning
                                            </button>
                                        </div>
                                        <div class="panel-collapse collapse {{ $advancedLayoutOpen ? 'in' : '' }}" id="advanced-layout-tuning">
                                            <div class="panel-body">
                                                <p class="help-block">Margins and spacing may shrink to fit the canvas unless auto-grow is enabled.</p>
                                                <div class="row">
                                                    @foreach ([
                                                        'layout_width' => ['Generated canvas width', 'layout.width', 1600, 'Layout dimensions help', 'Layout dimensions control generated coordinates; display dimensions control the LibreNMS viewer.'],
                                                        'layout_height' => ['Generated canvas height', 'layout.height', 800, 'Layout height help', 'Layout dimensions control generated coordinates; display dimensions control the LibreNMS viewer.'],
                                                        'layout_margin' => ['Margin', 'layout.margin', 60, 'Layout margin help', 'The margin may shrink with spacing to fit unless auto-grow is enabled.'],
                                                        'layout_layer_spacing' => ['Layer gap', 'layout.layer_spacing', 180, 'Layer gap help', 'This gap may shrink to fit unless auto-grow is enabled.'],
                                                        'layout_node_spacing' => ['Node gap', 'layout.node_spacing', 120, 'Node gap help', 'This gap may shrink to fit unless auto-grow is enabled.'],
                                                        'layout_radius' => ['Radial ring gap', 'layout.radius', 180, 'Radial ring gap help', 'Radial radius is the maximum gap between rings.'],
                                                        'layout_center_layer' => ['Center layer', 'layout.center_layer', '', 'Center layer help', 'Center layer places that numeric layer at the canvas center, with lower and higher layers on opposite sides.'],
                                                    ] as $field => [$label, $path, $default, $helpLabel, $helpText])
                                                        <div class="form-group col-md-3">
                                                            <label for="{{ $field }}">{{ $label }}</label>
                                                            @include('libremapper::help', ['helpLabel' => $helpLabel, 'helpText' => $helpText])
                                                            <input type="number" class="form-control" id="{{ $field }}" name="{{ $field }}"
                                                                   value="{{ old($field, data_get($map, $path, $default)) }}">
                                                        </div>
                                                    @endforeach
                                                    <div class="form-group col-md-3">
                                                        <label for="layout_separation">Ordering mode</label>
                                                        @include('libremapper::help', ['helpLabel' => 'Ordering mode help', 'helpText' => 'Simple ordering packs each layer; connected-branch ordering tries to keep related branches together.'])
                                                        <select class="form-control" id="layout_separation" name="layout_separation">
                                                            @foreach (['packed' => 'Simple layer ordering', 'constrained' => 'Keep connected branches together'] as $value => $label)
                                                                <option value="{{ $value }}" @selected(old('layout_separation', data_get($map, 'layout.separation', 'packed')) === $value)>{{ $label }}</option>
                                                            @endforeach
                                                        </select>
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                    </div>

                                    <div class="panel panel-default">
                                        <div class="panel-heading">
                                            <button class="btn btn-link" type="button" data-toggle="collapse"
                                                    data-target="#advanced-appearance" aria-controls="advanced-appearance"
                                                    aria-expanded="{{ $advancedAppearanceOpen ? 'true' : 'false' }}">
                                                Advanced appearance
                                            </button>
                                        </div>
                                        <div class="panel-collapse collapse {{ $advancedAppearanceOpen ? 'in' : '' }}" id="advanced-appearance">
                                            <div class="panel-body">
                                                @php
                                                    $nodeStyles = ['box', 'circle', 'database', 'ellipse', 'text', 'image', 'circularImage', 'diamond', 'dot', 'star', 'triangle', 'triangleDown', 'hexagon', 'square', 'icon'];
                                                    $edgeStyles = ['dynamic', 'continuous', 'discrete', 'diagonalCross', 'straightCross', 'horizontal', 'vertical', 'curvedCW', 'curvedCCW', 'cubicBezier'];
                                                    $nodeStyle = (string) old('node_style', data_get($map, 'node_defaults.style', 'box'));
                                                    $edgeStyle = (string) old('edge_style', data_get($map, 'edge_defaults.style', 'dynamic'));
                                                @endphp
                                                <div class="row">
                                                    <div class="form-group col-md-3">
                                                        <label for="node_style">Node style</label>
                                                        <select class="form-control" id="node_style" name="node_style">
                                                            @if (!in_array($nodeStyle, $nodeStyles, true))
                                                                <option value="{{ $nodeStyle }}" selected>{{ $nodeStyle }} (current unsupported value)</option>
                                                            @endif
                                                            @foreach ($nodeStyles as $style)
                                                                <option value="{{ $style }}" @selected($nodeStyle === $style)>{{ $style }}</option>
                                                            @endforeach
                                                        </select>
                                                    </div>
                                                    <div class="form-group col-md-3">
                                                        <label for="node_image">Node image</label>
                                                        <input class="form-control" id="node_image" name="node_image" list="node-image-options"
                                                               value="{{ old('node_image', data_get($map, 'node_defaults.image', '')) }}">
                                                        <datalist id="node-image-options">
                                                            @foreach ($nodeImages as $image)
                                                                <option value="{{ $image }}"></option>
                                                            @endforeach
                                                        </datalist>
                                                    </div>
                                                    <div class="form-group col-md-3">
                                                        <label for="edge_style">Link style</label>
                                                        <select class="form-control" id="edge_style" name="edge_style">
                                                            @if (!in_array($edgeStyle, $edgeStyles, true))
                                                                <option value="{{ $edgeStyle }}" selected>{{ $edgeStyle }} (current unsupported value)</option>
                                                            @endif
                                                            @foreach ($edgeStyles as $style)
                                                                <option value="{{ $style }}" @selected($edgeStyle === $style)>{{ $style }}</option>
                                                            @endforeach
                                                        </select>
                                                    </div>
                                                    @foreach ([
                                                        'node_size' => ['Node size', 'node_defaults.size', 25],
                                                        'node_text_size' => ['Node text size', 'node_defaults.text_size', 14],
                                                        'node_text_colour' => ['Node text colour', 'node_defaults.text_colour', '#243A54'],
                                                        'node_colour_bg' => ['Node background', 'node_defaults.colour_bg', '#D2E5FF'],
                                                        'node_colour_bdr' => ['Node border', 'node_defaults.colour_bdr', '#2B7CE9'],
                                                        'edge_text_size' => ['Link text size', 'edge_defaults.text_size', 12],
                                                        'edge_text_colour' => ['Link text colour', 'edge_defaults.text_colour', '#243A54'],
                                                    ] as $field => [$label, $path, $default])
                                                        <div class="form-group col-md-3">
                                                            <label for="{{ $field }}">{{ $label }}</label>
                                                            <input class="form-control" id="{{ $field }}" name="{{ $field }}"
                                                                   value="{{ old($field, data_get($map, $path, $default)) }}">
                                                        </div>
                                                    @endforeach
                                                </div>
                                            </div>
                                        </div>
                                    </div>

                                </div>
                            </div>
@endif
@if ($activeSubtab === 'configuration')
                                    @if (!$newMap)
                                        <div class="checkbox">
                                            <label>
                                                <input type="checkbox" name="scheduled" value="1"
                                                    @checked(old('scheduled', data_get($map, 'scheduled', false)))
                                                    @disabled(!in_array($selectedKey, $settings['initialized_maps'], true))>
                                                Reconcile with the external scheduler
                                            </label>
                                            @if (!in_array($selectedKey, $settings['initialized_maps'], true))
                                                <span class="help-block">A successful manual Apply is required first.</span>
                                            @else
                                                <span class="help-block">Scheduling is optional; run <code>libremapper reconcile</code> from cron or systemd.</span>
                                            @endif
                                        </div>
                                    @endif
@endif
<button class="btn btn-primary" type="submit">Save configuration</button>
</form>
@endif
@if ($activeSubtab === 'files')
                <div class="" id="files">
                    <p><strong>Advanced editing:</strong> edit the configuration files directly for provider-specific or unsupported settings.</p>
                    <p>A provider is a topology source. The default <code>librenms</code> provider uses the sidecar's existing LibreNMS database credentials. Configure the built-in Git source in Advanced → Oxidized connection; file and regex providers remain advanced alternatives configured here.</p>
                    <p>Load replaces unsaved text in the editor. Every save validates the full configuration tree. Managed files must be UTF-8 YAML or JSON and no larger than 5 MiB.</p>
                    <form method="GET" action="{{ route('plugin.settings', ['plugin' => 'libremapper']) }}" class="form-inline" style="margin-bottom: 16px">
                        <input type="hidden" name="tab" value="advanced"><input type="hidden" name="subtab" value="files"><input type="hidden" name="map" value="{{ $selectedKey }}">
                        <div class="form-group">
                            <label for="file">File</label>
                            <select class="form-control" id="file" name="file">
                                @foreach ($files as $file)
                                    <option value="{{ $file['path'] }}" @selected(!$newFile && $selectedFile && $file['path'] === $selectedFile['path'])>
                                        {{ $file['path'] }}
                                    </option>
                                @endforeach
                                <option value="__new__" @selected($newFile)>New file…</option>
                            </select>
                        </div>
                        <button class="btn btn-default" type="submit">Load</button>
                    </form>

                    @if ($selectedFile)
                        <div class="panel panel-default">
                            <div class="panel-heading lm-section-heading"><strong>{{ $selectedFile['path'] }} @include('libremapper::help', ['helpLabel' => 'Underlying configuration file', 'helpText' => 'A file can contain shared records or settings for multiple maps. Saving validates the configuration tree and makes affected maps drafts; review each affected map before publishing.'])</strong><span class="lm-scope lm-scope-shared">File scope</span></div>
                            <div class="panel-body">
                                <form method="POST" action="{{ route('libremapper.files.save') }}">
                                    @csrf <input type="hidden" name="map" value="{{ $selectedKey }}">
                                    @method('PUT')
                                    <input type="hidden" name="path" value="{{ $selectedFile['path'] }}">
                                    <input type="hidden" name="sha256" value="{{ $selectedFile['sha256'] }}">
                                    <div class="form-group">
                                        <label class="sr-only" for="file_content">{{ $selectedFile['path'] }}</label>
                                        <textarea class="form-control" rows="18" id="file_content" name="content" spellcheck="false">{{ old('content', $selectedFile['content']) }}</textarea>
                                    </div>
                                    <p class="help-block">SHA-256: <code>{{ $selectedFile['sha256'] }}</code></p>
                                    <label class="checkbox-inline">
                                        <input type="checkbox" name="force" value="1"> Overwrite if the file changed since it loaded
                                    </label>
                                    <button class="btn btn-primary" type="submit">Validate and save {{ $selectedFile['path'] }}</button>
                                </form>
                                <form method="POST" action="{{ route('libremapper.files.delete') }}" style="margin-top: 8px">
                                    @csrf <input type="hidden" name="map" value="{{ $selectedKey }}">
                                    @method('DELETE')
                                    <input type="hidden" name="path" value="{{ $selectedFile['path'] }}">
                                    <input type="hidden" name="sha256" value="{{ $selectedFile['sha256'] }}">
                                    <label class="checkbox-inline">
                                        <input type="checkbox" name="force" value="1"> Delete even if the file changed since it loaded
                                    </label>
                                    <button class="btn btn-danger" type="submit">Delete {{ $selectedFile['path'] }}</button>
                                </form>
                            </div>
                        </div>
                    @else
                        <div class="panel panel-default">
                            <div class="panel-heading"><strong>Create file</strong></div>
                            <div class="panel-body">
                                <form method="POST" action="{{ route('libremapper.files.create') }}">
                                    @csrf <input type="hidden" name="map" value="{{ $selectedKey }}">
                                    <div class="form-group">
                                        <label for="new_path">Relative path</label>
                                        <input class="form-control" id="new_path" name="path" placeholder="maps/new-map.yaml" value="{{ old('path') }}" required>
                                    </div>
                                    <div class="form-group">
                                        <label for="new_content">YAML or JSON</label>
                                        <textarea class="form-control" rows="18" id="new_content" name="content" spellcheck="false" required>{{ old('content') }}</textarea>
                                    </div>
                                    <button class="btn btn-primary" type="submit">Validate and create file</button>
                                </form>
                            </div>
                        </div>
                    @endif

                </div>
@elseif ($activeSubtab === 'maintenance')
                    <div class="panel panel-danger">
                        <div class="panel-heading lm-section-heading"><strong>Reset to defaults</strong><span class="lm-scope lm-scope-shared">All maps</span></div>
                        <div class="panel-body">
                            <p>Reset replaces every managed configuration file. The complete configuration tree is backed up under <code>/data/libremapper/backups</code> first.</p>
                            <form method="POST" action="{{ route('libremapper.config.reset') }}" class="form-inline">
                                @csrf <input type="hidden" name="map" value="{{ $selectedKey }}">
                                <label for="confirmation">Type RESET</label>
                                <input class="form-control" id="confirmation" name="confirmation" required>
                                <button class="btn btn-danger" type="submit">Back up and restore defaults</button>
                            </form>
                        </div>
                    </div>
@endif
<script>
$(function () {
    var form = $('#advanced-map-form');
    var pendingFields = @json(array_keys(session()->getOldInput()));
    form.find('input:not([type="hidden"]), select, textarea').each(function () { $(this).data('original', this.type === 'checkbox' ? this.checked : this.value); });
    form.on('submit', function () {
        form.find('input:not([type="hidden"]), select, textarea').each(function () {
            var changed = pendingFields.includes(this.name) || $(this).data('original') !== (this.type === 'checkbox' ? this.checked : this.value);
            if (this.type === 'checkbox') form.find('input[type="hidden"]').filter((i, field) => field.name === this.name).prop('disabled', !changed);
            this.disabled = this.disabled || !changed;
        });
    });
});
</script>
