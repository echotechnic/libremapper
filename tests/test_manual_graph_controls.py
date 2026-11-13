from pathlib import Path

import pytest
import yaml
from sqlalchemy import Engine, text
from test_reconciliation import manual_service

from libremapper.config import LinkGraphOverride, SourceConfig
from libremapper.providers.file import FileTopology


def test_manual_connection_references_inventory_without_a_manual_device(
    tmp_path: Path, custom_map_engine: Engine,
) -> None:
    mapper, _, _ = manual_service(tmp_path, custom_map_engine)
    with custom_map_engine.begin() as connection:
        connection.exec_driver_sql(
            'CREATE TABLE devices (device_id INTEGER PRIMARY KEY, hostname TEXT)'
        )
        connection.exec_driver_sql("INSERT INTO devices VALUES (1, 'a.site.example.net')")
    manual_path = tmp_path / 'manual.yaml'
    manual_path.write_text(yaml.safe_dump({
        'devices': [{'source_id': 'outside', 'hostname': 'external'}],
        'links': [{
            'source_id': 'external-link', 'protocol': 'manual',
            'a': {'device': 'librenms:1', 'interface': 'label',
                  'interface_definition': {'source_id': 'label', 'device_source_id': 'librenms:1',
                                           'name': 'WAN',
                                           'attributes': {'manual_label_only': True}}},
            'b': {'device': 'outside'},
        }],
    }))
    desired = mapper.build_desired(mapper.config.maps[0])
    assert {node.device_id for node in desired.nodes.values()} == {1, 2, None}
    assert any('WAN' in name for name in desired.edge_display_names.values())
    assert yaml.safe_load(manual_path.read_text())['devices'] == [
        {'source_id': 'outside', 'hostname': 'external'}
    ]
    mapper.config.maps[0].manual_device_inclusions = ['librenms:1']
    manual_path.write_text(yaml.safe_dump({'devices': [], 'links': []}))
    desired = mapper.build_desired(mapper.config.maps[0])
    assert {node.device_id for node in desired.nodes.values()} == {1, 2}
    mapper.config.maps[0].sources = []
    desired = mapper.build_desired(mapper.config.maps[0])
    assert {node.device_id for node in desired.nodes.values()} == {1}
    assert next(iter(desired.nodes.values())).label == 'a.site.example.net'
    with custom_map_engine.begin() as connection:
        connection.exec_driver_sql('DELETE FROM devices')
    with pytest.raises(ValueError, match='LibreNMS device 1 is missing'):
        mapper.build_desired(mapper.config.maps[0])


def test_inventory_reference_requires_matching_interface_definition() -> None:
    with pytest.raises(ValueError, match='unknown topology interface'):
        FileTopology.model_validate({'links': [{
            'source_id': 'link', 'a': {'device': 'librenms:1', 'interface': 'missing'},
            'b': {'device': 'librenms:2'},
        }]})


