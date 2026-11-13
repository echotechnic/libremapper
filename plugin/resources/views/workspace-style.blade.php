<style>
#libremapper-settings {
    --lm-map:#236796; --lm-shared:#875b16; --lm-label:#236796; --lm-layer:#70519a;
    --lm-surface:#fff; --lm-muted:#f8fafc; --lm-border:#dce2e8; --lm-help:#536b80; --lm-hidden:#8a5600; --lm-link:#1768a0;
    --lm-label-bg:#f4f8fc; --lm-label-border:#b8d4e9; --lm-label-selected:#deedf9;
    --lm-layer-bg:#f8f5fc; --lm-layer-border:#d2c2e5; --lm-layer-selected:#eae0f7;
    --lm-shared-bg:#fff8e9; --lm-shared-border:#e4ce9f;
}
.dark #libremapper-settings {
    --lm-map:#9bcbf0; --lm-shared:#f0ca84; --lm-label:#9bcbf0; --lm-layer:#cfb1f1;
    --lm-surface:#252b33; --lm-muted:#20262e; --lm-border:#485260; --lm-help:#b4c8dc; --lm-hidden:#f0ca84; --lm-link:#80c7ff;
    --lm-label-bg:#233545; --lm-label-border:#486d8b; --lm-label-selected:#304d65;
    --lm-layer-bg:#322b40; --lm-layer-border:#6c5688; --lm-layer-selected:#4a3b5f;
    --lm-shared-bg:#3c3324; --lm-shared-border:#8b6e38;
}
#libremapper-settings .lm-scope-key { display:flex; flex-wrap:wrap; align-items:center; gap:8px 20px; padding:10px 14px; margin:14px 0; border:1px solid var(--lm-border); border-radius:5px; background:var(--lm-muted); font-size:12px; }
#libremapper-settings .lm-scope-key > span { display:inline-flex; align-items:center; gap:7px; }
#libremapper-settings .lm-scope { display:inline-block; padding:3px 8px; border:1px solid; border-radius:12px; font-size:11px; font-weight:600; line-height:1.4; vertical-align:middle; white-space:nowrap; }
#libremapper-settings .lm-scope-map { color:var(--lm-map); background:var(--lm-label-bg); border-color:var(--lm-label-border); }
#libremapper-settings .lm-scope-shared { color:var(--lm-shared); background:var(--lm-shared-bg); border-color:var(--lm-shared-border); }
#libremapper-settings .lm-scope-preview { color:var(--lm-help); background:var(--lm-muted); border-color:var(--lm-border); }
#libremapper-settings .lm-help { border:0; background:transparent; color:var(--lm-help); padding:2px 5px; font-size:14px; line-height:1.2; vertical-align:middle; cursor:help; }
#libremapper-settings .lm-help:hover { color:var(--lm-map); }
#libremapper-settings .lm-help:focus { outline:2px solid var(--lm-map); outline-offset:2px; border-radius:3px; }
#libremapper-settings .workspace-subtabs { padding-bottom:10px; border-bottom:1px solid var(--lm-border); }
#libremapper-settings .lm-section { padding:18px; margin-bottom:20px; border:1px solid var(--lm-border); border-radius:5px; background:var(--lm-surface); }
#libremapper-settings .lm-section > h4:first-child { margin-top:0; }
#libremapper-settings .table-responsive { position:relative; max-width:100%; overflow-x:auto; }
#libremapper-settings .lm-shared-section { border-top:3px solid #c59b50; }
#libremapper-settings .lm-map-section { border-top:3px solid #5b96be; }
#libremapper-settings .lm-form-section { min-width:0; margin:0 0 18px; padding:0 0 16px; border:0; border-bottom:1px solid var(--lm-border); }
#libremapper-settings .lm-form-section > legend { margin-bottom:12px; padding:0; border:0; font-size:16px; font-weight:600; }
#libremapper-settings .lm-section-heading { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
#libremapper-settings .lm-section-heading > h3, #libremapper-settings .lm-section-heading > h4 { margin:0; }
#libremapper-settings .lm-device-filter { display:flex; align-items:flex-end; flex-wrap:wrap; gap:10px 20px; padding:14px; margin-bottom:14px; background:var(--lm-muted); border:1px solid var(--lm-border); border-radius:5px; }
#libremapper-settings .lm-device-filter .form-group { flex:1; min-width:220px; margin:0; }
#libremapper-settings .lm-device-filter .checkbox { margin:0 0 6px; }
#libremapper-settings .lm-bulk-controls { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:16px; margin-bottom:16px; }
#libremapper-settings .lm-bulk-lane { min-width:0; margin:0; padding:16px; border:1px solid var(--lm-label-border); border-left:4px solid var(--lm-label); border-radius:5px; background:var(--lm-label-bg); }
#libremapper-settings .lm-bulk-lane.lm-layer-lane { border-color:var(--lm-layer-border); border-left-color:var(--lm-layer); background:var(--lm-layer-bg); }
#libremapper-settings .lm-bulk-lane fieldset { min-width:0; }
#libremapper-settings .lm-bulk-lane legend { margin-bottom:12px; border:0; font-size:16px; font-weight:600; }
#libremapper-settings .lm-bulk-value { display:flex; flex-wrap:wrap; align-items:center; gap:8px 12px; margin-bottom:8px; }
#libremapper-settings .lm-bulk-value .form-control { width:110px; }
#libremapper-settings .lm-lane-actions { display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
#libremapper-settings .lm-impact { padding:10px 0; min-height:40px; font-weight:600; }
#libremapper-settings .lm-selection-meta { font-size:12px; margin-bottom:6px; }
#libremapper-settings .lm-selection-hidden { color:var(--lm-hidden); font-weight:600; }
#libremapper-settings .lm-lane-note { margin:8px 0 0; padding-top:8px; border-top:1px solid var(--lm-layer-border); font-size:12px; }
#libremapper-settings #device-list .lm-hostname-cell { border-left:2px solid var(--lm-label-border); background:var(--lm-label-bg); min-width:185px; }
#libremapper-settings #device-list .lm-placement-cell { border-left:2px solid var(--lm-layer-border); background:var(--lm-layer-bg); min-width:185px; }
#libremapper-settings #device-list .lm-hostname-cell.lm-selected { background:var(--lm-label-selected); box-shadow:inset 3px 0 var(--lm-label); }
#libremapper-settings #device-list .lm-placement-cell.lm-selected { background:var(--lm-layer-selected); box-shadow:inset 3px 0 var(--lm-layer); }
#libremapper-settings a:not(.btn), #libremapper-settings .btn-link, #libremapper-settings summary { color:var(--lm-link); }
#libremapper-settings a:not(.btn):hover, #libremapper-settings .btn-link:hover, #libremapper-settings summary:hover { text-decoration:underline; }
#libremapper-settings summary { cursor:pointer; }
#libremapper-settings a:focus-visible, #libremapper-settings button:focus-visible, #libremapper-settings summary:focus-visible { outline:2px solid var(--lm-link); outline-offset:2px; }
#libremapper-settings .nav-pills > .active > a { color:#fff; background:#236796; }
#libremapper-settings .nav-tabs > .active > a { color:var(--lm-map); font-weight:600; }
#libremapper-settings .lm-action-group { display:flex; align-items:center; flex-wrap:wrap; gap:4px 10px; }
#libremapper-settings .lm-action-group form { margin:0; }
#libremapper-settings .lm-catalog-table td { vertical-align:middle; }
#libremapper-settings .lm-editor-tabs { margin:0 0 16px; }
#libremapper-settings .lm-parser-choice { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:6px 16px; padding:12px; margin:0; border-bottom:1px solid var(--lm-border); }
#libremapper-settings .lm-parser-choice:last-of-type { margin-bottom:12px; }
#libremapper-settings .lm-parser-choice label { flex:1; min-width:200px; }
#libremapper-settings .lm-save-bar { margin:16px 0; padding-top:14px; border-top:1px solid var(--lm-border); }
.dark #libremapper-settings .preview-label { fill:#e5edf5; }
.dark #libremapper-settings .preview-tier { fill:#b2c1ce; }
@media (max-width:767px) {
    #libremapper-settings .lm-bulk-controls { grid-template-columns:minmax(0,1fr); }
    #libremapper-settings .lm-section { padding:12px; }
    #libremapper-settings #map-header { position:static !important; }
}
</style>
