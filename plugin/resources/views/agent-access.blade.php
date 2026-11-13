<div class="panel panel-default" id="agent-access">
    <div class="panel-heading lm-section-heading"><strong>Agent access @include('libremapper::help', ['helpLabel' => 'Global agent access', 'helpText' => 'MCP enablement and token profiles apply across the workspace immediately. They do not wait for map publication.'])</strong><span class="lm-scope lm-scope-shared">All maps</span></div>
    <div class="panel-body">
        <p>Connect agents to <code>{{ route('libremapper.mcp') }}</code> using Streamable HTTP and an existing LibreNMS API token in the <code>Authorization: Bearer</code> or <code>X-Auth-Token</code> header. Each token must belong to a plugin administrator.</p>
        <form method="POST" action="{{ route('libremapper.mcp.configure') }}" style="margin-bottom: 16px">
            @csrf <input type="hidden" name="map" value="{{ $selectedKey }}">
            <input type="hidden" name="mcp_enabled" value="0">
            <label><input type="checkbox" name="mcp_enabled" value="1" @checked($settings['mcp_enabled'] ?? false)> Enable MCP through this LibreNMS instance</label>
            <button class="btn btn-default" type="submit">Save agent access</button>
        </form>
        <p>Profiles restrict MCP operations. The token retains its existing permissions elsewhere in LibreNMS. Shared manual edits can affect multiple maps; whole-map deletion is unavailable.</p>
        @if (auth()->user()?->can('api.access'))
            <p>Create, rotate, or revoke your tokens in <a href="{{ route('api-access.index') }}">LibreNMS API access</a>. Token secrets are not displayed here.</p>
            @foreach ($agentTokens as $agentToken)
                <form class="form-inline" method="POST" action="{{ route('libremapper.mcp.configure') }}" style="margin-bottom: 8px">
                    @csrf <input type="hidden" name="map" value="{{ $selectedKey }}"><input type="hidden" name="token_id" value="{{ $agentToken->id }}">
                    <label for="agent-profile-{{ $agentToken->id }}">{{ $agentToken->description ?: 'Token '.$agentToken->id }}{{ $agentToken->disabled ? ' (disabled)' : '' }}</label>
                    <select class="form-control" id="agent-profile-{{ $agentToken->id }}" name="profile">
                        @foreach (['off' => 'No MCP access', 'inspect' => 'Inspect', 'edit' => 'Edit drafts and review', 'publish' => 'Edit and publish'] as $profile => $label)
                            <option value="{{ $profile }}" @selected(($settings['mcp_token_profiles'][$agentToken->profile_key] ?? 'off') === $profile)>{{ $label }}</option>
                        @endforeach
                    </select>
                    <button class="btn btn-default" type="submit">Save token profile</button>
                </form>
            @endforeach
        @endif
        <p><a href="{{ route('libremapper.mcp.skill') }}">Download the Libremapper agent skill</a>. Agents can also read <code>libremapper://skill</code> or use the <code>libremapper_workflow</code> prompt. Clients must support configured token headers; no OAuth login flow is provided.</p>
    </div>
</div>