def test_inventory_override_preserves_manual_layer_and_interface(
    tmp_path: Path, custom_map_engine: Engine,
) -> None:
    mapper, _, _ = manual_service(tmp_path, custom_map_engine)
    with custom_map_engine.begin() as connection:
        connection.exec_driver_sql(
            'CREATE TABLE devices (device_id INTEGER PRIMARY KEY, hostname TEXT)'
        )
        connection.exec_driver_sql("INSERT INTO devices VALUES (1, 'a.site.example.net')")
    config = mapper.config.maps[0]
    config.layout.infer_layers = False
    manual_path = tmp_path / 'manual.yaml'
    interface = {'source_id': 'uplink', 'device_source_id': 'a', 'name': 'WAN'}
    link = {'source_id': 'external-link', 'protocol': 'manual',
            'a': {'device': 'a', 'interface': 'uplink'}, 'b': {'device': 'outside'}}
    manual_path.write_text(yaml.safe_dump({
        'devices': [{'source_id': 'a', 'librenms_device_id': 1, 'layer': -4,
                     'interfaces': [interface]}, {'source_id': 'outside', 'hostname': 'external'}],
        'links': [link],
    }))
    before = mapper.build_desired(config)
    mapper.apply(mapper.create_plan('generated'))
    with custom_map_engine.begin() as connection:
        connection.exec_driver_sql("UPDATE custom_map_nodes SET style='ellipse' WHERE device_id=1")
    link['a']['device'] = 'librenms:1'
    interface['device_source_id'] = 'librenms:1'
    manual_path.write_text(yaml.safe_dump({
        'devices': [{'source_id': 'outside', 'hostname': 'external'}],
        'inventory_overrides': {'librenms:1': {'layer': -4, 'interfaces': [interface]}},
        'links': [link],
    }))
    after = mapper.build_desired(config)
    assert before.nodes == after.nodes
    assert before.edges == after.edges
    assert before.node_layers == after.node_layers
    assert before.edge_display_names == after.edge_display_names
    plan = mapper.create_plan('generated')
    assert not [op for op in plan.operations if op.action in {'create', 'delete'}]
    mapper.apply(plan)
    with custom_map_engine.connect() as connection:
        assert connection.exec_driver_sql(
            'SELECT style FROM custom_map_nodes WHERE device_id=1'
        ).scalar_one() == 'ellipse'


def test_inventory_migration_preserves_manual_only_node_and_edge_identity(
    tmp_path: Path, custom_map_engine: Engine,
) -> None:
    mapper, _, _ = manual_service(tmp_path, custom_map_engine)
    config = mapper.config.maps[0]
    config.sources = []
    config.manual_device_inclusions = ['a', 'b']
    with custom_map_engine.begin() as connection:
        connection.exec_driver_sql(
            'CREATE TABLE devices (device_id INTEGER PRIMARY KEY, hostname TEXT)'
        )
        connection.exec_driver_sql("INSERT INTO devices VALUES (1, 'a'), (2, 'b')")
    mapper.apply(mapper.create_plan('generated'))
    with custom_map_engine.begin() as connection:
        connection.exec_driver_sql("UPDATE custom_map_nodes SET style='ellipse' WHERE device_id=1")
        connection.exec_driver_sql("UPDATE custom_map_edges SET style='straight'")
    before = mapper.state.objects('generated')
    (tmp_path / 'manual.yaml').write_text(yaml.safe_dump({
        'devices': [], 'links': [{
            'source_id': 'guided:old:ab', 'protocol': 'manual', 'relationship_kind': 'logical',
            'a': {'device': 'librenms:1'}, 'b': {'device': 'librenms:2'},
        }],
    }))
    config.manual_device_inclusions = ['librenms:1', 'librenms:2']
    plan = mapper.create_plan('generated')
    assert not [op for op in plan.operations if op.action in {'create', 'delete'}]
    mapper.apply(plan)
    after = mapper.state.objects('generated')
    assert {item['librenms_id'] for item in before.values()} == {
        item['librenms_id'] for item in after.values()
    }
    with custom_map_engine.connect() as connection:
        assert connection.exec_driver_sql(
            'SELECT style FROM custom_map_nodes WHERE device_id=1'
        ).scalar_one() == 'ellipse'
        assert connection.exec_driver_sql(
            'SELECT style FROM custom_map_edges'
        ).scalar_one() == 'straight'
    assert mapper.create_plan('generated').changes == 0


