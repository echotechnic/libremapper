<?php

namespace Libremapper\Plugin\Http;

use App\Facades\LibrenmsConfig;
use GuzzleHttp\Psr7\HttpFactory;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;
use Libremapper\Plugin\Engine;
use Libremapper\Plugin\McpAccess;
use Libremapper\Plugin\McpToolHandler;
use Libremapper\Plugin\McpTools;
use Mcp\Schema\Tool;
use Mcp\Schema\ToolAnnotations;
use Mcp\Server;
use Mcp\Server\Session\FileSessionLock;
use Mcp\Server\Session\FileSessionStore;
use Mcp\Server\Transport\Http\Middleware\DnsRebindingProtectionMiddleware;
use Mcp\Server\Transport\StreamableHttpTransport;
use Symfony\Bridge\PsrHttpMessage\Factory\HttpFoundationFactory;
use Symfony\Bridge\PsrHttpMessage\Factory\PsrHttpFactory;
use Throwable;

class McpController
{
    public function handle(Request $request, Engine $engine)
    {
        $token = McpAccess::token($request);
        $factory = new HttpFactory;
        $psr = (new PsrHttpFactory($factory, $factory, $factory, $factory))->createRequest($request);
        $publicUrl = rtrim((string) LibrenmsConfig::get('base_url', config('app.url')), '/');
        $host = parse_url($publicUrl, PHP_URL_HOST);
        abort_unless(is_string($host) && $host !== '', 503, 'Configure the LibreNMS public URL before enabling MCP.');
        $origin = parse_url($publicUrl, PHP_URL_SCHEME).'://'.$host.(parse_url($publicUrl, PHP_URL_PORT) ? ':'.parse_url($publicUrl, PHP_URL_PORT) : '');
        abort_unless(strcasecmp($request->getHost(), $host) === 0, 403);
        abort_if($request->headers->has('Origin') && rtrim($request->header('Origin'), '/') !== $origin, 403);
        try {
            $plugin = McpAccess::plugin();
            $tools = new McpTools($engine, $plugin, $request, $token);
            $directory = rtrim(getenv('LIBREMAPPER_DATA_ROOT') ?: '/data/libremapper', '/').'/mcp-sessions/'.$request->user()->user_id.'-'.McpAccess::profileKey($token);
            $builder = Server::builder()->setServerInfo('Libremapper', \Composer\InstalledVersions::getPrettyVersion('echotechnic/libremapper') ?? 'development')
                ->setInstructions('Read libremapper://skill before editing maps. Inspect last stored network status, edit drafts, review every change, and publish only the exact authorized plan. Whole-map deletion is unavailable.')
                ->setSession(new FileSessionStore($directory))->setSessionLock(new FileSessionLock($directory, 0));
            $level = McpAccess::level($token);
            foreach (McpTools::definitions() as $name => $definition) {
                if ($level < $definition['level']) continue;
                $builder->add(new Tool($name, null, $definition['inputSchema'], $definition['description'],
                    new ToolAnnotations(readOnlyHint: $definition['level'] === 1, destructiveHint: $definition['level'] > 1, openWorldHint: false),
                    outputSchema: $definition['outputSchema']), new McpToolHandler($tools, $name));
            }
            $builder->addResource(fn (): string => McpAccess::skill(), 'libremapper://skill', name: 'libremapper', description: 'Agent skill for network inspection and reviewed map edits.', mimeType: 'text/markdown');
            $builder->addPrompt(fn (): array => [['role' => 'user', 'content' => ['type' => 'text', 'text' => McpAccess::skill()]]], name: 'libremapper_workflow', description: 'Load the Libremapper agent skill.');
            $response = $builder->build()->run(new StreamableHttpTransport($psr, $factory, $factory,
                middleware: [new DnsRebindingProtectionMiddleware(allowedHosts: [$host], allowedOrigins: [], responseFactory: $factory, streamFactory: $factory)], maxBodyBytes: 65536));
            return (new HttpFoundationFactory)->createResponse($response);
        } catch (Throwable $error) {
            Log::error('Libremapper MCP transport failed', ['exception' => $error::class, 'user_id' => $request->user()->user_id, 'token_id' => $token->id]);
            return response()->json(['error' => 'MCP operation failed. Check current map status before retrying.'], 500);
        }
    }

    public function configure(Request $request)
    {
        $validated = $request->validate(['mcp_enabled' => 'required_without:token_id|boolean', 'token_id' => 'required_without:mcp_enabled|integer|min:1', 'profile' => 'required_with:token_id|in:off,inspect,edit,publish']);
        $token = null;
        if (isset($validated['token_id'])) {
            abort_unless($request->user()->can('api.access'), 403);
            $token = McpAccess::tokens($request->user())->findOrFail($validated['token_id']);
        }
        DB::transaction(function () use ($validated, $token): void {
            $plugin = McpAccess::plugin()->newQuery()->whereKey(McpAccess::plugin()->getKey())->lockForUpdate()->firstOrFail();
            $settings = (array) $plugin->settings;
            if (array_key_exists('mcp_enabled', $validated)) $settings['mcp_enabled'] = (bool) $validated['mcp_enabled'];
            if (isset($validated['token_id'])) {
                $profileKey = McpAccess::profileKey($token);
                unset($settings['mcp_token_profiles'][$profileKey]);
                if (($validated['profile'] ?? 'off') !== 'off') $settings['mcp_token_profiles'][$profileKey] = $validated['profile'];
            }
            $plugin->settings = $settings;
            $plugin->saveOrFail();
        });
        return redirect()->route('plugin.settings', ['plugin' => 'libremapper', 'tab' => 'advanced', 'subtab' => 'agent-access', 'map' => $request->input('map')])->with('status', 'Agent access saved.');
    }

    public function skill()
    {
        return response(McpAccess::skill(), 200, ['Content-Type' => 'text/markdown; charset=UTF-8', 'Content-Disposition' => 'attachment; filename="SKILL.md"']);
    }
}
