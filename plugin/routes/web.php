<?php

use Illuminate\Support\Facades\Route;
use Libremapper\Plugin\Http\LibremapperController;
use Libremapper\Plugin\Http\McpController;

Route::middleware(['web', 'auth', 'can:plugin.admin'])->prefix('plugin/libremapper')->group(function (): void {
    Route::post('/agent-access', [McpController::class, 'configure'])->name('libremapper.mcp.configure');
    Route::get('/agent-skill', [McpController::class, 'skill'])->name('libremapper.mcp.skill');
    Route::get('/', [LibremapperController::class, 'index'])->name('libremapper.index');
    Route::get('/devices/{device}/interfaces', [LibremapperController::class, 'interfaceOptions'])->whereNumber('device')->name('libremapper.interfaces');
    Route::post('/maps/{map}/connections', [LibremapperController::class, 'saveManualLink'])->name('libremapper.connections.save');
    Route::put('/maps/{map}/connections', [LibremapperController::class, 'saveManualLink'])->name('libremapper.connections.edit');
    Route::post('/maps/{map}/manual-devices', [LibremapperController::class, 'saveManualDevice'])->name('libremapper.manual-devices.save');
    Route::put('/maps/{map}/manual-devices/inclusion', [LibremapperController::class, 'includeManualDevice'])->name('libremapper.manual-devices.inclusion');
    Route::delete('/maps/{map}/manual-devices', [LibremapperController::class, 'removeManualDevice'])->name('libremapper.manual-devices.remove');
    Route::put('/maps/{map}/traffic-interface', [LibremapperController::class, 'saveGraphOverride'])->name('libremapper.graph.save');
    Route::put('/maps/{map}/link-selection', [LibremapperController::class, 'saveLinkSelection'])->name('libremapper.links.selection');
    Route::put('/config', [LibremapperController::class, 'update'])->name('libremapper.config.update');
    Route::post('/oxidized', [LibremapperController::class, 'updateOxidized'])->name('libremapper.oxidized.update');
    Route::post('/maps/{map}/parsers', [LibremapperController::class, 'saveParserSelection'])->name('libremapper.parsers.selection');
    Route::post('/parsers', [LibremapperController::class, 'saveParser'])->name('libremapper.parsers.save');
    Route::post('/parsers/action', [LibremapperController::class, 'parserAction'])->name('libremapper.parsers.action');
    Route::post('/parsers/preview', [LibremapperController::class, 'parserPreview'])->name('libremapper.parsers.preview');
    Route::post('/parsers/files', [LibremapperController::class, 'parserPreview'])->name('libremapper.parsers.files');
    Route::post('/files', [LibremapperController::class, 'createFile'])->name('libremapper.files.create');
    Route::put('/files', [LibremapperController::class, 'saveFile'])->name('libremapper.files.save');
    Route::delete('/files', [LibremapperController::class, 'deleteFile'])->name('libremapper.files.delete');
    Route::post('/config/reset', [LibremapperController::class, 'reset'])->name('libremapper.config.reset');
    Route::post('/maps/{map}/plan', [LibremapperController::class, 'plan'])->name('libremapper.plan');
    Route::post('/maps/{map}/apply', [LibremapperController::class, 'apply'])->name('libremapper.apply');
    Route::post('/maps/{map}/manual-links', [LibremapperController::class, 'addManualLink'])->name('libremapper.manual-links.add');
    Route::put('/maps/{map}/manual-links/inclusion', [LibremapperController::class, 'includeManualLink'])->name('libremapper.manual-links.inclusion');
    Route::delete('/maps/{map}/manual-links', [LibremapperController::class, 'removeManualLink'])->name('libremapper.manual-links.remove');
});
