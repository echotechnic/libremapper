<?php

use Illuminate\Support\Facades\Route;
use Libremapper\Plugin\Http\McpController;

Route::middleware(['api', 'can:plugin.admin', 'throttle:120,1'])->group(function (): void {
    Route::match(['GET', 'POST', 'DELETE'], 'api/v0/libremapper/mcp', [McpController::class, 'handle'])->withoutMiddleware(\App\Http\Middleware\EnforceJson::class)->name('libremapper.mcp');
});
