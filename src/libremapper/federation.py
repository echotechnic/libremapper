from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from libremapper.config import IdentityConfig
from libremapper.models import (
    Device,
    FederatedDevice,
    FederatedInterface,
    FederatedLink,
    FederatedTopology,
    Interface,
    LinkFallback,
    SourceRef,
    SuppressLink,
    Topology,
    stable_hash,
)


def normalize_identity(value: Any) -> str:
    return str(value).strip().lower().rstrip(".")


def normalize_mac(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"[^0-9a-f]", "", value.lower())
    return normalized or None


def _relationship_kind(link: Any) -> str | None:
    value = link.relationship_kind or link.attributes.get("relationship_kind")
    return str(value) if value else None


def _suppression_matches(
    link: FederatedLink,
    suppression: SuppressLink,
    a_device: str | None,
    b_device: str | None,
    a_interface: str | None,
    b_interface: str | None,
) -> bool:
    if not a_device or not b_device or {link.a_device, link.b_device} != {
        a_device,
        b_device,
    }:
        return False
    endpoints = {
        link.a_device: link.a_interface,
        link.b_device: link.b_interface,
    }
    if suppression.a.interface and (
        a_interface is None or endpoints.get(a_device) != a_interface
    ):
        return False
    if suppression.b.interface and (
        b_interface is None or endpoints.get(b_device) != b_interface
    ):
        return False
    return not suppression.protocol or (link.protocol or "").lower() == (
        suppression.protocol.lower()
    )


