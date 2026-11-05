#!/usr/bin/env python3
"""OCI cross-region DRG peering console.

Local web UI that uses the OCI Python SDK (default ~/.oci/config) to:

- pick DRGs in different regions
- create / connect / tear down Remote Peering Connections
- attach a VCN from any compartment, with VCN or SUBNET route type
- create a dedicated DRG route table (never the DRG default table)
- edit the subnet route table and security lists that actually carry traffic

Nothing is mutated unless the UI explicitly calls a write endpoint.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from datetime import datetime
from functools import wraps
from typing import Any

import oci
from flask import Flask, jsonify, request, send_from_directory
from oci.core.models import (
    CreateDrgDetails,
    AddDrgRouteDistributionStatementDetails,
    AddDrgRouteDistributionStatementsDetails,
    AddDrgRouteRuleDetails,
    AddDrgRouteRulesDetails,
    ConnectRemotePeeringConnectionsDetails,
    CreateDrgAttachmentDetails,
    CreateDrgRouteDistributionDetails,
    CreateDrgRouteTableDetails,
    CreateRemotePeeringConnectionDetails,
    DrgAttachmentIdDrgRouteDistributionMatchCriteria,
    DrgAttachmentMatchAllDrgRouteDistributionMatchCriteria,
    EgressSecurityRule,
    IngressSecurityRule,
    PortRange,
    RemoveDrgRouteRulesDetails,
    RouteRule,
    TcpOptions,
    UdpOptions,
    UpdateDrgAttachmentDetails,
    UpdateRouteTableDetails,
    UpdateSecurityListDetails,
    VcnDrgAttachmentNetworkCreateDetails,
    VcnDrgAttachmentNetworkUpdateDetails,
)
from oci.exceptions import ServiceError
from oci.pagination import list_call_get_all_results


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Commercial regions commonly used for cross-region RPC. The UI also accepts
# any region identifier the local SDK / tenancy subscription knows about.
FALLBACK_REGIONS = [
    "af-johannesburg-1",
    "ap-chuncheon-1",
    "ap-hyderabad-1",
    "ap-melbourne-1",
    "ap-mumbai-1",
    "ap-osaka-1",
    "ap-seoul-1",
    "ap-singapore-1",
    "ap-sydney-1",
    "ap-tokyo-1",
    "ca-montreal-1",
    "ca-toronto-1",
    "eu-amsterdam-1",
    "eu-frankfurt-1",
    "eu-madrid-1",
    "eu-marseille-1",
    "eu-milan-1",
    "eu-paris-1",
    "eu-stockholm-1",
    "eu-zurich-1",
    "il-jerusalem-1",
    "me-abudhabi-1",
    "me-dubai-1",
    "me-jeddah-1",
    "mx-monterrey-1",
    "mx-queretaro-1",
    "sa-bogota-1",
    "sa-santiago-1",
    "sa-saopaulo-1",
    "sa-vinhedo-1",
    "uk-cardiff-1",
    "uk-london-1",
    "us-ashburn-1",
    "us-chicago-1",
    "us-phoenix-1",
    "us-sanjose-1",
]

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
_client_lock = threading.Lock()
_clients: dict[tuple[str, str, str], Any] = {}


def configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        return oci.util.to_dict(value)
    except Exception:
        if isinstance(value, (list, tuple)):
            return [to_plain(item) for item in value]
        if isinstance(value, dict):
            return {str(k): to_plain(v) for k, v in value.items()}
        return str(value)


def api_error(status: int, message: str, **extra: Any):
    body = {"ok": False, "error": message}
    body.update(extra)
    return jsonify(body), status


def handle_errors(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ServiceError as exc:
            return api_error(
                exc.status or 502,
                f"OCI {exc.status} {exc.code}: {exc.message}",
                opc_request_id=getattr(exc, "request_id", None),
                target_service=getattr(exc, "target_service", None),
            )
        except oci.exceptions.ConfigFileNotFound as exc:
            return api_error(400, f"OCI config not found: {exc}")
        except oci.exceptions.InvalidConfig as exc:
            return api_error(400, f"OCI config is invalid: {exc}")
        except oci.exceptions.ProfileNotFound as exc:
            return api_error(400, f"OCI profile not found: {exc}")
        except ValueError as exc:
            return api_error(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - surface unexpected SDK failures to the UI
            traceback.print_exc()
            return api_error(500, f"{type(exc).__name__}: {exc}")

    return wrapper


def request_json() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")
    return data


def require(data: dict[str, Any], *keys: str) -> None:
    missing = [key for key in keys if not data.get(key)]
    if missing:
        raise ValueError("Missing parameter: " + ", ".join(missing))


def config_path() -> str:
    return os.path.expanduser(request.args.get("config") or request_json().get("config") or "~/.oci/config")


def profile_name(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    return request.args.get("profile") or request_json().get("profile") or "DEFAULT"


def load_config(profile: str, region: str | None = None) -> dict[str, Any]:
    path = config_path()
    if not os.path.exists(path):
        raise oci.exceptions.ConfigFileNotFound(f"{path} does not exist")
    config = oci.config.from_file(file_location=path, profile_name=profile)
    if region:
        config["region"] = region
    if not config.get("tenancy"):
        raise ValueError(f"profile {profile} has no tenancy OCID")
    return config


def signer_for(config: dict[str, Any]):
    if config.get("security_token_file"):
        with open(config["security_token_file"], encoding="utf-8") as handle:
            token = handle.read().strip()
        private_key = oci.signer.load_private_key_from_file(config["key_file"], config.get("pass_phrase"))
        return oci.auth.signers.SecurityTokenSigner(token, private_key)
    return None


def client(kind: str, profile: str, region: str):
    key = (kind, profile, region, config_path())
    with _client_lock:
        cached = _clients.get(key)
        if cached is not None:
            return cached
        config = load_config(profile, region)
        signer = signer_for(config)
        kwargs = {"config": config, "signer": signer} if signer else {"config": config}
        if kind == "network":
            created = oci.core.VirtualNetworkClient(**kwargs)
        elif kind == "identity":
            created = oci.identity.IdentityClient(**kwargs)
        elif kind == "search":
            created = oci.resource_search.ResourceSearchClient(**kwargs)
        else:
            raise ValueError(f"Unknown client type: {kind}")
        _clients[key] = created
        return created


def network(profile: str, region: str) -> oci.core.VirtualNetworkClient:
    if not region:
        raise ValueError("region is required")
    return client("network", profile, region)


def identity(profile: str, region: str | None = None) -> oci.identity.IdentityClient:
    config = load_config(profile, region)
    return client("identity", profile, config["region"])


def list_all(method, *args, **kwargs) -> list[Any]:
    return list_call_get_all_results(method, *args, **kwargs).data


def short(ocid: str | None) -> str:
    if not ocid:
        return ""
    if len(ocid) <= 28:
        return ocid
    return f"{ocid[:18]}...{ocid[-8:]}"


def compartment_name_map(profile: str, root_id: str) -> dict[str, str]:
    ident = identity(profile)
    names = {root_id: "(root)"}
    try:
        root = ident.get_compartment(root_id).data
        names[root_id] = root.name or "(root)"
    except ServiceError:
        pass
    try:
        compartments = list_all(
            ident.list_compartments,
            root_id,
            compartment_id_in_subtree=True,
            access_level="ACCESSIBLE",
        )
    except TypeError:
        compartments = list_all(ident.list_compartments, root_id, compartment_id_in_subtree=True)
    for item in compartments:
        if getattr(item, "lifecycle_state", "ACTIVE") == "ACTIVE":
            names[item.id] = item.name
    return names


def list_profiles() -> list[str]:
    path = os.path.expanduser(request.args.get("config") or "~/.oci/config")
    if not os.path.exists(path):
        return []
    profiles = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line.startswith("[") and line.endswith("]") and not line.startswith("[#"):
                profiles.append(line[1:-1])
    return profiles


def known_regions() -> list[str]:
    regions = list(getattr(oci.regions, "REGIONS", []) or [])
    merged = []
    seen = set()
    for name in list(regions) + FALLBACK_REGIONS:
        if name and name not in seen:
            seen.add(name)
            merged.append(name)
    return sorted(merged)


def subnet_cidrs(subnet) -> list[str]:
    blocks = list(getattr(subnet, "ipv4_cidr_blocks", None) or [])
    if not blocks and getattr(subnet, "cidr_block", None):
        blocks = [subnet.cidr_block]
    return [block for block in blocks if block]


def rule_to_dict(rule) -> dict[str, Any]:
    data = to_plain(rule) or {}
    if not isinstance(data, dict):
        return {"raw": data}
    return data


def port_range(spec: dict[str, Any] | None):
    if not spec:
        return None
    minimum = spec.get("min")
    maximum = spec.get("max", minimum)
    if minimum in (None, "") or maximum in (None, ""):
        return None
    item = PortRange()
    item.min = int(minimum)
    item.max = int(maximum)
    return item


def protocol_options(protocol: str, spec: dict[str, Any]):
    tcp = udp = None
    code = str(protocol or "all").lower()
    if code in {"6", "tcp"}:
        tcp = TcpOptions()
        tcp.destination_port_range = port_range(spec.get("destination_port_range"))
        tcp.source_port_range = port_range(spec.get("source_port_range"))
        if tcp.destination_port_range is None and tcp.source_port_range is None:
            tcp = None
        code = "6"
    elif code in {"17", "udp"}:
        udp = UdpOptions()
        udp.destination_port_range = port_range(spec.get("destination_port_range"))
        udp.source_port_range = port_range(spec.get("source_port_range"))
        if udp.destination_port_range is None and udp.source_port_range is None:
            udp = None
        code = "17"
    elif code in {"1", "icmp"}:
        code = "1"
    elif code in {"all", "any", "*"}:
        code = "all"
    return code, tcp, udp


def build_ingress(spec: dict[str, Any]) -> IngressSecurityRule:
    protocol, tcp, udp = protocol_options(spec.get("protocol") or "all", spec)
    rule = IngressSecurityRule()
    rule.protocol = protocol
    rule.source = spec["source"]
    rule.source_type = spec.get("source_type") or "CIDR_BLOCK"
    rule.is_stateless = bool(spec.get("is_stateless") or False)
    rule.description = spec.get("description") or "drg-peering"
    rule.tcp_options = tcp
    rule.udp_options = udp
    return rule


def build_egress(spec: dict[str, Any]) -> EgressSecurityRule:
    protocol, tcp, udp = protocol_options(spec.get("protocol") or "all", spec)
    rule = EgressSecurityRule()
    rule.protocol = protocol
    rule.destination = spec["destination"]
    rule.destination_type = spec.get("destination_type") or "CIDR_BLOCK"
    rule.is_stateless = bool(spec.get("is_stateless") or False)
    rule.description = spec.get("description") or "drg-peering"
    rule.tcp_options = tcp
    rule.udp_options = udp
    return rule


def same_ingress(existing, candidate: IngressSecurityRule) -> bool:
    return (
        getattr(existing, "source", None) == candidate.source
        and str(getattr(existing, "protocol", "")) == str(candidate.protocol)
        and (getattr(existing, "source_type", None) or "CIDR_BLOCK") == candidate.source_type
    )


def same_egress(existing, candidate: EgressSecurityRule) -> bool:
    return (
        getattr(existing, "destination", None) == candidate.destination
        and str(getattr(existing, "protocol", "")) == str(candidate.protocol)
        and (getattr(existing, "destination_type", None) or "CIDR_BLOCK") == candidate.destination_type
    )


def copy_security_rule(rule, direction: str):
    """Rebuild a returned security rule so it can be sent back on update."""
    if direction == "ingress":
        copied = IngressSecurityRule()
        copied.source = rule.source
        copied.source_type = rule.source_type
    else:
        copied = EgressSecurityRule()
        copied.destination = rule.destination
        copied.destination_type = rule.destination_type
    copied.protocol = rule.protocol
    copied.is_stateless = rule.is_stateless
    copied.description = rule.description
    copied.icmp_options = rule.icmp_options
    copied.tcp_options = rule.tcp_options
    copied.udp_options = rule.udp_options
    return copied


def copy_route_rule(rule) -> RouteRule:
    copied = RouteRule()
    copied.destination = rule.destination or rule.cidr_block
    copied.destination_type = rule.destination_type or "CIDR_BLOCK"
    copied.network_entity_id = rule.network_entity_id
    copied.description = rule.description
    copied.route_type = rule.route_type
    return copied


def wait_until_available(getter, target_states: tuple[str, ...] = ("AVAILABLE",), timeout: int = 180):
    response = getter()
    try:
        return oci.wait_until(
            None,
            response,
            evaluate_response=lambda r: getattr(r.data, "lifecycle_state", None) in target_states,
            max_wait_seconds=timeout,
            max_interval_seconds=5,
        ).data
    except Exception:
        # wait_until needs a client in some SDK versions; fall back to the client-bound helper.
        return response.data


def vcn_network_details(vcn_id: str, route_type: str, route_table_id: str | None = None):
    details = VcnDrgAttachmentNetworkCreateDetails()
    details.id = vcn_id
    details.vcn_route_type = route_type
    if route_type == "SUBNET_CIDRS":
        details.route_table_id = None
    elif route_table_id:
        details.route_table_id = route_table_id
    return details


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/health")
@handle_errors
def health():
    return jsonify({"ok": True, "sdk": oci.__version__})


@app.get("/api/bootstrap")
@handle_errors
def bootstrap():
    profiles = list_profiles()
    selected = request.args.get("profile") or (profiles[0] if profiles else "DEFAULT")
    regions = known_regions()
    tenancy = None
    subscribed = []
    config_error = None
    try:
        config = load_config(selected)
        tenancy = config.get("tenancy")
        home = config.get("region") or "us-ashburn-1"
        ident = identity(selected, home)
        subscriptions = list_all(ident.list_region_subscriptions, tenancy)
        subscribed = sorted(
            {
                item.region_name
                for item in subscriptions
                if getattr(item, "status", "READY") in {"READY", "IN_PROGRESS", None}
            }
        )
    except Exception as exc:  # noqa: BLE001 - bootstrap should still render the page
        config_error = f"{type(exc).__name__}: {exc}"
    return jsonify(
        {
            "ok": True,
            "profiles": profiles,
            "profile": selected,
            "tenancy": tenancy,
            "regions": regions,
            "subscribed_regions": subscribed,
            "config": os.path.expanduser(request.args.get("config") or "~/.oci/config"),
            "config_error": config_error,
            "sdk": oci.__version__,
        }
    )


@app.get("/api/compartments")
@handle_errors
def compartments():
    profile = profile_name()
    config = load_config(profile)
    names = compartment_name_map(profile, config["tenancy"])
    items = [{"id": cid, "name": name} for cid, name in sorted(names.items(), key=lambda pair: pair[1].lower())]
    return jsonify({"ok": True, "tenancy": config["tenancy"], "compartments": items})


@app.get("/api/drgs")
@handle_errors
def drgs():
    profile = profile_name()
    region = request.args.get("region")
    compartment_id = request.args.get("compartment_id")
    if not region or not compartment_id:
        raise ValueError("region and compartment_id are required")
    vn = network(profile, region)
    items = []
    for drg in list_all(vn.list_drgs, compartment_id):
        if getattr(drg, "lifecycle_state", "") == "TERMINATED":
            continue
        items.append(
            {
                "id": drg.id,
                "name": drg.display_name,
                "state": drg.lifecycle_state,
                "compartment_id": drg.compartment_id,
                "region": region,
                "redundancy": getattr(drg, "redundancy_status", None),
            }
        )
    items.sort(key=lambda item: (item["name"] or "").lower())
    return jsonify({"ok": True, "drgs": items})


@app.post("/api/drgs")
@handle_errors
def create_drg():
    data = request_json()
    require(data, "profile", "region", "compartment_id", "display_name")
    name = data["display_name"].strip()
    if not name:
        raise ValueError("DRG name is required")
    vn = network(data["profile"], data["region"])
    details = CreateDrgDetails()
    details.compartment_id = data["compartment_id"]
    details.display_name = name
    created = vn.create_drg(details).data
    return jsonify({"ok": True, "drg": to_plain(created), "id": created.id, "region": data["region"]})


@app.get("/api/vcns")
@handle_errors
def vcns():
    profile = profile_name()
    region = request.args.get("region")
    compartment_id = request.args.get("compartment_id")
    if not region or not compartment_id:
        raise ValueError("region and compartment_id are required")
    vn = network(profile, region)
    items = []
    for vcn in list_all(vn.list_vcns, compartment_id):
        if getattr(vcn, "lifecycle_state", "") not in {"AVAILABLE", "UPDATING", "PROVISIONING"}:
            continue
        items.append(
            {
                "id": vcn.id,
                "name": vcn.display_name,
                "cidrs": list(vcn.cidr_blocks or ([vcn.cidr_block] if vcn.cidr_block else [])),
                "compartment_id": vcn.compartment_id,
                "default_route_table_id": vcn.default_route_table_id,
                "default_security_list_id": vcn.default_security_list_id,
                "state": vcn.lifecycle_state,
            }
        )
    items.sort(key=lambda item: (item["name"] or "").lower())
    return jsonify({"ok": True, "vcns": items})


@app.get("/api/subnets")
@handle_errors
def subnets():
    profile = profile_name()
    region = request.args.get("region")
    compartment_id = request.args.get("compartment_id")
    vcn_id = request.args.get("vcn_id")
    if not region or not compartment_id:
        raise ValueError("region and compartment_id are required")
    vn = network(profile, region)
    kwargs = {"vcn_id": vcn_id} if vcn_id else {}
    items = []
    for subnet in list_all(vn.list_subnets, compartment_id, **kwargs):
        if getattr(subnet, "lifecycle_state", "") == "TERMINATED":
            continue
        items.append(
            {
                "id": subnet.id,
                "name": subnet.display_name,
                "cidrs": subnet_cidrs(subnet),
                "cidr": subnet.cidr_block,
                "vcn_id": subnet.vcn_id,
                "compartment_id": subnet.compartment_id,
                "route_table_id": subnet.route_table_id,
                "security_list_ids": list(subnet.security_list_ids or []),
                "prohibit_public_ip": subnet.prohibit_public_ip_on_vnic,
                "ad": subnet.availability_domain,
                "state": subnet.lifecycle_state,
                "region": region,
            }
        )
    items.sort(key=lambda item: (item["name"] or "").lower())
    return jsonify({"ok": True, "subnets": items})


def attachment_dict(item) -> dict[str, Any]:
    network_details = to_plain(item.network_details) or {}
    return {
        "id": item.id,
        "name": item.display_name,
        "state": item.lifecycle_state,
        "type": network_details.get("type") or ("VCN" if item.vcn_id else None),
        "network_id": network_details.get("id") or item.vcn_id,
        "vcn_id": item.vcn_id or (network_details.get("id") if network_details.get("type") == "VCN" else None),
        "vcn_route_type": network_details.get("vcn_route_type"),
        "route_table_id": network_details.get("route_table_id") or item.route_table_id,
        "drg_route_table_id": item.drg_route_table_id,
        "export_drg_route_distribution_id": item.export_drg_route_distribution_id,
        "compartment_id": item.compartment_id,
        "is_cross_tenancy": item.is_cross_tenancy,
    }


@app.get("/api/drg-context")
@handle_errors
def drg_context():
    """Attachments, RPCs and non-default DRG route tables for one DRG.

    VCN attachments live in the VCN compartment. RPC attachments live in the
    RPC compartment, which is not always the compartment passed in. Query both.
    """
    profile = profile_name()
    region = request.args.get("region")
    compartment_id = request.args.get("compartment_id")
    drg_id = request.args.get("drg_id")
    vcn_compartment_id = request.args.get("vcn_compartment_id")
    require({"region": region, "compartment_id": compartment_id, "drg_id": drg_id}, "region", "compartment_id", "drg_id")
    vn = network(profile, region)
    compartments = [compartment_id]
    if vcn_compartment_id and vcn_compartment_id not in compartments:
        compartments.append(vcn_compartment_id)
    attachments = []
    seen = set()
    for cid in compartments:
        for item in list_all(vn.list_drg_attachments, cid, drg_id=drg_id):
            if item.lifecycle_state in {"DETACHING", "DETACHED"} or item.id in seen:
                continue
            seen.add(item.id)
            attachments.append(attachment_dict(item))
    rpcs = []
    for item in list_all(vn.list_remote_peering_connections, compartment_id, drg_id=drg_id):
        if item.lifecycle_state in {"TERMINATING", "TERMINATED"}:
            continue
        rpcs.append(
            {
                "id": item.id,
                "name": item.display_name,
                "state": item.lifecycle_state,
                "peering_status": item.peering_status,
                "peer_id": item.peer_id,
                "peer_region_name": item.peer_region_name,
                "drg_id": item.drg_id,
                "compartment_id": item.compartment_id,
                "region": region,
                "is_cross_tenancy": item.is_cross_tenancy_peering,
            }
        )
    # An RPC becomes a DRG attachment in the RPC's own compartment. Pull those
    # in so the new DRG route table can import exactly that RPC, not every attachment.
    for rpc in rpcs:
        if any(item["type"] == "REMOTE_PEERING_CONNECTION" and item["network_id"] == rpc["id"] for item in attachments):
            continue
        rpc_compartment = rpc["compartment_id"] or compartment_id
        for item in list_all(
            vn.list_drg_attachments,
            rpc_compartment,
            drg_id=drg_id,
            attachment_type="REMOTE_PEERING_CONNECTION",
            network_id=rpc["id"],
        ):
            if item.lifecycle_state in {"DETACHING", "DETACHED"} or item.id in seen:
                continue
            seen.add(item.id)
            attachments.append(attachment_dict(item))
    tables = []
    distributions = []
    for item in list_all(vn.list_drg_route_tables, drg_id):
        if item.lifecycle_state == "TERMINATED":
            continue
        tables.append(
            {
                "id": item.id,
                "name": item.display_name,
                "state": item.lifecycle_state,
                "import_drg_route_distribution_id": item.import_drg_route_distribution_id,
                "is_ecmp_enabled": item.is_ecmp_enabled,
                "is_default_name": (item.display_name or "").lower() in {"default", "autogenerated"},
            }
        )
    for item in list_all(vn.list_drg_route_distributions, drg_id):
        if item.lifecycle_state == "TERMINATED":
            continue
        distributions.append(
            {
                "id": item.id,
                "name": item.display_name,
                "state": item.lifecycle_state,
                "distribution_type": item.distribution_type,
            }
        )
    return jsonify(
        {
            "ok": True,
            "attachments": attachments,
            "rpcs": rpcs,
            "drg_route_tables": tables,
            "drg_route_distributions": distributions,
        }
    )


@app.get("/api/route-tables")
@handle_errors
def route_tables():
    profile = profile_name()
    region = request.args.get("region")
    compartment_id = request.args.get("compartment_id")
    vcn_id = request.args.get("vcn_id")
    require({"region": region, "compartment_id": compartment_id, "vcn_id": vcn_id}, "region", "compartment_id", "vcn_id")
    vn = network(profile, region)
    items = []
    for item in list_all(vn.list_route_tables, compartment_id, vcn_id=vcn_id):
        if item.lifecycle_state == "TERMINATED":
            continue
        items.append(
            {
                "id": item.id,
                "name": item.display_name,
                "vcn_id": item.vcn_id,
                "compartment_id": item.compartment_id,
                "state": item.lifecycle_state,
                "rule_count": len(item.route_rules or []),
            }
        )
    items.sort(key=lambda item: (item["name"] or "").lower())
    return jsonify({"ok": True, "route_tables": items})


@app.get("/api/route-table")
@handle_errors
def route_table():
    profile = profile_name()
    region = request.args.get("region")
    route_table_id = request.args.get("route_table_id")
    require({"region": region, "route_table_id": route_table_id}, "region", "route_table_id")
    vn = network(profile, region)
    item = vn.get_route_table(route_table_id).data
    rules = []
    for rule in item.route_rules or []:
        rules.append(
            {
                "destination": rule.destination or rule.cidr_block,
                "destination_type": rule.destination_type,
                "network_entity_id": rule.network_entity_id,
                "description": rule.description,
                "route_type": rule.route_type,
            }
        )
    return jsonify(
        {
            "ok": True,
            "route_table": {
                "id": item.id,
                "name": item.display_name,
                "vcn_id": item.vcn_id,
                "compartment_id": item.compartment_id,
                "state": item.lifecycle_state,
                "rules": rules,
            },
        }
    )


@app.get("/api/security-list")
@handle_errors
def security_list():
    profile = profile_name()
    region = request.args.get("region")
    security_list_id = request.args.get("security_list_id")
    require({"region": region, "security_list_id": security_list_id}, "region", "security_list_id")
    vn = network(profile, region)
    item = vn.get_security_list(security_list_id).data
    return jsonify(
        {
            "ok": True,
            "security_list": {
                "id": item.id,
                "name": item.display_name,
                "vcn_id": item.vcn_id,
                "compartment_id": item.compartment_id,
                "state": item.lifecycle_state,
                "ingress": [rule_to_dict(rule) for rule in item.ingress_security_rules or []],
                "egress": [rule_to_dict(rule) for rule in item.egress_security_rules or []],
            },
        }
    )


@app.get("/api/drg-route-table")
@handle_errors
def drg_route_table():
    profile = profile_name()
    region = request.args.get("region")
    table_id = request.args.get("drg_route_table_id")
    require({"region": region, "drg_route_table_id": table_id}, "region", "drg_route_table_id")
    vn = network(profile, region)
    table = vn.get_drg_route_table(table_id).data
    rules = []
    for rule in list_all(vn.list_drg_route_rules, table_id):
        rules.append(
            {
                "id": rule.id,
                "destination": rule.destination,
                "destination_type": rule.destination_type,
                "next_hop_drg_attachment_id": rule.next_hop_drg_attachment_id,
                "route_type": rule.route_type,
                "is_conflict": rule.is_conflict,
                "is_blackhole": rule.is_blackhole,
                "route_provenance": rule.route_provenance,
            }
        )
    statements = []
    match_all = False
    if table.import_drg_route_distribution_id:
        distribution_statements = list_all(
            vn.list_drg_route_distribution_statements,
            table.import_drg_route_distribution_id,
        )
        statements = [to_plain(statement) for statement in distribution_statements]
        match_all = any(
            getattr(statement, "action", None) == "ACCEPT" and statement_is_match_all(statement)
            for statement in distribution_statements
        )
    return jsonify(
        {
            "ok": True,
            "drg_route_table": {
                "id": table.id,
                "name": table.display_name,
                "state": table.lifecycle_state,
                "import_drg_route_distribution_id": table.import_drg_route_distribution_id,
                "match_all": match_all,
                "is_ecmp_enabled": table.is_ecmp_enabled,
                "rules": rules,
                "import_statements": statements,
            },
        }
    )


@app.post("/api/rpc")
@handle_errors
def create_rpc():
    data = request_json()
    require(data, "profile", "region", "compartment_id", "drg_id", "display_name")
    vn = network(data["profile"], data["region"])
    details = CreateRemotePeeringConnectionDetails()
    details.compartment_id = data["compartment_id"]
    details.drg_id = data["drg_id"]
    details.display_name = data["display_name"]
    created = vn.create_remote_peering_connection(details).data
    return jsonify({"ok": True, "rpc": to_plain(created), "id": created.id, "region": data["region"]})


@app.post("/api/rpc/connect")
@handle_errors
def connect_rpc():
    """Connect from the requester RPC to the accepter RPC. Requester region initiates."""
    data = request_json()
    require(data, "profile", "region", "rpc_id", "peer_id", "peer_region_name")
    if data["region"] == data["peer_region_name"] and data["rpc_id"] == data["peer_id"]:
        raise ValueError("An RPC cannot be connected to itself")
    vn = network(data["profile"], data["region"])
    details = ConnectRemotePeeringConnectionsDetails()
    details.peer_id = data["peer_id"]
    details.peer_region_name = data["peer_region_name"]
    vn.connect_remote_peering_connections(data["rpc_id"], details)
    return jsonify(
        {
            "ok": True,
            "message": f"Peering requested from {data['region']} to {data['peer_region_name']}",
            "rpc_id": data["rpc_id"],
            "peer_id": data["peer_id"],
        }
    )


@app.delete("/api/rpc")
@handle_errors
def delete_rpc():
    data = request_json()
    require(data, "profile", "region", "rpc_id")
    vn = network(data["profile"], data["region"])
    vn.delete_remote_peering_connection(data["rpc_id"])
    return jsonify({"ok": True, "deleted": data["rpc_id"]})


@app.post("/api/attachments")
@handle_errors
def create_attachment():
    data = request_json()
    require(data, "profile", "region", "drg_id", "vcn_id", "vcn_route_type")
    route_type = data["vcn_route_type"]
    if route_type not in {"VCN_CIDRS", "SUBNET_CIDRS"}:
        raise ValueError("vcn_route_type must be VCN_CIDRS or SUBNET_CIDRS")
    if route_type == "VCN_CIDRS" and not data.get("route_table_id"):
        raise ValueError("VCN_CIDRS requires a VCN route table, usually the VCN default route table")
    vn = network(data["profile"], data["region"])
    details = CreateDrgAttachmentDetails()
    details.drg_id = data["drg_id"]
    details.display_name = data.get("display_name") or f"vcn-attach-{short(data['vcn_id'])}"
    # OCI rejects the request when both vcnId and networkDetails are set.
    details.network_details = vcn_network_details(data["vcn_id"], route_type, data.get("route_table_id"))
    if data.get("drg_route_table_id"):
        details.drg_route_table_id = data["drg_route_table_id"]
    created = vn.create_drg_attachment(details).data
    return jsonify({"ok": True, "attachment": to_plain(created), "id": created.id})


@app.put("/api/attachments")
@handle_errors
def update_attachment():
    data = request_json()
    require(data, "profile", "region", "attachment_id")
    vn = network(data["profile"], data["region"])
    details = UpdateDrgAttachmentDetails()
    if data.get("display_name"):
        details.display_name = data["display_name"]
    if data.get("drg_route_table_id"):
        details.drg_route_table_id = data["drg_route_table_id"]
    if data.get("vcn_route_type"):
        network_details = VcnDrgAttachmentNetworkUpdateDetails()
        network_details.vcn_route_type = data["vcn_route_type"]
        if data["vcn_route_type"] == "VCN_CIDRS":
            if not data.get("route_table_id"):
                raise ValueError("Switching to VCN_CIDRS requires route_table_id")
            network_details.route_table_id = data["route_table_id"]
        details.network_details = network_details
    updated = vn.update_drg_attachment(data["attachment_id"], details).data
    return jsonify({"ok": True, "attachment": to_plain(updated)})


@app.delete("/api/attachments")
@handle_errors
def delete_attachment():
    data = request_json()
    require(data, "profile", "region", "attachment_id")
    vn = network(data["profile"], data["region"])
    vn.delete_drg_attachment(data["attachment_id"])
    return jsonify({"ok": True, "deleted": data["attachment_id"]})


def statement_is_match_all(statement) -> bool:
    criteria_list = getattr(statement, "match_criteria", None)
    # OCI represents MATCH_ALL as an empty matchCriteria array. Older SDK
    # model variants can also deserialize an explicit MATCH_ALL criterion.
    if not criteria_list:
        return True
    for criteria in criteria_list:
        match_type = getattr(criteria, "match_type", None)
        if match_type == "MATCH_ALL" or type(criteria).__name__.endswith("MatchAllDrgRouteDistributionMatchCriteria"):
            return True
    return False


def distribution_match_all_statements(vn, distribution_id: str) -> list[Any]:
    if not distribution_id:
        return []
    return [
        statement
        for statement in list_all(vn.list_drg_route_distribution_statements, distribution_id)
        if statement_is_match_all(statement)
    ]


def distribution_has_match_all(vn, distribution_id: str) -> bool:
    return any(
        getattr(statement, "action", None) == "ACCEPT"
        for statement in distribution_match_all_statements(vn, distribution_id)
    )


def next_statement_priority(vn, distribution_id: str) -> int:
    priorities = {
        int(statement.priority)
        for statement in list_all(vn.list_drg_route_distribution_statements, distribution_id)
        if getattr(statement, "priority", None) is not None
    }
    if not priorities:
        return 0
    highest = max(priorities)
    if highest < 65535:
        return highest + 1
    for candidate in range(65535, -1, -1):
        if candidate not in priorities:
            return candidate
    raise ValueError(f"No free priority remains in route distribution {distribution_id}")


def wait_for_attachment_table(vn, attachment, timeout: int = 90):
    """A newly created attachment can exist before OCI assigns its DRG route table."""
    if getattr(attachment, "drg_route_table_id", None):
        return attachment
    deadline = time.time() + timeout
    last = attachment
    while time.time() < deadline:
        last = vn.get_drg_attachment(attachment.id).data
        if last.drg_route_table_id and last.lifecycle_state in {"ATTACHED", "AVAILABLE"}:
            return last
        time.sleep(3)
    return last


def find_rpc_attachment(vn, compartment_id: str, drg_id: str, rpc_id: str, timeout: int = 60):
    deadline = time.time() + timeout
    while True:
        items = list_all(
            vn.list_drg_attachments,
            compartment_id,
            drg_id=drg_id,
            attachment_type="REMOTE_PEERING_CONNECTION",
            network_id=rpc_id,
        )
        live = [item for item in items if item.lifecycle_state not in {"DETACHING", "DETACHED"}]
        if live:
            return live[0]
        for item in list_all(vn.list_drg_attachments, compartment_id, drg_id=drg_id, attachment_type="REMOTE_PEERING_CONNECTION"):
            network_id = getattr(item.network_details, "id", None)
            if network_id == rpc_id and item.lifecycle_state not in {"DETACHING", "DETACHED"}:
                return item
        if time.time() >= deadline:
            return None
        time.sleep(3)


def ensure_match_all(vn, attachment) -> dict[str, Any]:
    """Add MATCH_ALL to the import distribution already used by this attachment.

    Does not create a DRG route table and does not replace the attachment's table.
    """
    attachment = wait_for_attachment_table(vn, attachment)
    table_id = attachment.drg_route_table_id
    if not table_id:
        raise ValueError(f"{attachment.display_name or attachment.id} has no DRG route table yet")
    table = vn.get_drg_route_table(table_id).data
    distribution_id = table.import_drg_route_distribution_id
    if not distribution_id:
        raise ValueError(f"DRG route table {table.display_name or table.id} has no import route distribution")
    match_all_statements = distribution_match_all_statements(vn, distribution_id)
    if any(getattr(item, "action", None) == "ACCEPT" for item in match_all_statements):
        return {
            "attachment_id": attachment.id,
            "attachment_type": attachment_type_of(attachment),
            "drg_route_table_id": table.id,
            "drg_route_table_name": table.display_name,
            "import_drg_route_distribution_id": distribution_id,
            "changed": False,
            "message": "MATCH_ALL already present",
        }
    if match_all_statements:
        actions = sorted({getattr(item, "action", "UNKNOWN") for item in match_all_statements})
        raise ValueError(
            f"Import distribution {distribution_id} already has MATCH_ALL with action "
            f"{', '.join(actions)}. OCI permits only one MATCH_ALL statement."
        )
    statement = AddDrgRouteDistributionStatementDetails()
    statement.action = "ACCEPT"
    statement.priority = next_statement_priority(vn, distribution_id)
    # OCI defines MATCH_ALL as an empty matchCriteria array.
    statement.match_criteria = []
    payload = AddDrgRouteDistributionStatementsDetails()
    payload.statements = [statement]
    added = True
    try:
        vn.add_drg_route_distribution_statements(distribution_id, payload)
    except ServiceError as exc:
        # A concurrent request can add MATCH_ALL after our read. Treat the
        # duplicate-MATCH_ALL response as success after re-reading the state.
        duplicate = exc.status == 400 and "multiple DRG Route Distribution Statements" in (exc.message or "")
        if not duplicate or not distribution_has_match_all(vn, distribution_id):
            raise
        added = False
    return {
        "attachment_id": attachment.id,
        "attachment_type": attachment_type_of(attachment),
        "drg_route_table_id": table.id,
        "drg_route_table_name": table.display_name,
        "import_drg_route_distribution_id": distribution_id,
        "changed": added,
        "priority": statement.priority,
        "message": "MATCH_ALL added" if added else "MATCH_ALL already present",
    }


def ensure_import_distribution(vn, drg_id: str, display_name: str, rpc_attachment_ids: list[str]) -> str:
    details = CreateDrgRouteDistributionDetails()
    details.drg_id = drg_id
    details.display_name = display_name
    details.distribution_type = "IMPORT"
    created = vn.create_drg_route_distribution(details).data
    statements = []
    priority = 10
    # Only attachments of THIS DRG are valid match targets. A peer-region RPC
    # attachment OCID is not visible here and OCI returns NotAuthorizedOrNotFound.
    known = {
        item.id
        for item in list_all(vn.list_drg_attachments, created.compartment_id, drg_id=drg_id, attachment_type="REMOTE_PEERING_CONNECTION")
    }
    targets = [item for item in (rpc_attachment_ids or []) if item in known]
    if not targets:
        # No RPC attachment yet: match all RPC attachments so the table becomes useful
        # as soon as the RPC attaches. Still never matches VCN attachments.
        criteria = oci.core.models.DrgAttachmentTypeDrgRouteDistributionMatchCriteria()
        criteria.attachment_type = "REMOTE_PEERING_CONNECTION"
        statement = AddDrgRouteDistributionStatementDetails()
        statement.action = "ACCEPT"
        statement.priority = 1
        statement.match_criteria = [criteria]
        statements.append(statement)
    else:
        for attachment_id in targets:
            criteria = DrgAttachmentIdDrgRouteDistributionMatchCriteria()
            criteria.drg_attachment_id = attachment_id
            statement = AddDrgRouteDistributionStatementDetails()
            statement.action = "ACCEPT"
            statement.priority = priority
            statement.match_criteria = [criteria]
            statements.append(statement)
            priority += 10
    payload = AddDrgRouteDistributionStatementsDetails()
    payload.statements = statements
    vn.add_drg_route_distribution_statements(created.id, payload)
    return created.id


@app.post("/api/drg-route-imports")
@handle_errors
def ensure_drg_route_imports():
    """Confirm MATCH_ALL on the route tables already associated with the VCN and RPC attachments."""
    data = request_json()
    require(data, "profile", "region", "drg_id")
    vn = network(data["profile"], data["region"])
    compartment_id = data.get("compartment_id")
    attachments = []
    if data.get("vcn_attachment_id"):
        attachments.append(vn.get_drg_attachment(data["vcn_attachment_id"]).data)
    rpc_attachment = None
    if data.get("rpc_attachment_id"):
        rpc_attachment = vn.get_drg_attachment(data["rpc_attachment_id"]).data
    elif data.get("rpc_id") and compartment_id:
        rpc_attachment = find_rpc_attachment(vn, compartment_id, data["drg_id"], data["rpc_id"])
        if rpc_attachment is None:
            raise ValueError("RPC attachment is not visible yet. Wait until the RPC attaches to the DRG, then try again.")
    if rpc_attachment is not None:
        attachments.append(rpc_attachment)
    if not attachments:
        raise ValueError("Need both the VCN attachment and the RPC attachment")
    results = []
    for attachment in attachments:
        if attachment.drg_id != data["drg_id"]:
            raise ValueError(f"{attachment.id} does not belong to the selected DRG")
        results.append(ensure_match_all(vn, attachment))
    vcn_result = next((item for item in results if item.get("attachment_type") == "VCN"), None)
    rpc_result = next((item for item in results if item.get("attachment_type") == "REMOTE_PEERING_CONNECTION"), None)
    if vcn_result is None or rpc_result is None:
        raise ValueError("Could not ensure MATCH_ALL on both the VCN attachment and the RPC attachment route tables")
    return jsonify(
        {
            "ok": True,
            "results": results,
            "changed": any(item["changed"] for item in results),
            "vcn_import": vcn_result,
            "rpc_import": rpc_result,
        }
    )


def attachment_type_of(attachment) -> str:
    details_object = getattr(attachment, "network_details", None)
    network_details = to_plain(details_object) or {}
    kind = network_details.get("type") if isinstance(network_details, dict) else getattr(details_object, "type", None)
    if kind:
        return kind
    if getattr(attachment, "vcn_id", None):
        return "VCN"
    class_name = type(details_object).__name__.upper()
    if "VCN" in class_name:
        return "VCN"
    if "REMOTEPEERINGCONNECTION" in class_name or "REMOTE_PEERING_CONNECTION" in class_name:
        return "REMOTE_PEERING_CONNECTION"
    return ""


def serialize_drg_rule(rule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "destination": rule.destination,
        "next_hop_drg_attachment_id": rule.next_hop_drg_attachment_id,
        "route_type": rule.route_type,
        "route_provenance": rule.route_provenance,
        "is_conflict": rule.is_conflict,
        "is_blackhole": rule.is_blackhole,
    }


def list_table_rules(vn, table_id: str) -> list[dict[str, Any]]:
    return [serialize_drg_rule(rule) for rule in list_all(vn.list_drg_route_rules, table_id)]


def peer_routes_in_table(vn, table_id: str, destinations: list[str]) -> list[dict[str, Any]]:
    wanted = set(destinations)
    return [rule for rule in list_table_rules(vn, table_id) if rule["destination"] in wanted]


def ensure_static_peer_routes(vn, table_id: str, next_hop_id: str, destinations: list[str]) -> dict[str, Any]:
    existing = {
        (rule.destination, rule.next_hop_drg_attachment_id)
        for rule in list_all(vn.list_drg_route_rules, table_id)
        if not rule.is_blackhole
    }
    missing = [cidr for cidr in destinations if (cidr, next_hop_id) not in existing]
    added = []
    if missing:
        built = []
        for cidr in missing:
            rule = AddDrgRouteRuleDetails()
            rule.destination = cidr
            rule.destination_type = "CIDR_BLOCK"
            rule.next_hop_drg_attachment_id = next_hop_id
            built.append(rule)
        payload = AddDrgRouteRulesDetails()
        payload.route_rules = built
        created = vn.add_drg_route_rules(table_id, payload).data or []
        added = [to_plain(item) for item in created]
    return {"added": missing, "rules": added}


@app.post("/api/drg-peer-routes")
@app.post("/api/drg-attachment-routes")
@handle_errors
def ensure_drg_peer_routes():
    """Read VCN and RPC attachment route tables and verify expected learned CIDRs.

    This endpoint is read-only. VCN table routes should learn peer CIDRs from
    the local RPC attachment. RPC table routes should learn local CIDRs from
    the local VCN attachment.
    """
    data = request_json()
    require(data, "profile", "region", "drg_id", "vcn_attachment_id", "local_cidrs", "peer_cidrs")
    local_cidrs = [cidr for cidr in (data.get("local_cidrs") or []) if cidr]
    peer_cidrs = [cidr for cidr in (data.get("peer_cidrs") or []) if cidr]
    if not local_cidrs or not peer_cidrs:
        raise ValueError("local_cidrs and peer_cidrs are required")
    vn = network(data["profile"], data["region"])
    vcn_attachment = wait_for_attachment_table(vn, vn.get_drg_attachment(data["vcn_attachment_id"]).data)
    if vcn_attachment.drg_id != data["drg_id"]:
        raise ValueError("VCN attachment does not belong to the selected DRG")
    rpc_attachment = None
    if data.get("rpc_attachment_id"):
        rpc_attachment = wait_for_attachment_table(vn, vn.get_drg_attachment(data["rpc_attachment_id"]).data)
    elif data.get("rpc_id") and data.get("compartment_id"):
        find_timeout = min(max(int(data.get("attachment_wait_seconds") or 0), 0), 60)
        found = find_rpc_attachment(
            vn,
            data["compartment_id"],
            data["drg_id"],
            data["rpc_id"],
            timeout=find_timeout,
        )
        if found is None:
            raise ValueError("RPC attachment is not visible yet")
        rpc_attachment = wait_for_attachment_table(vn, found)
    if rpc_attachment is None:
        raise ValueError("Need the RPC attachment to install peer routes")
    specs = [
        # Traffic leaving the VCN needs peer CIDRs through the RPC attachment.
        (vcn_attachment, rpc_attachment.id, "VCN", peer_cidrs, "REMOTE_DESTINATIONS"),
        # Traffic arriving from the RPC needs local CIDRs through the VCN attachment.
        (rpc_attachment, vcn_attachment.id, "REMOTE_PEERING_CONNECTION", local_cidrs, "LOCAL_DESTINATIONS"),
    ]
    ensure_imports = bool(data.get("ensure_match_all"))
    import_results: list[dict[str, Any]] = []
    import_errors: list[dict[str, str]] = []
    for attachment, _next_hop, kind, _expected_cidrs, _route_role in specs:
        if not attachment.drg_route_table_id:
            raise ValueError(f"{kind} attachment has no DRG route table yet")
        if ensure_imports:
            try:
                import_results.append(ensure_match_all(vn, attachment))
            except Exception as exc:  # Keep returning route tables for diagnosis.
                import_errors.append({"attachment_type": kind, "error": str(exc)})

    wait_seconds = min(max(int(data.get("wait_seconds") or 0), 0), 90)
    deadline = time.time() + wait_seconds
    snapshots: list[dict[str, Any]] = []
    while True:
        snapshots = []
        all_learned = True
        for attachment, next_hop, kind, expected_cidrs, route_role in specs:
            table = vn.get_drg_route_table(attachment.drg_route_table_id).data
            all_rules = list_table_rules(vn, table.id)
            expected_set = set(expected_cidrs)
            expected_routes = [rule for rule in all_rules if rule["destination"] in expected_set]
            valid = [
                rule
                for rule in expected_routes
                if rule["next_hop_drg_attachment_id"] == next_hop and not rule["is_blackhole"]
            ]
            learned = {rule["destination"] for rule in valid}
            missing = [cidr for cidr in expected_cidrs if cidr not in learned]
            if missing:
                all_learned = False
            snapshots.append(
                {
                    "attachment_type": kind,
                    "attachment_id": attachment.id,
                    "drg_route_table_id": table.id,
                    "drg_route_table_name": table.display_name,
                    "import_drg_route_distribution_id": table.import_drg_route_distribution_id,
                    "match_all": distribution_has_match_all(vn, table.import_drg_route_distribution_id),
                    "route_role": route_role,
                    "expected_cidrs": expected_cidrs,
                    "next_hop_drg_attachment_id": next_hop,
                    "learned": sorted(learned),
                    "missing": missing,
                    "expected_routes": expected_routes,
                    "routes": all_rules,
                }
            )
        if all_learned or time.time() >= deadline:
            break
        time.sleep(3)

    return jsonify(
        {
            "ok": True,
            "tables": snapshots,
            "local_cidrs": local_cidrs,
            "peer_cidrs": peer_cidrs,
            "all_routes_learned": all(not table["missing"] for table in snapshots),
            "import_results": import_results,
            "import_errors": import_errors,
        }
    )


@app.post("/api/drg-route-tables")
@handle_errors
def create_drg_route_table():
    """Always create a new DRG route table. The DRG default table is never selected or updated."""
    data = request_json()
    require(data, "profile", "region", "drg_id", "display_name")
    name = data["display_name"].strip()
    if name.lower() in {"default", "autogenerated"}:
        raise ValueError("Refusing Default / Autogenerated. This action only creates a dedicated DRG route table")
    vn = network(data["profile"], data["region"])
    distribution_id = data.get("import_drg_route_distribution_id")
    if not distribution_id:
        distribution_id = ensure_import_distribution(
            vn,
            data["drg_id"],
            data.get("distribution_name") or f"{name}-import",
            list(data.get("rpc_attachment_ids") or []),
        )
    details = CreateDrgRouteTableDetails()
    details.drg_id = data["drg_id"]
    details.display_name = name
    details.import_drg_route_distribution_id = distribution_id
    details.is_ecmp_enabled = bool(data.get("is_ecmp_enabled") or False)
    created = vn.create_drg_route_table(details).data
    assigned = []
    for attachment_id in data.get("assign_attachment_ids") or []:
        update = UpdateDrgAttachmentDetails()
        update.drg_route_table_id = created.id
        vn.update_drg_attachment(attachment_id, update)
        assigned.append(attachment_id)
    return jsonify(
        {
            "ok": True,
            "drg_route_table": to_plain(created),
            "id": created.id,
            "import_drg_route_distribution_id": distribution_id,
            "assigned_attachment_ids": assigned,
        }
    )


@app.post("/api/drg-route-rules")
@handle_errors
def add_drg_static_rules():
    data = request_json()
    require(data, "profile", "region", "drg_route_table_id", "rules")
    if not isinstance(data["rules"], list) or not data["rules"]:
        raise ValueError("rules must not be empty")
    vn = network(data["profile"], data["region"])
    built = []
    for spec in data["rules"]:
        if not spec.get("destination") or not spec.get("next_hop_drg_attachment_id"):
            raise ValueError("Each DRG static route needs a destination and the peer attachment id")
        rule = AddDrgRouteRuleDetails()
        rule.destination = spec["destination"]
        rule.destination_type = "CIDR_BLOCK"
        rule.next_hop_drg_attachment_id = spec["next_hop_drg_attachment_id"]
        built.append(rule)
    payload = AddDrgRouteRulesDetails()
    payload.route_rules = built
    result = vn.add_drg_route_rules(data["drg_route_table_id"], payload).data
    return jsonify({"ok": True, "rules": [to_plain(item) for item in result or []]})


@app.delete("/api/drg-route-rules")
@handle_errors
def remove_drg_static_rules():
    data = request_json()
    require(data, "profile", "region", "drg_route_table_id", "route_rule_ids")
    vn = network(data["profile"], data["region"])
    payload = RemoveDrgRouteRulesDetails()
    payload.route_rule_ids = list(data["route_rule_ids"])
    vn.remove_drg_route_rules(data["drg_route_table_id"], payload)
    return jsonify({"ok": True, "removed": payload.route_rule_ids})


@app.post("/api/route-rules")
@handle_errors
def add_vcn_route_rules():
    """Append CIDR routes toward a DRG on the subnet (or VCN) route table. Existing rules are preserved."""
    data = request_json()
    require(data, "profile", "region", "route_table_id", "rules")
    vn = network(data["profile"], data["region"])
    current = vn.get_route_table(data["route_table_id"]).data
    merged = [copy_route_rule(rule) for rule in current.route_rules or []]
    existing_keys = {(rule.destination, rule.network_entity_id) for rule in merged}
    added = []
    skipped = []
    for spec in data["rules"]:
        destination = spec.get("destination")
        entity = spec.get("network_entity_id")
        if not destination or not entity:
            raise ValueError("Each route needs a destination and network_entity_id (the DRG OCID)")
        key = (destination, entity)
        if key in existing_keys:
            skipped.append(destination)
            continue
        rule = RouteRule()
        rule.destination = destination
        rule.destination_type = spec.get("destination_type") or "CIDR_BLOCK"
        rule.network_entity_id = entity
        rule.description = spec.get("description") or "cross-region subnet via DRG"
        merged.append(rule)
        existing_keys.add(key)
        added.append(destination)
    if not added:
        return jsonify({"ok": True, "added": [], "skipped": skipped, "message": "Those CIDRs are already in the route table"})
    update = UpdateRouteTableDetails()
    update.route_rules = merged
    updated = vn.update_route_table(data["route_table_id"], update).data
    return jsonify({"ok": True, "added": added, "skipped": skipped, "route_table": to_plain(updated)})


@app.delete("/api/route-rules")
@handle_errors
def remove_vcn_route_rules():
    data = request_json()
    require(data, "profile", "region", "route_table_id", "destinations")
    vn = network(data["profile"], data["region"])
    current = vn.get_route_table(data["route_table_id"]).data
    drop = set(data["destinations"])
    entity = data.get("network_entity_id")
    kept = []
    removed = []
    for rule in current.route_rules or []:
        destination = rule.destination or rule.cidr_block
        matches_entity = entity is None or rule.network_entity_id == entity
        if destination in drop and matches_entity:
            removed.append(destination)
            continue
        kept.append(copy_route_rule(rule))
    update = UpdateRouteTableDetails()
    update.route_rules = kept
    vn.update_route_table(data["route_table_id"], update)
    return jsonify({"ok": True, "removed": removed})


@app.post("/api/security-rules")
@handle_errors
def add_security_rules():
    data = request_json()
    require(data, "profile", "region", "security_list_id")
    ingress_specs = data.get("ingress") or []
    egress_specs = data.get("egress") or []
    if not ingress_specs and not egress_specs:
        raise ValueError("At least one ingress or egress rule is required")
    vn = network(data["profile"], data["region"])
    current = vn.get_security_list(data["security_list_id"]).data
    ingress = [copy_security_rule(rule, "ingress") for rule in current.ingress_security_rules or []]
    egress = [copy_security_rule(rule, "egress") for rule in current.egress_security_rules or []]
    added = {"ingress": [], "egress": []}
    skipped = {"ingress": [], "egress": []}
    for spec in ingress_specs:
        if not spec.get("source"):
            raise ValueError("ingress rules need a source CIDR")
        candidate = build_ingress(spec)
        if any(same_ingress(rule, candidate) for rule in ingress):
            skipped["ingress"].append(spec["source"])
            continue
        ingress.append(candidate)
        added["ingress"].append(spec["source"])
    for spec in egress_specs:
        if not spec.get("destination"):
            raise ValueError("egress rules need a destination CIDR")
        candidate = build_egress(spec)
        if any(same_egress(rule, candidate) for rule in egress):
            skipped["egress"].append(spec["destination"])
            continue
        egress.append(candidate)
        added["egress"].append(spec["destination"])
    if not added["ingress"] and not added["egress"]:
        return jsonify({"ok": True, "added": added, "skipped": skipped, "message": "Rules already exist; nothing new was written"})
    update = UpdateSecurityListDetails()
    update.ingress_security_rules = ingress
    update.egress_security_rules = egress
    updated = vn.update_security_list(data["security_list_id"], update).data
    return jsonify({"ok": True, "added": added, "skipped": skipped, "security_list_id": updated.id})


@app.post("/api/plan")
@handle_errors
def plan():
    """Dry description of what a dragged subnet-to-subnet link still needs. Does not call mutating APIs."""
    data = request_json()
    left = data.get("left") or {}
    right = data.get("right") or {}
    steps = []
    if not left.get("rpc_id") or not right.get("rpc_id"):
        steps.append("Create an RPC on each DRG, then connect from one side to the other.")
    elif not left.get("peering_status") or left.get("peering_status") != "PEERED":
        steps.append("RPCs exist but are not PEERED yet. Connect them.")
    if not left.get("attachment_id"):
        steps.append(f"Attach {left.get('vcn_name') or 'the Region A VCN'} to the Region A DRG, route type = {left.get('vcn_route_type') or 'SUBNET_CIDRS'}.")
    if not right.get("attachment_id"):
        steps.append(f"Attach {right.get('vcn_name') or 'the Region B VCN'} to the Region B DRG, route type = {right.get('vcn_route_type') or 'SUBNET_CIDRS'}.")
    steps.append("Create a dedicated DRG route table on each side (never the DRG default). Import only the peer RPC attachment and assign it to the local VCN attachment.")
    steps.append("Add the peer subnet CIDR to the local subnet route table, with the local DRG as the next hop.")
    steps.append("Add the peer subnet CIDR to the subnet security list as ingress and egress.")
    return jsonify(
        {
            "ok": True,
            "left_cidrs": left.get("cidrs") or [],
            "right_cidrs": right.get("cidrs") or [],
            "steps": steps,
        }
    )


def main() -> None:
    configure_console_encoding()
    host = os.environ.get("DRG_PEER_HOST", DEFAULT_HOST)
    port = int(os.environ.get("DRG_PEER_PORT", DEFAULT_PORT))
    print(f"OCI DRG peering console: http://{host}:{port}")
    print("Writes happen only after you click create, connect, or delete in the page.")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
