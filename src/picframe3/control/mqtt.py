"""MQTT bridge with Home Assistant discovery.

Publishes one retained discovery message per entity so the frame appears in
Home Assistant as a single device with a light, a pause switch, buttons,
and sensors -- no YAML on the Home Assistant side.

Differences from ``picframe``'s MQTT layer worth noting: a Last Will makes the
device show as unavailable within seconds of a power cut; state is published as
one JSON document that every entity reads with a template, so a frame with
twenty entities still produces one message per update instead of twenty; and
the connection retries with backoff instead of exiting when the broker is
briefly unreachable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import Any

from ..config import MqttConfig
from ..events import Action, Command

_log = logging.getLogger(__name__)

ONLINE = "online"
OFFLINE = "offline"


class MqttBridge:
    def __init__(self, app, config: MqttConfig):
        self.app = app
        self.config = config
        try:
            import aiomqtt  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "MQTT support needs aiomqtt (pip install 'picframe3[mqtt]')"
            ) from exc
        self.prefix = f"{config.topic_prefix}/{config.device_id}"
        self._client = None
        self._last_payload: str | None = None

    # -- topics ------------------------------------------------------------
    @property
    def state_topic(self) -> str:
        return f"{self.prefix}/state"

    @property
    def availability_topic(self) -> str:
        return f"{self.prefix}/availability"

    @property
    def command_topic(self) -> str:
        return f"{self.prefix}/cmd"

    def entity_topic(self, name: str) -> str:
        return f"{self.prefix}/{name}/set"

    # -- lifecycle ---------------------------------------------------------
    async def run(self) -> None:
        import aiomqtt

        delay = 2.0
        while True:
            try:
                tls = None
                if self.config.tls_ca:
                    import ssl

                    tls = ssl.create_default_context(cafile=self.config.tls_ca)
                    if self.config.tls_insecure:
                        tls.check_hostname = False
                        tls.verify_mode = ssl.CERT_NONE
                async with aiomqtt.Client(
                    hostname=self.config.host,
                    port=self.config.port,
                    username=self.config.username or None,
                    password=self.config.password or None,
                    tls_context=tls,
                    identifier=f"picframe3-{self.config.device_id}",
                    will=aiomqtt.Will(self.availability_topic, OFFLINE, qos=1, retain=True),
                    keepalive=30,
                ) as client:
                    self._client = client
                    _log.info("connected to MQTT broker %s:%d",
                              self.config.host, self.config.port)
                    delay = 2.0
                    await client.publish(self.availability_topic, ONLINE, qos=1, retain=True)
                    await self._announce(client)
                    await client.subscribe(f"{self.prefix}/#")
                    unsubscribe = self.app.bus.subscribe(self._on_state)
                    try:
                        await asyncio.gather(
                            self._listen(client),
                            self._heartbeat(client),
                        )
                    finally:
                        unsubscribe()
            except asyncio.CancelledError:
                await self._goodbye()
                raise
            except Exception as exc:
                _log.warning("MQTT connection lost (%s); retrying in %.0fs", exc, delay)
                self._client = None
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

    async def _goodbye(self) -> None:
        if self._client is not None:
            try:
                await self._client.publish(self.availability_topic, OFFLINE,
                                           qos=1, retain=True)
            except Exception:  # pragma: no cover
                pass

    async def _heartbeat(self, client) -> None:
        while True:
            await self._publish_state(client, force=True)
            await asyncio.sleep(max(5.0, self.config.publish_interval))

    def _on_state(self, _state) -> None:
        if self._client is not None:
            asyncio.ensure_future(self._publish_state(self._client))

    async def _publish_state(self, client, *, force: bool = False) -> None:
        payload = json.dumps(self.app.state().as_dict(), default=str)
        if not force and payload == self._last_payload:
            return
        self._last_payload = payload
        try:
            await client.publish(self.state_topic, payload, qos=0, retain=True)
        except Exception as exc:  # pragma: no cover
            _log.debug("state publish failed: %s", exc)

    # -- inbound -----------------------------------------------------------
    async def _listen(self, client) -> None:
        async for message in client.messages:
            topic = str(message.topic)
            payload = message.payload.decode("utf-8", "replace").strip()
            if topic.endswith("/state") or topic.endswith("/availability"):
                continue
            try:
                await self._dispatch(topic, payload)
            except Exception:
                _log.exception("failed to handle MQTT message on %s", topic)

    async def _dispatch(self, topic: str, payload: str) -> None:
        leaf = topic[len(self.prefix):].strip("/")
        if leaf in ("cmd", ""):
            command = Command.parse(payload, source="mqtt")
            if command is None:
                _log.warning("ignoring unknown MQTT command %r", payload[:60])
                return
            self.app.bus.submit(command)
            return
        name = leaf.rsplit("/", 1)[0] if leaf.endswith("/set") else leaf
        low = payload.lower()

        if name == "display":
            self.app.bus.submit(Command(
                Action.DISPLAY_ON if low in ("on", "true", "1") else Action.DISPLAY_OFF,
                source="mqtt"))
        elif name == "brightness":
            try:
                self.app.bus.submit(Command(
                    Action.BRIGHTNESS, {"value": float(payload) / 255.0}, source="mqtt"))
            except ValueError:
                pass
        elif name == "pause":
            self.app.bus.submit(Command(
                Action.PAUSE if low in ("on", "true", "1") else Action.RESUME, source="mqtt"))
        elif name in ("next", "previous", "rescan", "delete"):
            self.app.bus.submit(Command(Action(name), source="mqtt"))
        elif name == "interval":
            self.app.bus.submit(Command(
                Action.SET_CONFIG, {"key": "slideshow.interval", "value": payload},
                source="mqtt"))
        elif name == "transition":
            self.app.bus.submit(Command(
                Action.SET_CONFIG, {"key": "slideshow.transition", "value": payload},
                source="mqtt"))
        elif name == "order":
            self.app.bus.submit(Command(
                Action.SET_CONFIG, {"key": "slideshow.order", "value": payload},
                source="mqtt"))
        elif name == "subfolder":
            self.app.bus.submit(Command(
                Action.SET_CONFIG, {"key": "library.subfolder", "value": payload},
                source="mqtt"))
        else:
            _log.debug("unhandled MQTT topic %s", topic)

    # -- discovery ---------------------------------------------------------
    def _device(self) -> dict[str, Any]:
        from .. import __version__

        return {
            "identifiers": [f"picframe3_{self.config.device_id}"],
            "name": self.config.device_name,
            "manufacturer": "picframe3",
            "model": "Raspberry Pi picture frame",
            "sw_version": __version__,
            "configuration_url": f"http://{socket.gethostname()}.local:"
                                 f"{self.app.config.http.port}/",
        }

    async def _announce(self, client) -> None:
        from ..gfx import transitions

        device = self._device()
        base = {
            "device": device,
            "availability_topic": self.availability_topic,
            "state_topic": self.state_topic,
            "qos": 1,
        }
        uid = self.config.device_id
        entities: list[tuple[str, str, dict[str, Any]]] = [
            ("light", "display", {
                **base,
                "name": "Display",
                "unique_id": f"picframe3_{uid}_display",
                "schema": "template",
                "command_topic": self.entity_topic("display"),
                "state_template": "{{ 'on' if value_json.display_on else 'off' }}",
                "brightness_template": "{{ (value_json.brightness * 255) | round(0) }}",
                "command_on_template": "on",
                "command_off_template": "off",
                "icon": "mdi:image-frame",
            }),
            ("switch", "pause", {
                **base,
                "name": "Pause",
                "unique_id": f"picframe3_{uid}_pause",
                "command_topic": self.entity_topic("pause"),
                "value_template": "{{ 'ON' if value_json.paused else 'OFF' }}",
                "payload_on": "on", "payload_off": "off",
                "state_on": "ON", "state_off": "OFF",
                "icon": "mdi:pause",
            }),
            ("number", "interval", {
                **base,
                "name": "Seconds per picture",
                "unique_id": f"picframe3_{uid}_interval",
                "command_topic": self.entity_topic("interval"),
                "value_template": "{{ value_json.interval }}",
                "min": 5, "max": 3600, "step": 5,
                "unit_of_measurement": "s",
                "mode": "box",
                "icon": "mdi:timer-outline",
            }),
            ("select", "transition", {
                **base,
                "name": "Transition",
                "unique_id": f"picframe3_{uid}_transition",
                "command_topic": self.entity_topic("transition"),
                "value_template": "{{ value_json.transition }}",
                "options": ["random", *transitions.names()],
                "icon": "mdi:transition",
            }),
            ("select", "order", {
                **base,
                "name": "Order",
                "unique_id": f"picframe3_{uid}_order",
                "command_topic": self.entity_topic("order"),
                "value_template": "{{ value_json.order }}",
                "options": ["shuffle", "random", "date_desc", "date_asc",
                            "name", "folder", "recent", "least_played"],
                "icon": "mdi:sort",
            }),
            ("sensor", "current", {
                **base,
                "name": "Current picture",
                "unique_id": f"picframe3_{uid}_current",
                "value_template": "{{ value_json.current.basename | default('') }}",
                "json_attributes_topic": self.state_topic,
                "json_attributes_template": "{{ value_json.current | tojson }}",
                "icon": "mdi:image",
            }),
            ("sensor", "pictures", {
                **base,
                "name": "Pictures indexed",
                "unique_id": f"picframe3_{uid}_pictures",
                "value_template": "{{ value_json.library.files | default(0) }}",
                "state_class": "measurement",
                "icon": "mdi:image-multiple",
            }),
            ("binary_sensor", "scanning", {
                **base,
                "name": "Scanning",
                "unique_id": f"picframe3_{uid}_scanning",
                "value_template": "{{ 'ON' if value_json.scanning else 'OFF' }}",
                "payload_on": "ON", "payload_off": "OFF",
                "device_class": "running",
            }),
        ]
        for action, label, icon in (
            ("next", "Next picture", "mdi:skip-next"),
            ("previous", "Previous picture", "mdi:skip-previous"),
            ("rescan", "Rescan library", "mdi:folder-refresh"),
            ("delete", "Remove current picture", "mdi:delete"),
        ):
            entities.append(("button", action, {
                "device": device,
                "availability_topic": self.availability_topic,
                "name": label,
                "unique_id": f"picframe3_{uid}_{action}",
                "command_topic": self.entity_topic(action),
                "payload_press": "press",
                "icon": icon,
                "entity_category": "config" if action == "rescan" else None,
            }))

        for component, object_id, payload in entities:
            topic = (f"{self.config.discovery_prefix}/{component}/"
                     f"picframe3_{uid}/{object_id}/config")
            clean = {k: v for k, v in payload.items() if v is not None}
            await client.publish(topic, json.dumps(clean), qos=1, retain=True)
        _log.info("published Home Assistant discovery for %d entities", len(entities))
