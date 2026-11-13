                            <div class="panel panel-default">
                            <div class="panel-heading lm-section-heading"><strong>Review: {{ $selectedKey }}</strong><span class="lm-scope lm-scope-map">This map</span></div>
                            <div class="panel-body">
                                <p>Review reads current inventory and stores the exact devices and links to publish. Publishing rechecks configuration, topology, and LibreNMS state.</p>
                                <form method="POST" action="{{ route('libremapper.plan', ['map' => $selectedKey]) }}">
                                    @csrf
                                    <button class="btn btn-default btn-xs" type="button" data-toggle="collapse"
                                            data-target="#preview-options" aria-expanded="false" aria-controls="preview-options">Review options</button>
                                    <div class="collapse" id="preview-options">
                                        <div class="checkbox">
                                            <label><input type="checkbox" name="adopt" value="1"> Take over an existing map</label>
                                            <span class="text-muted">Allow Libremapper to manage a LibreNMS map with the same name.</span>
                                        </div>
                                        <div class="checkbox">
                                            <label><input type="checkbox" name="relayout" value="1"> Reposition all managed nodes</label>
                                            <span class="text-muted">Generate fresh coordinates instead of preserving existing positions.</span>
                                        </div>
                                    </div>
                                    <button class="btn btn-info" type="submit" @disabled($configError)>Review changes</button>
                                </form>

                                @include('libremapper::preview')

                                @if ($plan)
                                    <h4>{{ $plan['device_count'] }} {{ $plan['device_count'] === 1 ? 'device' : 'devices' }} · {{ $plan['link_count'] }} {{ $plan['link_count'] === 1 ? 'link' : 'links' }} · {{ $plan['changes'] }} {{ $plan['changes'] === 1 ? 'change' : 'changes' }}</h4>
                                    <p>{{ collect($plan['operations'])->where('action', 'create')->count() }} additions · {{ collect($plan['operations'])->where('action', 'update')->count() }} updates · <strong class="text-danger">{{ collect($plan['operations'])->where('action', 'delete')->count() }} removals</strong></p>
                                    @if ($plan['conflicts'])
                                        <div class="alert alert-danger" role="alert">
                                            <strong>{{ count($plan['conflicts']) }} conflicts block publishing.</strong>
                                            <button class="btn btn-link btn-xs" type="button" data-toggle="collapse"
                                                    data-target="#plan-conflicts" aria-expanded="false" aria-controls="plan-conflicts">Show details</button>
                                            <div class="collapse" id="plan-conflicts">
                                                <ul style="margin-top: 8px">
                                                    @foreach ($plan['conflicts'] as $conflict)
                                                        <li>{{ $conflict }}</li>
                                                    @endforeach
                                                </ul>
                                            </div>
                                        </div>
                                    @endif
                                    @if ($map['link_graph_overrides'] ?? [])
                                        <details><summary>Traffic interface choices</summary>
                                        @foreach ($map['link_graph_overrides'] as $override)
                                            <form method="POST" action="{{ route('libremapper.graph.save', ['map' => $selectedKey]) }}">@csrf @method('PUT')
                                                <input type="hidden" name="map_hash" value="{{ hash('sha256', json_encode($map, JSON_THROW_ON_ERROR)) }}"><input type="hidden" name="canonical_id" value="{{ $override['canonical_id'] }}"><input type="hidden" name="remove" value="1">
                                                {{ $override['canonical_id'] }} · {{ $override['port_id'] ?? 'Automatic' }} <button type="submit" class="btn btn-link btn-xs">Remove traffic choice</button>
                                            </form>
                                        @endforeach
                                        </details>
                                    @endif
                                    @if ($plan['warnings'])
                                        <div class="alert alert-warning" role="alert">
                                            <strong>{{ count($plan['warnings']) }} warnings require review:</strong>
                                            {{ collect($plan['warning_groups'])->map(fn ($group) => $group['count'].' × '.$group['label'])->implode(' · ') }}
                                            <button class="btn btn-link btn-xs" type="button" data-toggle="collapse"
                                                    data-target="#plan-warnings" aria-expanded="false" aria-controls="plan-warnings">Show details</button>
                                            <div class="collapse" id="plan-warnings">
                                                <ul style="margin-top: 8px">
                                                    @foreach ($plan['warnings'] as $warning)
                                                        <li>{{ $warning }}</li>
                                                    @endforeach
                                                </ul>
                                            </div>
                                        </div>
                                    @endif

                                    @if ($plan['changes'])
                                        <button class="btn btn-default btn-xs" type="button" data-toggle="collapse"
                                                data-target="#plan-changes" aria-expanded="false" aria-controls="plan-changes">
                                            Show planned changes ({{ $plan['changes'] }})
                                        </button>
                                        <div class="collapse" id="plan-changes" style="margin-top: 8px">
                                            <div class="table-responsive">
                                                <table class="table table-condensed table-striped">
                                                    <thead><tr><th>Action</th><th>Kind</th><th>Object</th><th>Preserved fields</th></tr></thead>
                                                    <tbody>
                                                        @foreach ($plan['operations'] as $operation)
                                                            @if (($operation['action'] ?? '') !== 'noop')
                                                                <tr class="{{ $operation['action'] === 'delete' ? 'danger' : '' }}">
                                                                    <td>{{ $actionLabels[$operation['action']] ?? $operation['action'] }}</td>
                                                                    <td>{{ $kindLabels[$operation['kind']] ?? $operation['kind'] }}</td>
                                                                    <td>
                                                                        <strong>{{ $operation['display_name'] ?? $operation['canonical_id'] }}</strong>
                                                                        @if ($operation['reconciliation_reason'] ?? null)
                                                                            <br>{{ $operation['reconciliation_reason'] }}
                                                                        @endif
                                                                    </td>
                                                                    <td>{{ implode(', ', $operation['preserved_fields'] ?? []) }}</td>
                                                                </tr>
                                                            @endif
                                                        @endforeach
                                                    </tbody>
                                                </table>
                                            </div>
                                        </div>
                                    @else
                                        <p>No map changes are required.</p>
                                    @endif
                                    <form method="POST" action="{{ route('libremapper.apply', ['map' => $selectedKey]) }}">
                                        @csrf
                                        <input type="hidden" name="plan_sha256" value="{{ $plan['plan_sha256'] }}">
                                        @if ($plan['warnings'])
                                            <label class="checkbox-inline"><input type="checkbox" name="confirm_warnings" value="1" required> I reviewed the warnings</label>
                                        @endif
                                        <button class="btn btn-danger" type="submit" @disabled($configError || $plan['conflicts'])>Publish to LibreNMS</button>
                                    </form>
                                @endif
                            </div>
                            </div>