def seed_ports(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql('ALTER TABLE ports ADD COLUMN ifSpeed INTEGER DEFAULT 0')
        connection.exec_driver_sql('ALTER TABLE ports ADD COLUMN disabled INTEGER DEFAULT 1')
        connection.exec_driver_sql(
            "ALTER TABLE ports ADD COLUMN ifType TEXT DEFAULT 'softwareLoopback'"
        )
        connection.exec_driver_sql('''INSERT OR REPLACE INTO ports
            (port_id, device_id, deleted, ifName) VALUES
            (101, 1, 0, 'Ethernet1'), (102, 1, 0, 'Loopback0'),
            (201, 2, 0, 'Ethernet2'), (301, 3, 0, 'Wrong owner'),
            (103, 1, 1, 'Deleted')''')


def test_manual_only_membership_stable_identity_and_external_connections(
    tmp_path: Path, custom_map_engine: Engine,
) -> None:
    mapper, _, _ = manual_service(tmp_path, custom_map_engine)
    manual_path = tmp_path / 'manual.yaml'
    manual = {'devices': [
        {'source_id': 'outside', 'hostname': 'outside.example.net'},
        {'source_id': 'peer', 'hostname': 'peer.example.net'},
        {'source_id': 'standalone', 'hostname': 'standalone.example.net'},
    ], 'links': [{'source_id': 'external-pair', 'a': {'device': 'outside'},
                 'b': {'device': 'peer'}, 'protocol': 'manual'}]}
    manual_path.write_text(yaml.safe_dump(manual))
    config = mapper.config.maps[0]
    config.sources = []
    assert not mapper.build_desired(config).nodes
    config.manual_device_inclusions = ['outside', 'standalone']
    first = mapper.create_plan('generated')
    assert len([op for op in first.operations if op.kind == 'node']) == 3
    assert len([op for op in first.operations if op.kind == 'edge']) == 1
    mapper.apply(first)
    manual['devices'][0]['hostname'] = 'renamed.example.net'
    manual_path.write_text(yaml.safe_dump(manual))
    edited = mapper.create_plan('generated')
    assert not [op for op in edited.operations if op.action in {'create', 'delete'}]
    config.manual_device_exclusions = ['peer']
    desired = mapper.build_desired(config)
    assert len(desired.nodes) == 2 and not desired.edges


def test_manual_exclusion_removes_federated_inventory_alias(
    tmp_path: Path, custom_map_engine: Engine,
) -> None:
    mapper, _, _ = manual_service(tmp_path, custom_map_engine)
    config = mapper.config.maps[0]
    config.manual_device_exclusions = ['a']
    desired = mapper.build_desired(config)
    assert {node.device_id for node in desired.nodes.values()} == {2}
    assert not desired.edges
    config.manual_device_exclusions = []
    config.sources = [SourceConfig(provider='external', selectors=[])]
    config.manual_device_inclusions = ['a']
    assert len(mapper.build_desired(config).nodes) == 2


@pytest.mark.parametrize(('a_port', 'b_port', 'expected'), [
    (101, None, 101), (None, 201, 201), (101, 201, 101), (103, 201, 201),
])
def test_automatic_uses_valid_interface_on_either_endpoint(
    tmp_path: Path, custom_map_engine: Engine,
    a_port: int | None, b_port: int | None, expected: int,
) -> None:
    mapper, path, discovered = manual_service(tmp_path, custom_map_engine)
    seed_ports(custom_map_engine)
    for device, port in zip(discovered['devices'], (a_port, b_port), strict=True):
        device['interfaces'][0]['librenms_port_id'] = port
    path.write_text(yaml.safe_dump(discovered))
    desired = mapper.build_desired(mapper.config.maps[0])
    assert next(iter(desired.edges.values())).port_id == expected


def test_graph_swap_keeps_identity_native_style_positions_and_direction(
    tmp_path: Path, custom_map_engine: Engine,
) -> None:
    mapper, _, _ = manual_service(tmp_path, custom_map_engine)
    seed_ports(custom_map_engine)
    mapper.apply(mapper.create_plan('generated'))
    first = next(iter(mapper.state.objects('generated', 'edge').values()))
    with custom_map_engine.begin() as connection:
        connection.execute(text("UPDATE custom_map_edges SET style='straight', reverse=1, "
                                "mid_x=456, label='native label'"))
    config = mapper.config.maps[0]
    config.link_graph_overrides = [
        LinkGraphOverride(canonical_id=first['canonical_id'], port_id=102)
    ]
    plan = mapper.create_plan('generated')
    edge = next(op for op in plan.operations if op.kind == 'edge')
    assert edge.librenms_id == first['librenms_id']
    assert edge.canonical_id == first['canonical_id']
    assert edge.values == {'port_id': 102}
    assert edge.graph['mode'] == 'overridden'
    mapper.apply(plan)
    assert not [op for op in mapper.create_plan('generated').operations if op.action != 'noop']
    config.link_graph_overrides[0].port_id = None
    automatic = mapper.create_plan('generated')
    assert next(op for op in automatic.operations if op.kind == 'edge').values == {'port_id': 101}
    mapper.apply(automatic)
    config.link_graph_overrides[0].port_id = 102
    stale = mapper.create_plan('generated')
    with custom_map_engine.begin() as connection:
        connection.execute(text('UPDATE ports SET deleted=1 WHERE port_id=102'))
    with pytest.raises(RuntimeError):
        mapper.apply(stale)
    assert mapper.create_plan('generated').conflicts


@pytest.mark.parametrize('port_id', [103, 301, 999])
def test_invalid_override_blocks_publish(
    tmp_path: Path, custom_map_engine: Engine, port_id: int,
) -> None:
    mapper, _, _ = manual_service(tmp_path, custom_map_engine)
    seed_ports(custom_map_engine)
    config = mapper.config.maps[0]
    canonical_id = next(iter(mapper.build_desired(config).edges))
    config.link_graph_overrides = [LinkGraphOverride(canonical_id=canonical_id, port_id=port_id)]
    plan = mapper.create_plan('generated')
    assert plan.conflicts
    with pytest.raises(RuntimeError):
        mapper.apply(plan)
    config.link_graph_overrides = [LinkGraphOverride(canonical_id='missing', port_id=None)]
    assert 'missing connection' in mapper.create_plan('generated').conflicts[0]


def test_external_logical_link_has_counters_but_custom_label_does_not(
    tmp_path: Path, custom_map_engine: Engine,
) -> None:
    mapper, path, discovered = manual_service(tmp_path, custom_map_engine)
    seed_ports(custom_map_engine)
    discovered['devices'][1].pop('librenms_device_id')
    path.write_text(yaml.safe_dump(discovered))
    mapper.config.manual_provider = None
    desired = mapper.build_desired(mapper.config.maps[0])
    assert next(iter(desired.edges.values())).port_id == 101
    discovered['devices'][0]['interfaces'][0]['attributes'] = {'manual_label_only': True}
    path.write_text(yaml.safe_dump(discovered))
    assert next(iter(mapper.build_desired(mapper.config.maps[0]).edges.values())).port_id is None


def test_explicit_graph_on_logical_fallback_changes_only_binding(
    tmp_path: Path, custom_map_engine: Engine,
) -> None:
    mapper, path, discovered = manual_service(tmp_path, custom_map_engine)
    seed_ports(custom_map_engine)
    path.write_text(yaml.safe_dump({**discovered, 'links': []}))
    mapper.apply(mapper.create_plan('generated'))
    edge_record = next(iter(mapper.state.objects('generated', 'edge').values()))
    mapper.config.maps[0].link_graph_overrides = [
        LinkGraphOverride(canonical_id=edge_record['canonical_id'], port_id=201)
    ]
    plan = mapper.create_plan('generated')
    edge = next(op for op in plan.operations if op.kind == 'edge')
    assert edge.values == {'port_id': 201}
    assert edge.librenms_id == edge_record['librenms_id']
    assert edge.graph['mode'] == 'overridden'


@pytest.mark.parametrize('changes', [
    {'manual_device_inclusions': ['same'], 'manual_device_exclusions': ['same']},
    {'link_graph_overrides': [{'canonical_id': 'edge', 'port_id': 0}]},
    {'link_graph_overrides': [{'canonical_id': 'edge'}, {'canonical_id': 'edge'}]},
])
def test_invalid_membership_and_graph_configuration(changes: dict) -> None:
    from pydantic import ValidationError

    from libremapper.config import MapConfig

    with pytest.raises(ValidationError):
        MapConfig.model_validate({'name': 'Example', 'sources': [], **changes})


@pytest.mark.parametrize('topology', [
    {'devices': [{'source_id': 'duplicate'}, {'source_id': 'duplicate'}]},
    {'devices': [{'source_id': 'a'}],
     'links': [{'source_id': 'broken', 'a': {'device': 'a'}, 'b': {'device': 'missing'}}]},
    {'devices': [{'source_id': 'a'}, {'source_id': 'b'}],
     'suppress_links': [{'a': {'device': 'a', 'interface': 'missing'}, 'b': {'device': 'b'}}]},
])
def test_ambiguous_or_dangling_manual_records_fail_validation(topology: dict) -> None:
    from pydantic import ValidationError

    from libremapper.providers.file import FileTopology

    with pytest.raises(ValidationError):
        FileTopology.model_validate(topology)


def test_manual_default_layers_are_ignored_automatically_and_restored_in_manual_layouts(
    tmp_path: Path, custom_map_engine: Engine,
) -> None:
    from libremapper.config import SelectorConfig

    mapper, _, _ = manual_service(tmp_path, custom_map_engine)
    manual_path = tmp_path / 'manual.yaml'
    topology = yaml.safe_load(manual_path.read_text())
    topology['devices'].append({'source_id': 'placed', 'hostname': 'Placed', 'layer': 7})
    manual_path.write_text(yaml.safe_dump(topology))
    config = mapper.config.maps[0]
    config.sources = []
    config.manual_device_inclusions = ['placed']
    config.layout.infer_layers = True
    config.layout.type = 'auto'
    desired = mapper.build_desired(config)
    assert list(desired.node_layers.values()) == [0]
    assert desired.layout_summary['resolved'] == 'force'
    config.layer_overrides = [SelectorConfig(type='canonical', value='manual:placed', layer=2)]
    assert mapper.build_desired(config).nodes == desired.nodes
    config.layout.type = 'top'
    config.layout.infer_layers = False
    assert list(mapper.build_desired(config).node_layers.values()) == [2]
    config.layer_overrides = []
    assert list(mapper.build_desired(config).node_layers.values()) == [7]
    assert yaml.safe_load(manual_path.read_text()) == topology


def test_manual_source_layer_override_survives_inventory_merging_and_renaming(
    tmp_path: Path, custom_map_engine: Engine,
) -> None:
    from libremapper.config import SelectorConfig

    mapper, _, _ = manual_service(tmp_path, custom_map_engine)
    config = mapper.config.maps[0]
    config.layout.type = 'top'
    config.layout.infer_layers = False
    original = mapper.build_desired(config)
    config.layer_overrides = [SelectorConfig(type='canonical', value='manual:a', layer=-6)]
    desired = mapper.build_desired(config)
    canonical = next(key for key, node in desired.nodes.items() if node.device_id == 1)
    assert desired.node_layers[canonical] == -6
    assert desired.nodes.keys() == original.nodes.keys()
    other_map = config.model_copy(update={'layer_overrides': []})
    assert mapper.build_desired(other_map).node_layers == original.node_layers
    manual_path = tmp_path / 'manual.yaml'
    topology = yaml.safe_load(manual_path.read_text())
    topology['devices'][0]['hostname'] = 'Renamed alias'
    manual_path.write_text(yaml.safe_dump(topology))
    assert mapper.build_desired(config).node_layers[canonical] == -6
    config.layout.infer_layers = True
    assert mapper.build_desired(config).node_layers[canonical] != -6