class FederationEngine:
    def __init__(self, identity: IdentityConfig) -> None:
        self.identity = identity

    def merge(
        self, topologies: list[Topology], *, fallback_providers: set[str] | None = None
    ) -> FederatedTopology:
        result = FederatedTopology()
        source_device_index: dict[tuple[str, str], str] = {}
        source_interface_index: dict[tuple[str, str, str], str] = {}

        device_observations = [
            (topology.provider, device) for topology in topologies for device in topology.devices
        ]
        observation_indexes = {
            (provider, id(device)): index
            for index, (provider, device) in enumerate(device_observations)
        }
        parents = list(range(len(device_observations)))

        def find(item: int) -> int:
            while parents[item] != item:
                parents[item] = parents[parents[item]]
                item = parents[item]
            return item

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        token_owner: dict[str, int] = {}
        observation_tokens: list[list[str]] = []
        aliases = {
            normalize_identity(key): normalize_identity(value)
            for key, value in self.identity.aliases.items()
        }
        for index, (provider, device) in enumerate(device_observations):
            candidates = self._identity_candidates(provider, device)
            tokens = list(candidates)
            tokens.extend(aliases[candidate] for candidate in candidates if candidate in aliases)
            observation_tokens.append(tokens)
            for token in tokens:
                owner = token_owner.setdefault(token, index)
                union(index, owner)

        group_canonical: dict[int, str] = {}
        for index, tokens in enumerate(observation_tokens):
            root = find(index)
            if root not in group_canonical:
                alias = next((aliases[token] for token in tokens if token in aliases), None)
                group_canonical[root] = alias or tokens[0]

        for topology in topologies:
            result.warnings.extend(topology.warnings)
            for device in topology.devices:
                observation_index = observation_indexes[(topology.provider, id(device))]
                canonical_id = group_canonical[find(observation_index)]
                federated = result.devices.setdefault(
                    canonical_id, FederatedDevice(canonical_id=canonical_id)
                )
                source_device_index[(topology.provider, device.source_id)] = canonical_id
                self._merge_device(federated, topology.provider, device, result.conflicts)
                for interface in device.interfaces:
                    interface_id = self._merge_interface(
                        federated, topology.provider, interface, result.conflicts
                    )
                    source_interface_index[
                        (topology.provider, device.source_id, interface.source_id)
                    ] = interface_id

        link_observations: dict[
            tuple[str, str], list[tuple[str, Any, str, str, str | None, str | None]]
        ] = defaultdict(list)
        for topology in topologies:
            for link in topology.links:
                a_device = source_device_index.get((topology.provider, link.a.device))
                b_device = source_device_index.get((topology.provider, link.b.device))
                if not a_device or not b_device or a_device == b_device:
                    continue
                a_interface = (
                    source_interface_index.get((topology.provider, link.a.device, link.a.interface))
                    if link.a.interface
                    else None
                )
                b_interface = (
                    source_interface_index.get((topology.provider, link.b.device, link.b.interface))
                    if link.b.interface
                    else None
                )
                if (a_device, a_interface or "") > (b_device, b_interface or ""):
                    a_device, b_device = b_device, a_device
                    a_interface, b_interface = b_interface, a_interface
                pair = (a_device, b_device)
                link_observations[pair].append(
                    (
                        topology.provider,
                        link,
                        a_device,
                        b_device,
                        a_interface,
                        b_interface,
                    )
                )

        def resolved(provider: str, device: str, interface: Any) -> str | None:
            return source_interface_index.get((provider, device, interface)) if interface else None

        for pair, observations in link_observations.items():
            lldp_keys = {
                (a_interface, b_interface)
                for _, link, _, _, a_interface, b_interface in observations
                if (link.protocol or "").lower() == "lldp" and a_interface and b_interface
            }
            link_aliases: dict[
                tuple[str, str | None, str | None], set[tuple[str, str]]
            ] = defaultdict(set)
            for provider, link, a_device, _, a_interface, b_interface in observations:
                protocol = (link.protocol or "").lower()
                original_a = source_device_index[(provider, link.a.device)]
                if protocol == "lldp" and link.attributes.get("remote_member_interface"):
                    member = resolved(
                        provider, link.b.device, link.attributes["remote_member_interface"]
                    )
                    target = (a_interface, b_interface)
                    alternate = (
                        (a_interface, member) if original_a == a_device else (member, b_interface)
                    )
                elif (
                    protocol == "ip-p2p"
                    and link.attributes.get("local_underlay_interface")
                    and link.attributes.get("remote_aggregate_interface")
                ):
                    local = resolved(
                        provider, link.a.device, link.attributes["local_underlay_interface"]
                    )
                    remote = resolved(
                        provider, link.b.device, link.attributes["remote_aggregate_interface"]
                    )
                    target = (local, remote) if original_a == a_device else (remote, local)
                    alternate = (a_interface, b_interface)
                else:
                    continue
                if target[0] and target[1] and all(alternate) and target in lldp_keys:
                    link_aliases[(protocol, *alternate)].add((target[0], target[1]))

            normalized = []
            for provider, link, a_device, b_device, a_interface, b_interface in observations:
                choices = link_aliases.get(
                    ((link.protocol or "").lower(), a_interface, b_interface), set()
                )
                if len(choices) == 1:
                    a_interface, b_interface = next(iter(choices))
                normalized.append((provider, link, a_device, b_device, a_interface, b_interface))
            observations = normalized
            precise: dict[
                tuple[str, str, str, str],
                list[tuple[str, Any, str, str, str | None, str | None]],
            ] = defaultdict(list)
            vague: list[tuple[str, Any, str, str, str | None, str | None]] = []
            for observation in observations:
                _, _, a_device, b_device, a_interface, b_interface = observation
                if a_interface or b_interface:
                    precise[
                        (
                            a_device,
                            a_interface or "",
                            b_device,
                            b_interface or "",
                        )
                    ].append(observation)
                else:
                    vague.append(observation)

            logical_vague = [
                observation
                for observation in vague
                if _relationship_kind(observation[1]) == "logical"
            ]
            direct_vague = [
                observation for observation in vague if observation not in logical_vague
            ]
            manual_vague = [
                observation
                for observation in logical_vague
                if observation[0] in (fallback_providers or set())
                or (observation[1].protocol or "").lower() == "manual"
            ]
            clusters = list(precise.items())
            discovered_keys = [key for key, _ in clusters]
            if clusters:
                for _, cluster in clusters:
                    cluster.extend(
                        observation
                        for observation in logical_vague
                        if observation not in manual_vague
                    )
                if len(clusters) == 1:
                    clusters[0][1].extend(manual_vague)
                    clusters[0][1].extend(direct_vague)
                elif direct_vague:
                    result.warnings.append(
                        f"ambiguous link observations between {pair[0]} and {pair[1]} "
                        "could not be merged with parallel interface-level links"
                    )
                    clusters.append(((pair[0], "", pair[1], ""), direct_vague))
            elif vague:
                clusters = [((pair[0], "", pair[1], ""), vague)]
                if any(observation not in manual_vague for observation in vague):
                    discovered_keys = [clusters[0][0]]

            for provider, observation, *_ in manual_vague:
                result.link_fallbacks.append(
                    LinkFallback(
                        source=SourceRef(provider=provider, source_id=observation.source_id),
                        a_device=pair[0],
                        b_device=pair[1],
                        status=(
                            "parallel-covered"
                            if len(discovered_keys) > 1
                            else "corroborated"
                            if discovered_keys
                            else "active"
                        ),
                        canonical_ids=[
                            stable_hash(key)[:24]
                            for key in (discovered_keys or [(pair[0], "", pair[1], "")])
                        ],
                    )
                )

            for cluster_key, cluster in clusters:
                ordered = sorted(
                    cluster,
                    key=lambda observation: (
                        observation in manual_vague,
                        _relationship_kind(observation[1]) == "logical",
                        {"lldp": 0, "cdp": 1, "ip-p2p": 2}.get(
                            (observation[1].protocol or "").lower(), 3
                        ),
                        observation[0],
                        observation[1].source_id,
                    ),
                )
                provider, link, a_device, b_device, a_interface, b_interface = ordered[0]
                canonical_id = stable_hash(cluster_key)[:24]
                port_id = self._preferred_port(result, a_device, a_interface, b_device, b_interface)
                attributes: dict[str, Any] = {}
                evidence: set[str] = set()
                for _, observation, *_ in ordered:
                    for key, value in observation.attributes.items():
                        if key not in {"evidence", "relationship_kind"}:
                            attributes.setdefault(key, value)
                    observed_evidence = observation.attributes.get("evidence", [])
                    if isinstance(observed_evidence, str):
                        evidence.add(observed_evidence)
                    else:
                        evidence.update(str(item) for item in observed_evidence)
                relationship_kinds = [
                    _relationship_kind(observation) for _, observation, *_ in ordered
                ]
                default_logical = (
                    result.devices[a_device].librenms_device_id is None
                    or result.devices[b_device].librenms_device_id is None
                ) and "direct" not in relationship_kinds
                attributes["relationship_kind"] = (
                    "logical"
                    if default_logical or all(kind == "logical" for kind in relationship_kinds)
                    else "direct"
                )
                attributes["evidence"] = sorted(evidence)
                federated_link = FederatedLink(
                    canonical_id=canonical_id,
                    a_device=a_device,
                    b_device=b_device,
                    a_interface=a_interface,
                    b_interface=b_interface,
                    librenms_port_id=port_id,
                    protocol=link.protocol,
                    confidence=max(observation.confidence for _, observation, *_ in ordered),
                    attributes=attributes,
                    sources=[
                        SourceRef(provider=item_provider, source_id=observation.source_id)
                        for item_provider, observation, *_ in ordered
                    ],
                )
                result.links[canonical_id] = federated_link

        return result

    def merge_with_manual(
        self,
        topologies: list[Topology],
        manual: Topology,
        suppressions: list[SuppressLink],
        suppressible_providers: set[str],
        *,
        fallback_providers: set[str] | None = None,
        manual_device_inclusions: set[str] | None = None,
    ) -> FederatedTopology:
        selected = self.merge(topologies, fallback_providers=fallback_providers)
        if not selected.devices and not manual_device_inclusions:
            return selected

        outer_layer = max((device.layer for device in selected.devices.values()), default=-1)
        manual = manual.model_copy(deep=True)
        for device in manual.devices:
            if not device.attributes.pop("_libremapper_explicit_layer", False):
                device.layer = outer_layer + 1
            else:
                device.attributes["_libremapper_manual_layer"] = device.layer

        combined = self.merge(
            [*topologies, manual],
            fallback_providers=(fallback_providers or set()) | {manual.provider},
        )
        selected_ids = {
            canonical_id for canonical_id, device in combined.devices.items()
            if canonical_id in selected.devices or any(
                source.provider == manual.provider
                and source.source_id in (manual_device_inclusions or set())
                for source in device.sources
            )
        }
        manual_links = {link.source_id: link for link in manual.links}
        relevant_manual = {
            canonical_id
            for canonical_id, link in combined.links.items()
            if any(
                source.provider == manual.provider and source.source_id in manual_links
                for source in link.sources
            )
            and (link.a_device in selected_ids or link.b_device in selected_ids)
        }
        relevant_manual.update(
            canonical_id
            for fallback in combined.link_fallbacks
            if fallback.source.provider == manual.provider
            and (fallback.a_device in selected_ids or fallback.b_device in selected_ids)
            for canonical_id in fallback.canonical_ids
        )
        included_ids = set(selected_ids)
        for canonical_id in relevant_manual:
            link = combined.links[canonical_id]
            included_ids.update((link.a_device, link.b_device))

        combined.devices = {
            canonical_id: device
            for canonical_id, device in combined.devices.items()
            if canonical_id in included_ids
        }
        combined.links = {
            canonical_id: link
            for canonical_id, link in combined.links.items()
            if canonical_id in relevant_manual
            or (
                link.a_device in selected_ids
                and link.b_device in selected_ids
                and any(source.provider != manual.provider for source in link.sources)
            )
        }
        combined.link_fallbacks = [
            fallback
            for fallback in combined.link_fallbacks
            if all(canonical_id in combined.links for canonical_id in fallback.canonical_ids)
        ]

        source_devices = {
            (source.provider, source.source_id): canonical_id
            for canonical_id, device in combined.devices.items()
            for source in device.sources
        }
        source_interfaces = {
            (source.provider, device_id, source.source_id): interface_id
            for device_id, device in combined.devices.items()
            for interface_id, interface in device.interfaces.items()
            for source in interface.sources
        }

        for suppression in suppressions:
            a_device = source_devices.get((manual.provider, suppression.a.device))
            b_device = source_devices.get((manual.provider, suppression.b.device))
            relevant = a_device in selected_ids or b_device in selected_ids
            if not relevant:
                continue
            a_interface = (
                source_interfaces.get((manual.provider, a_device, suppression.a.interface))
                if a_device and suppression.a.interface
                else None
            )
            b_interface = (
                source_interfaces.get((manual.provider, b_device, suppression.b.interface))
                if b_device and suppression.b.interface
                else None
            )

            removed = [
                canonical_id
                for canonical_id, link in combined.links.items()
                if link.sources
                and not any(
                    canonical_id in fallback.canonical_ids for fallback in combined.link_fallbacks
                )
                and all(source.provider in suppressible_providers for source in link.sources)
                and _suppression_matches(
                    link,
                    suppression,
                    a_device,
                    b_device,
                    a_interface,
                    b_interface,
                )
            ]
            for canonical_id in removed:
                del combined.links[canonical_id]
            if not removed:
                combined.warnings.append(
                    "manual suppression matched no automatic relationship: "
                    f"{suppression.a.device} -- {suppression.b.device}"
                )

        return combined

    def _identity_candidates(self, provider: str, device: Device) -> list[str]:
        candidates = [f"{provider}:{normalize_identity(device.source_id)}"]
        if device.librenms_device_id is not None:
            candidates.append(f"librenms:{device.librenms_device_id}")
        for key in self.identity.keys:
            value = getattr(device, key)
            if value:
                candidates.append(f"{key}:{normalize_identity(value)}")
                candidates.append(normalize_identity(value))
        return candidates

    def _merge_device(
        self,
        target: FederatedDevice,
        provider: str,
        source: Device,
        conflicts: list[str],
    ) -> None:
        first_source = not target.sources
        target.sources.append(SourceRef(provider=provider, source_id=source.source_id))
        for field in (
            "hostname", "fqdn", "management_ip", "librenms_device_id", "short_hostname_parts"
        ):
            incoming = getattr(source, field)
            current = getattr(target, field)
            if current is None and incoming is not None:
                setattr(target, field, incoming)
            elif incoming is not None and current != incoming:
                conflicts.append(
                    f"device {target.canonical_id}: {field} kept {current!r}, "
                    f"ignored {incoming!r} from {provider}"
                )
        if first_source:
            target.layer = source.layer
        else:
            target.layer = min(target.layer, source.layer)
        for key, value in source.attributes.items():
            target.attributes.setdefault(key, value)

    def _merge_interface(
        self,
        device: FederatedDevice,
        provider: str,
        source: Interface,
        conflicts: list[str],
    ) -> str:
        source_name = normalize_identity(source.name) if source.name else None
        source_mac = normalize_mac(source.mac)

        def matches(interface: FederatedInterface) -> bool:
            if any(
                ref.provider == provider and ref.source_id == source.source_id
                for ref in interface.sources
            ):
                return True
            if source.attributes.get("manual_label_only") or interface.attributes.get(
                "manual_label_only"
            ):
                return False
            if source.librenms_port_id is not None and interface.librenms_port_id is not None:
                return source.librenms_port_id == interface.librenms_port_id
            if any(ref.provider == provider for ref in interface.sources):
                return False
            if source.if_index is not None and interface.if_index == source.if_index:
                return True
            interface_name = normalize_identity(interface.name) if interface.name else None
            if source_name and interface_name == source_name:
                return True
            interface_mac = normalize_mac(interface.mac)
            return bool(
                source_mac
                and interface_mac == source_mac
                and not (
                    source.if_index is not None
                    and interface.if_index is not None
                    and source.if_index != interface.if_index
                )
                and not (source_name and interface_name and source_name != interface_name)
            )

        existing_id = next(
            (
                interface_id
                for interface_id, interface in device.interfaces.items()
                if matches(interface)
            ),
            None,
        )
        canonical_id = existing_id or (
            f"{provider}:{source.source_id}" if source.attributes.get("manual_label_only") else None
        ) or next(
            (
                token
                for token in (
                    f"ifindex:{source.if_index}" if source.if_index is not None else None,
                    f"name:{source_name}" if source_name else None,
                    f"mac:{source_mac}" if source_mac else None,
                )
                if token
            ),
            f"{provider}:{source.source_id}",
        )
        if existing_id is None and canonical_id in device.interfaces:
            canonical_id = f"{provider}:{source.source_id}"
        target = device.interfaces.setdefault(
            canonical_id, FederatedInterface(canonical_id=canonical_id)
        )
        target.sources.append(SourceRef(provider=provider, source_id=source.source_id))
        for field in ("name", "if_index", "mac", "librenms_port_id"):
            incoming = getattr(source, field)
            current = getattr(target, field)
            if current is None and incoming is not None:
                setattr(target, field, incoming)
            elif incoming is not None and current != incoming:
                conflicts.append(
                    f"interface {device.canonical_id}/{canonical_id}: {field} "
                    f"kept {current!r}, ignored {incoming!r} from {provider}"
                )
        for key, value in source.attributes.items():
            target.attributes.setdefault(key, value)
        return canonical_id

    @staticmethod
    def _preferred_port(
        topology: FederatedTopology,
        a_device: str,
        a_interface: str | None,
        b_device: str,
        b_interface: str | None,
    ) -> int | None:
        for device_id, interface_id in (
            (a_device, a_interface),
            (b_device, b_interface),
        ):
            if interface_id:
                interface = topology.devices[device_id].interfaces.get(interface_id)
                if (
                    interface and interface.librenms_port_id
                    and not interface.attributes.get("manual_label_only")
                ):
                    return interface.librenms_port_id
        return None
