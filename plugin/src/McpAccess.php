<?php

namespace Libremapper\Plugin;

use App\Models\ApiToken;
use App\Models\Plugin;
use App\Models\User;
use Illuminate\Database\Eloquent\Builder;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Http\Request;
use Illuminate\Support\Collection;

class McpAccess
{
    public const LEVELS = ['inspect' => 1, 'edit' => 2, 'publish' => 3];

    public static function plugin(): Plugin
    {
        return Plugin::query()->where('plugin_name', Engine::PLUGIN)->where('version', 2)->firstOrFail();
    }

    public static function tokens(User $user): Builder
    {
        return method_exists($user, 'tokens') ? $user->tokens()->getQuery() : ApiToken::where('user_id', $user->user_id);
    }

    public static function tokenOptions(User $user): Collection
    {
        $modern = method_exists($user, 'tokens');
        return self::tokens($user)->orderBy('id')->get($modern ? ['id', 'name', 'expires_at', 'created_at'] : ['id', 'description', 'disabled'])
            ->map(fn (Model $token): object => (object) ['id' => $token->id, 'description' => $modern ? $token->name : $token->description,
                'disabled' => ! self::active($token), 'profile_key' => self::profileKey($token)]);
    }

    public static function profileKey(Model $token): string
    {
        // Native migration can reassign numeric IDs; older profiles must not grant a different token access.
        return ($token->getTable() === 'api_tokens' ? '' : $token->getTable().'-').$token->getKey();
    }

    public static function token(Request $request): Model
    {
        abort_unless($request->user()?->can('plugin.admin'), 403);
        foreach (['api_token', 'access_token', 'token'] as $name) abort_if($request->query->has($name), 400, 'Credentials must be sent in headers.');
        $bearer = $request->bearerToken();
        $header = $request->header('X-Auth-Token');
        abort_if($bearer && $header && ! hash_equals($bearer, $header), 400, 'Conflicting credentials.');
        $secret = $header ?: $bearer;
        abort_unless(is_string($secret) && $secret !== '', 401);
        $query = self::tokens($request->user());
        if (method_exists($request->user(), 'tokens')) {
            $model = $query->getModel();
            $found = $model::findToken($secret);
            $token = $found ? $query->whereKey($found->getKey())->first() : null;
        } else {
            $token = $query->where('token_hash', $secret)->first();
        }
        abort_unless($token && self::active($token), 401);
        $settings = (array) self::plugin()->settings;
        abort_unless($settings['mcp_enabled'] ?? false, 404);
        abort_unless(isset(self::LEVELS[$settings['mcp_token_profiles'][self::profileKey($token)] ?? '']), 403);
        return $token;
    }

    public static function level(Model $token): int
    {
        $settings = (array) self::plugin()->settings;
        $fresh = $token->fresh();
        return ($settings['mcp_enabled'] ?? false) && $fresh && self::active($fresh)
            ? (self::LEVELS[$settings['mcp_token_profiles'][self::profileKey($fresh)] ?? ''] ?? 0) : 0;
    }

    private static function active(Model $token): bool
    {
        if ($token->getTable() === 'api_tokens') return $token->disabled === false;
        $expiration = config('sanctum.expiration');
        return (! $token->expires_at || ! $token->expires_at->isPast())
            && (! $expiration || $token->created_at->gt(now()->subMinutes($expiration)));
    }

    public static function skill(): string
    {
        return (string) file_get_contents(__DIR__.'/../../skills/libremapper/SKILL.md');
    }
}
