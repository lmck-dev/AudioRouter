"""Canned pw-dump objects, so the graph layer can be tested without PipeWire."""

from __future__ import annotations

from typing import Any

NODE = "PipeWire:Interface:Node"
PORT = "PipeWire:Interface:Port"
LINK = "PipeWire:Interface:Link"
CLIENT = "PipeWire:Interface:Client"


def node(
    node_id: int,
    name: str,
    media_class: str = "",
    *,
    serial: int | None = None,
    description: str = "",
    client_id: int | None = None,
    **props: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "node.name": name,
        "node.description": description,
        "media.class": media_class,
    }
    if serial is not None:
        payload["object.serial"] = serial
    if client_id is not None:
        payload["client.id"] = client_id
    payload.update(props)
    return {"id": node_id, "type": NODE, "info": {"props": payload}}


def sink(node_id: int, name: str, *, hardware: bool = True, **kwargs: Any) -> dict[str, Any]:
    if hardware:
        kwargs.setdefault("device.id", 40 + node_id)
    return node(node_id, name, "Audio/Sink", **kwargs)


def stream(node_id: int, name: str, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("application.name", name)
    return node(node_id, name, "Stream/Output/Audio", **kwargs)


def port(port_id: int, node_id: int, direction: str = "out") -> dict[str, Any]:
    return {
        "id": port_id,
        "type": PORT,
        "info": {"props": {"node.id": node_id, "port.direction": direction}},
    }


def link(link_id: int, output_port: int, input_port: int) -> dict[str, Any]:
    return {
        "id": link_id,
        "type": LINK,
        "info": {"output-port-id": output_port, "input-port-id": input_port},
    }


def client(client_id: int, pid: int) -> dict[str, Any]:
    return {
        "id": client_id,
        "type": CLIENT,
        "info": {"props": {"application.process.id": pid}},
    }


def removal(object_id: int) -> dict[str, Any]:
    """What pw-dump -m emits when an object goes away."""
    return {"id": object_id, "info": None}
