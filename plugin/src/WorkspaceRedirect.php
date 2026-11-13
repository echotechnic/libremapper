<?php

namespace Libremapper\Plugin;

use Closure;
use Illuminate\Http\Request;
use Libremapper\Plugin\Http\LibremapperController;

class WorkspaceRedirect
{
    public function handle(Request $request, Closure $next): mixed
    {
        $tab = $request->query('tab');
        if ($request->isMethod('GET') && $request->is('plugin/settings/libremapper')
            && is_string($tab) && (in_array($tab, ['guided', 'oxidized', 'files'], true)
                || ($tab === 'links' && $request->query('subtab') === 'oxidized'))) {
            $subtab = match ($tab) { 'oxidized', 'links' => 'parsers', 'files' => 'files', default => null };
            return redirect()->to($request->fullUrlWithQuery(['tab' => LibremapperController::workspaceTab($tab)] + ($subtab ? ['subtab' => $subtab] : [])));
        }
        return $next($request);
    }
}
