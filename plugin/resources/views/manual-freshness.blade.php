<input type="hidden" name="manual_sha256" value="{{ $manualCatalog['sha256'] }}">
<input type="hidden" name="map_hash" value="{{ hash('sha256', json_encode($map, JSON_THROW_ON_ERROR)) }}">
