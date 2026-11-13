import json
import runpy
from pathlib import Path


def test_mcp_map_schema_tracks_model_and_rejects_unexposed_fields() -> None:
    root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(str(root / "scripts/generate-mcp-schema"))
    schema = namespace["map_schema"]()
    assert json.loads((root / "plugin/resources/mcp-map-schema.json").read_text()) == schema
    assert {"key", "example", "providers", "librenms"}.isdisjoint(schema["properties"])
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["SourceConfig"]["additionalProperties"] is False
    assert schema["properties"]["sources"]["type"] == "array"
