                    <div class="panel panel-default">
                        <div class="panel-heading lm-section-heading"><strong>Oxidized connection @include('libremapper::help', ['helpLabel' => 'Shared Git source', 'helpText' => 'The repository and fetch credentials are shared by maps using this source. Changing this connection is separate from enabling parsers on one map.'])</strong><span class="lm-scope lm-scope-shared">Shared across maps</span></div>
                        <div class="panel-body">
                            <p>Build connections from Oxidized configuration backups and command output. The Git connection is shared. Enable parsers for each map in Links → Parsers.</p>
                            @if ($oxidizedMaps)<p><strong>Maps referencing this shared source:</strong> {{ implode(', ', $oxidizedMaps) }}. Disable their parsers before disconnecting it.</p>@endif
                            <form method="POST" action="{{ route('libremapper.oxidized.update') }}">
                                @csrf <input type="hidden" name="map" value="{{ $selectedKey }}"><input type="hidden" name="sha256" value="{{ $mainSha256 }}">
                                <div class="row">
                                    <div class="form-group col-md-3">
                                        <label for="oxidized_mode">Source</label> @include('libremapper::help', ['helpLabel' => 'Source connection', 'helpText' => 'Choose a remote Git repository or a local mount. Disabled disconnects the shared source; first turn off its parser selections in every map using it.'])
                                        <select class="form-control" id="oxidized_mode" name="mode">
                                            <option value="disabled" @selected(old('mode', $oxidizedMode) === 'disabled')>Disabled</option>
                                            <option value="gitea" @selected(old('mode', $oxidizedMode) === 'gitea')>Remote Git repository</option>
                                            <option value="local" @selected(old('mode', $oxidizedMode) === 'local')>Local Oxidized repository</option>
                                        </select>
                                    </div>
                                    <div id="oxidized_remote_fields" @if(old('mode', $oxidizedMode) !== 'gitea') hidden @endif>
                                        <div class="form-group col-md-4">
                                            <label for="oxidized_repository">Git SSH URL</label>
                                            <input class="form-control" id="oxidized_repository" name="repository"
                                                   value="{{ old('repository', $oxidizedMode === 'gitea' ? $oxidizedRepository : '') }}"
                                                   placeholder="git@gitea.example.net:network/oxidized.git"
                                                   @required(old('mode', $oxidizedMode) === 'gitea')>
                                        </div>
                                        <div class="form-group col-md-5">
                                            <label for="oxidized_password">Read-only fetch key</label>
                                            <textarea class="form-control" id="oxidized_password" name="password" rows="3"
                                                      autocomplete="off" spellcheck="false"
                                                      placeholder="Paste the private key; leave blank to keep the saved key"></textarea>
                                        </div>
                                    </div>
                                    <div id="oxidized_local_help" class="col-md-9" @if(old('mode', $oxidizedMode) !== 'local') hidden @endif>
                                        <p class="help-block">Mount the local Oxidized bare repository or checked-out worktree read-only at <code>/oxidized.git</code>.</p>
                                    </div>
                                </div>
                                <input type="hidden" name="ref" value="{{ old('ref', $oxidizedRef) }}">
                                <p id="oxidized_remote_help" class="help-block" @if(old('mode', $oxidizedMode) !== 'gitea') hidden @endif>The key is stored with owner-only access in Libremapper's data volume, never in YAML. The first successful connection pins the Git server identity; later identity changes are refused.</p>
                                <button class="btn btn-default" type="submit">Save and test source</button>
                                @if ($oxidizedProvider)
                                    <span class="text-success" style="margin-left: 8px">Configured</span>
                                @endif
                            </form>
                        </div>
                    </div>
