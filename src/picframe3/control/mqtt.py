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
from ..install import install_hint
from ..library.playlist import ANY_OTHER, ANYTHING, DATE_WINDOWS

_log = logging.getLogger(__name__)

#: An icon per rolling date window, so the buttons are distinguishable at a
#: glance on a dashboard rather than being six identical calendars.
DATE_WINDOW_ICONS = {
    "all": "mdi:calendar-blank",
    "today": "mdi:calendar-today",
    "7d": "mdi:calendar-week",
    "30d": "mdi:calendar-month",
    "90d": "mdi:calendar-range",
    "1y": "mdi:calendar-clock",
    "3y": "mdi:calendar-multiple",
    "on_this_day": "mdi:calendar-star",
}

ONLINE = "online"
OFFLINE = "offline"

#: Actions the broker may not give, whatever the payload says.  ``Command.parse``
#: accepts every action there is, and a bridge subscribed to a broker that most
#: houses run without per-client ACLs should not be a way to stop the frame --
#: `restart` is announced and does the job anyone actually wants.
MQTT_REFUSED = frozenset({Action.QUIT})


def _as_brightness(payload: str) -> float | None:
    """Home Assistant's 0-255 brightness as a fraction, or None if not a number."""
    try:
        value = float(payload.strip())
    except (AttributeError, ValueError):
        return None
    return max(0.0, min(1.0, value / 255.0))


class MqttBridge:
    def __init__(self, app, config: MqttConfig):
        self.app = app
        self.config = config
        try:
            import aiomqtt  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                f"MQTT support needs aiomqtt ({install_hint('mqtt')})"
            ) from exc
        self.prefix = f"{config.topic_prefix}/{config.device_id}"
        self._client = None
        self._last_payload: str | None = None
        #: What the published picture is of.  The picture is only re-rendered
        #: and re-sent when this changes, so a state update every second does
        #: not put a megabyte a minute through the broker.
        self._image_key: tuple | None = None

    # -- topics ------------------------------------------------------------
    @property
    def state_topic(self) -> str:
        return f"{self.prefix}/state"

    @property
    def availability_topic(self) -> str:
        return f"{self.prefix}/availability"

    @property
    def image_topic(self) -> str:
        return f"{self.prefix}/image"

    @property
    def command_topic(self) -> str:
        return f"{self.prefix}/cmd"

    def entity_topic(self, name: str) -> str:
        return f"{self.prefix}/{name}/set"

    def subscriptions(self) -> list[str]:
        """The only topics the frame listens on.

        Two narrow filters rather than one ``<prefix>/#``.  The wildcard meant
        the bridge acted on anything published anywhere under its own prefix --
        including the topics it publishes itself -- so any client on the broker
        could reconfigure or stop the frame simply by inventing a topic name.
        These two are exactly what the announced entities publish to, and a
        test holds them against the discovery payloads.
        """
        return [self.command_topic, f"{self.prefix}/+/set"]

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
                    self._image_key = None
                    _log.info("connected to MQTT broker %s:%d",
                              self.config.host, self.config.port)
                    delay = 2.0
                    await client.publish(self.availability_topic, ONLINE, qos=1, retain=True)
                    await self._announce(client)
                    for topic in self.subscriptions():
                        await client.subscribe(topic, qos=1)
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
        state = self.app.state().as_dict()
        payload = json.dumps(state, default=str)
        if force or payload != self._last_payload:
            self._last_payload = payload
            try:
                await client.publish(self.state_topic, payload, qos=0, retain=True)
            except Exception as exc:  # pragma: no cover
                _log.debug("state publish failed: %s", exc)
        await self._publish_image(client, state.get("current") or {})

    async def _publish_image(self, client, current: dict[str, Any]) -> None:
        """Send the picture on the frame, when it is a different picture.

        Retained, so Home Assistant has something to show the moment it
        restarts rather than an empty card until the frame next moves on.  The
        photograph itself is sent rather than a screenshot: it can be rendered
        while the display is off, it costs the render loop nothing, and it is
        what a dashboard is actually asking for.
        """
        if not self.config.publish_image:
            return
        # Imported here rather than at the top: it reaches Pillow through the
        # media package, and the bridge itself has no other use for either.
        from ..media.preview import render_preview

        path = current.get("path")
        if not path:
            return
        key = (current.get("id"), path, current.get("mtime"))
        if key == self._image_key:
            return
        # Claimed before the render rather than after: a slow picture would
        # otherwise be started again by every state update in the meantime.
        self._image_key = key
        edge = max(64, int(self.config.image_width))
        data = await asyncio.get_running_loop().run_in_executor(
            None, render_preview, path, bool(current.get("is_video")),
            (edge, edge), int(self.config.image_quality),
        )
        if data is None:
            _log.debug("no picture could be rendered for %s", path)
            return
        try:
            await client.publish(self.image_topic, data, qos=0, retain=True)
        except Exception as exc:  # pragma: no cover
            _log.debug("image publish failed: %s", exc)
            self._image_key = None

    # -- inbound -----------------------------------------------------------
    async def _listen(self, client) -> None:
        async for message in client.messages:
            topic = str(message.topic)
            # The frame is subscribed to its own prefix, so it hears everything
            # it publishes.  ``/image`` is skipped before the payload is even
            # decoded: it is a JPEG, and turning a megabyte of it into a string
            # on every picture change would be pure waste.
            if topic.endswith(("/state", "/availability", "/image")):
                continue
            payload = message.payload.decode("utf-8", "replace").strip()
            try:
                await self._dispatch(topic, payload)
            except Exception:
                _log.exception("failed to handle MQTT message on %s", topic)

    def _submit(self, command: Command | None) -> bool:
        """Hand a command to the frame, unless the broker may not give it.

        Anything that can reach the broker can reach this, and a broker is not
        an authenticated channel in most houses -- so the two commands that are
        not about showing photographs are checked here rather than trusted.
        """
        if command is None:
            return False
        if command.action in MQTT_REFUSED:
            _log.warning("refusing %s over MQTT: it is not a command this "
                         "surface accepts", command.action.value)
            return False
        if command.action is Action.DELETE and not self.app.config.http.allow_delete:
            _log.warning("refusing a delete over MQTT: http.allow_delete is off. "
                         "The frame's own buttons can still remove a picture.")
            return False
        if command.action is Action.SET_CONFIG:
            from ..uischema import check_path_setting

            problem = check_path_setting(str(command.payload.get("key") or ""),
                                         command.payload.get("value"),
                                         self.app.config)
            if problem:
                _log.warning("refusing a setting over MQTT: %s", problem)
                return False
        return self.app.bus.submit(command)

    async def _dispatch(self, topic: str, payload: str) -> None:
        leaf = topic[len(self.prefix):].strip("/")
        if leaf in ("cmd", ""):
            command = Command.parse(payload, source="mqtt")
            if command is None:
                _log.warning("ignoring unknown MQTT command %r", payload[:60])
                return
            self._submit(command)
            return
        name = leaf.rsplit("/", 1)[0] if leaf.endswith("/set") else leaf
        low = payload.lower()

        if name == "display":
            # Home Assistant's template light sends the *rendered*
            # `command_on_template` here, which for a slider drag is the
            # brightness and nothing else.  Treating a number as "not the word
            # on, therefore off" is what made the slider turn the screen off.
            #
            # `0` and `1` are the exception: they are what a hand-written
            # automation has always published here to mean off and on, and
            # reading `0` as "brightness zero, screen on" left the frame black
            # while Home Assistant showed the light as lit -- and, because the
            # brightness is a saved setting, wrote that zero into the config.
            if low in ("on", "off", "true", "false", "0", "1"):
                self._submit(Command(
                    Action.DISPLAY_ON if low in ("on", "true", "1")
                    else Action.DISPLAY_OFF, source="mqtt"))
                return
            level = _as_brightness(payload)
            if level is not None:
                self._submit(Command(Action.DISPLAY_ON, source="mqtt"))
                self._submit(Command(Action.BRIGHTNESS, {"value": level}, source="mqtt"))
            else:
                self._submit(Command(
                    Action.DISPLAY_ON if low in ("on", "true") else Action.DISPLAY_OFF,
                    source="mqtt"))
        elif name == "brightness":
            # Kept for anyone publishing by hand; the light entity itself now
            # sends its brightness on the display topic, as its schema says.
            level = _as_brightness(payload)
            if level is not None:
                self._submit(Command(Action.BRIGHTNESS, {"value": level}, source="mqtt"))
        elif name == "pause":
            self._submit(Command(
                Action.PAUSE if low in ("on", "true", "1") else Action.RESUME,
                source="mqtt"))
        elif name in ("next", "previous", "rescan", "delete", "restart"):
            # Every one of these is an announced button.  `restart` was missing
            # here while being announced, so Home Assistant showed a Restart
            # button that did nothing at all and logged "unhandled topic".
            self._submit(Command(Action(name), source="mqtt"))
        elif name == "interval":
            self._submit(Command(
                Action.SET_CONFIG, {"key": "slideshow.interval", "value": payload},
                source="mqtt"))
        elif name == "transition":
            self._submit(Command(
                Action.SET_CONFIG, {"key": "slideshow.transition", "value": payload},
                source="mqtt"))
        elif name == "order":
            self._submit(Command(
                Action.SET_CONFIG, {"key": "slideshow.order", "value": payload},
                source="mqtt"))
        elif name == "subfolder":
            self._submit(Command(
                Action.SET_CONFIG, {"key": "library.subfolder", "value": payload},
                source="mqtt"))
        elif name in self.FILTER_TOPICS:
            # One box per filter, and each one sends only itself: the frame
            # merges it into the filter already in force rather than replacing
            # it, so typing a place name does not wipe the date range.
            field = self.FILTER_TOPICS[name]
            value: Any = payload
            if field == "tags_match_all":
                value = low in ("on", "true", "1")
            self._submit(Command(Action.SET_FILTERS, {field: value}, source="mqtt"))
        elif name == "clear_filters":
            self._submit(Command(Action.SET_FILTERS, {"reset": True}, source="mqtt"))
        elif name.startswith("dates_"):
            # One button per rolling window.  A button rather than a select
            # because that is how these get used -- one tap on a dashboard --
            # and what it sends is the rule ("the last 7 days"), never the two
            # dates it happens to resolve to today.
            window = name[len("dates_"):]
            self._submit(Command(Action.SET_FILTERS, {"date_window": window},
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

    def _folder_options(self) -> list[str]:
        """The folders the dropdown offers, as the frame sees them.

        Fixed when discovery is published, which is why a folder that appears
        later shows up as "(other)" until the frame restarts -- better than a
        select that Home Assistant logs a warning about on every update.
        """
        try:
            return self.app.folder_choices()
        except Exception:      # pragma: no cover - a frame with no index yet
            return []

    def _discovery_topic(self, component: str, object_id: str) -> str:
        return (f"{self.config.discovery_prefix}/{component}/"
                f"picframe3_{self.config.device_id}/{object_id}/config")

    async def _announce(self, client) -> None:
        entities = self.discovery_entities()
        for component, object_id, payload in entities:
            clean = {k: v for k, v in payload.items() if v is not None}
            await client.publish(self._discovery_topic(component, object_id),
                                 json.dumps(clean), qos=1, retain=True)
        for component, object_id in self.retired_entities():
            # An empty retained payload is how Home Assistant is told an entity
            # has gone; anything else and it stays for ever, unavailable.
            await client.publish(self._discovery_topic(component, object_id),
                                 "", qos=1, retain=True)
        _log.info("published Home Assistant discovery for %d entities", len(entities))

    def retired_entities(self) -> list[tuple[str, str]]:
        """Entities a previous run may have announced that this one does not."""
        announced = {(component, object_id)
                     for component, object_id, _ in self.discovery_entities()}
        optional = self.HEALTH_ENTITIES + self.NETWORK_ENTITIES
        return [entity for entity in optional if entity not in announced]

    #: Which filter each ``.../<name>/set`` topic writes to.  The frame's own
    #: vocabulary is on the right; what Home Assistant calls the entity is on
    #: the left, and the two are allowed to differ.
    #: The three that share a name with a sensor about the picture on screen
    #: carry a ``_filter`` suffix, because two entities of the same device may
    #: not share an object id -- "tags" is already what this photograph is
    #: tagged with.  The bare names are accepted too, for anyone sending MQTT
    #: by hand.
    FILTER_TOPICS = {
        "folder_filter": "subfolder",
        "folder": "subfolder",
        "tags_filter": "tags",
        "tags": "tags",
        "tags_match_all": "tags_match_all",
        "location_filter": "location",
        "location": "location",
        "date_from": "date_from",
        "date_to": "date_to",
    }

    #: The diagnostic entities that exist only while health reporting is on.
    #: Listed here as well so switching the reporting off can take them out of
    #: Home Assistant again -- a retained discovery message nobody withdraws
    #: leaves five sensors sitting there for ever, permanently unavailable.
    HEALTH_ENTITIES = (("sensor", "cpu_temp"), ("sensor", "cpu_load"),
                       ("sensor", "memory"), ("sensor", "disk_free"),
                       ("binary_sensor", "undervoltage"))

    #: The two entities that exist only while the network watcher is on.
    NETWORK_ENTITIES = (("binary_sensor", "network"), ("sensor", "network_repairs"))

    def _network_entities(self, base: dict[str, Any], uid: str
                          ) -> list[tuple[str, str, dict[str, Any]]]:
        """Whether the frame can reach the house, and how often it has mended itself.

        Both are diagnostics rather than something to look at every day.  The
        connectivity sensor is the honest one: it can only ever be *off* in
        Home Assistant retrospectively, because a frame that cannot reach the
        gateway cannot reach the broker either.  What it is really for is the
        moment afterwards -- the frame comes back, says it was away, and the
        repair counter says whether it needed help getting there.
        """
        if not self.app.config.network.enabled:
            return []
        return [
            ("binary_sensor", "network", {
                **base,
                "name": "Network",
                "unique_id": f"picframe3_{uid}_network",
                # Empty, not OFF, until the first check has run: an appliance
                # that announces itself as disconnected while it is starting up
                # writes a false outage into the history of every frame.
                "value_template":
                    "{% set v = value_json.network.online | default(none) %}"
                    "{{ '' if v is none else ('ON' if v else 'OFF') }}",
                "payload_on": "ON", "payload_off": "OFF",
                "device_class": "connectivity",
                "json_attributes_topic": self.state_topic,
                "json_attributes_template":
                    "{{ value_json.network | default({}, true) | tojson }}",
                "entity_category": "diagnostic",
            }),
            ("sensor", "network_repairs", {
                **base,
                "name": "Network repairs",
                "unique_id": f"picframe3_{uid}_network_repairs",
                "value_template":
                    "{{ value_json.network.repairs | default(0, true) }}",
                "state_class": "total_increasing",
                "icon": "mdi:wifi-sync",
                "entity_category": "diagnostic",
            }),
        ]

    def _health_entities(self, base: dict[str, Any], uid: str
                         ) -> list[tuple[str, str, dict[str, Any]]]:
        """Temperature, load, memory, free space and the power supply.

        Every one of them reads a flattened field of ``health`` rather than
        reaching through two levels, and every one is guarded: on a machine
        that cannot take a reading the field is ``null``, and a guarded
        template leaves the sensor unknown instead of pushing the string
        "None" into a numeric entity.
        """
        if not self.app.config.health.enabled:
            return []

        def guarded(field: str, expression: str | None = None) -> str:
            # Falls back to an empty string, not to None: Jinja renders None as
            # the *word* "None", which a numeric sensor rejects, while an empty
            # payload is the one thing Home Assistant is documented to ignore --
            # so a reading that briefly cannot be taken leaves the last known
            # value standing instead of blanking the entity.
            value = f"value_json.health.{field}"
            return (f"{{{{ {expression or value} if {value} is not none "
                    f"else '' }}}}")

        return [
            ("sensor", "cpu_temp", {
                **base,
                "name": "CPU temperature",
                "unique_id": f"picframe3_{uid}_cpu_temp",
                "value_template": guarded("cpu_temp"),
                "device_class": "temperature",
                "unit_of_measurement": "°C",
                "state_class": "measurement",
                "suggested_display_precision": 1,
                "entity_category": "diagnostic",
            }),
            ("sensor", "cpu_load", {
                **base,
                "name": "CPU load",
                "unique_id": f"picframe3_{uid}_cpu_load",
                # One minute of run queue over the number of cores: a load of
                # 4 on the Pi 5's four cores is 100 %, not 400 %.
                "value_template": guarded("cpu_percent"),
                "unit_of_measurement": "%",
                "state_class": "measurement",
                "json_attributes_topic": self.state_topic,
                "json_attributes_template":
                    "{{ value_json.health.load | default({}, true) | tojson }}",
                "icon": "mdi:cpu-64-bit",
                "entity_category": "diagnostic",
            }),
            ("sensor", "memory", {
                **base,
                "name": "Memory used",
                "unique_id": f"picframe3_{uid}_memory",
                "value_template": guarded("memory_percent"),
                "unit_of_measurement": "%",
                "state_class": "measurement",
                "json_attributes_topic": self.state_topic,
                "json_attributes_template":
                    "{{ value_json.health.memory | default({}, true) | tojson }}",
                "icon": "mdi:memory",
                "entity_category": "diagnostic",
            }),
            ("sensor", "disk_free", {
                **base,
                "name": "Free space",
                "unique_id": f"picframe3_{uid}_disk_free",
                # The disk the photographs are on, which is not necessarily
                # the root filesystem -- and the one that fills up.
                "value_template": guarded(
                    "disk_free", "(value_json.health.disk_free / 1073741824) | round(1)"),
                "device_class": "data_size",
                "unit_of_measurement": "GiB",
                "state_class": "measurement",
                "json_attributes_topic": self.state_topic,
                "json_attributes_template":
                    "{{ value_json.health.disk | default({}, true) | tojson }}",
                "icon": "mdi:harddisk",
                "entity_category": "diagnostic",
            }),
            ("binary_sensor", "undervoltage", {
                **base,
                "name": "Power supply",
                "unique_id": f"picframe3_{uid}_undervoltage",
                # On for a brownout happening now *and* for one that happened
                # while nobody was watching: the flag the firmware keeps until
                # the next reboot is the only trace of a 3am dip, and a frame
                # that dips is a frame that will corrupt its card eventually.
                "value_template":
                    "{{ 'ON' if (value_json.health.undervoltage "
                    "or value_json.health.undervoltage_since_boot) else 'OFF' }}",
                "payload_on": "ON", "payload_off": "OFF",
                "device_class": "problem",
                "json_attributes_topic": self.state_topic,
                "json_attributes_template":
                    "{{ value_json.health.power | default({}, true) | tojson }}",
                "entity_category": "diagnostic",
            }),
        ]

    def discovery_entities(self) -> list[tuple[str, str, dict[str, Any]]]:
        """Every entity the frame offers Home Assistant.

        Built as data rather than published inline so it can be checked -- that
        the unique ids really are unique, and that no template reads a field
        the frame does not actually publish.
        """
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
                # Home Assistant renders this and publishes the result.  The
                # constant "on" it used to be threw the brightness away on
                # every slider drag, so the slider in the dashboard moved and
                # the frame did not: `brightness` is only defined when the
                # slider is what was touched, hence the guard.
                "command_on_template":
                    "{% if brightness is defined %}{{ brightness }}"
                    "{% else %}on{% endif %}",
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
            # -- what is on the screen, broken out ------------------------
            # The whole record already rides along as attributes of "Current
            # picture", but attributes cannot be put on a dashboard, used in a
            # condition or spoken by a TTS automation without templating.  One
            # entity per fact is what makes those things one click each.
            ("image", "picture", {
                "device": device,
                "availability_topic": self.availability_topic,
                "qos": 1,
                "name": "Picture",
                "unique_id": f"picframe3_{uid}_picture",
                "image_topic": self.image_topic,
                "content_type": "image/jpeg",
                "json_attributes_topic": self.state_topic,
                "json_attributes_template": "{{ value_json.current | tojson }}",
                "icon": "mdi:image-frame",
            }),
            ("sensor", "title", {
                **base,
                "name": "Title",
                "unique_id": f"picframe3_{uid}_title",
                "value_template":
                    "{{ value_json.current.title or value_json.current.caption "
                    "or value_json.current.basename | default('') }}",
                "icon": "mdi:format-title",
            }),
            ("sensor", "taken", {
                **base,
                "name": "Taken",
                "unique_id": f"picframe3_{uid}_taken",
                # Pre-formatted as ISO 8601 with an offset by the frame: a bare
                # Unix timestamp is not accepted by a timestamp sensor, and
                # templating one in Jinja loses the local zone.
                "value_template":
                    "{{ value_json.current.taken_iso if value_json.current.taken_iso "
                    "else None }}",
                "device_class": "timestamp",
                "icon": "mdi:calendar-clock",
            }),
            ("sensor", "place", {
                **base,
                "name": "Place",
                "unique_id": f"picframe3_{uid}_place",
                # Latitude and longitude come along as attributes, which is
                # what a map card reads.
                "value_template": "{{ value_json.current.location | default('') }}",
                "json_attributes_topic": self.state_topic,
                "json_attributes_template":
                    "{{ {'latitude': value_json.current.latitude, "
                    "'longitude': value_json.current.longitude, "
                    "'source_type': 'gps'} | tojson }}",
                "icon": "mdi:map-marker",
            }),
            ("sensor", "tags", {
                **base,
                "name": "Tags",
                "unique_id": f"picframe3_{uid}_tags",
                "value_template": "{{ value_json.current.tags_text | default('') }}",
                "json_attributes_topic": self.state_topic,
                "json_attributes_template":
                    "{{ {'tags': value_json.current.tags | default([])} | tojson }}",
                "icon": "mdi:tag-multiple",
            }),
            ("sensor", "camera", {
                **base,
                "name": "Camera",
                "unique_id": f"picframe3_{uid}_camera",
                "value_template": "{{ value_json.current.model | default('') }}",
                "json_attributes_topic": self.state_topic,
                "json_attributes_template":
                    "{{ {'make': value_json.current.make, "
                    "'lens': value_json.current.lens, "
                    "'f_number': value_json.current.f_number, "
                    "'exposure_time': value_json.current.exposure_time, "
                    "'iso': value_json.current.iso, "
                    "'focal_length': value_json.current.focal_length} | tojson }}",
                "icon": "mdi:camera",
            }),
            ("sensor", "folder", {
                **base,
                "name": "Folder",
                "unique_id": f"picframe3_{uid}_folder",
                "value_template":
                    "{{ (value_json.current.folder | default('')).split('/') | last }}",
                "icon": "mdi:folder-image",
            }),
            ("sensor", "shown_as", {
                **base,
                "name": "Laid out as",
                "unique_id": f"picframe3_{uid}_shown_as",
                "value_template": "{{ value_json.current.shown_as | default('') }}",
                "icon": "mdi:crop",
                "entity_category": "diagnostic",
            }),
            # -- how evenly the library is being shown --------------------
            ("sensor", "pictures", {
                **base,
                "name": "Pictures indexed",
                "unique_id": f"picframe3_{uid}_pictures",
                "value_template": "{{ value_json.library.files | default(0) }}",
                "state_class": "measurement",
                "icon": "mdi:image-multiple",
            }),
            ("sensor", "round", {
                **base,
                "name": "Shuffle round",
                "unique_id": f"picframe3_{uid}_round",
                "value_template": "{{ value_json.playlist_round | default(1) }}",
                "state_class": "total_increasing",
                "json_attributes_topic": self.state_topic,
                "json_attributes_template":
                    "{{ {'remaining': value_json.playlist_remaining, "
                    "'times_shown_min': value_json.library.shown_min, "
                    "'times_shown_max': value_json.library.shown_max, "
                    "'never_shown': value_json.library.never_shown} | tojson }}",
                "icon": "mdi:shuffle-variant",
                "entity_category": "diagnostic",
            }),
            ("binary_sensor", "restart_required", {
                **base,
                "name": "Restart needed",
                "unique_id": f"picframe3_{uid}_restart_required",
                # Some settings — the broker, the HTTP port, which folders are
                # indexed — only a fresh process can pick up.  Saying so out
                # loud beats leaving someone to wonder why nothing changed.
                "value_template":
                    "{{ 'ON' if value_json.restart_required else 'OFF' }}",
                "payload_on": "ON", "payload_off": "OFF",
                "device_class": "problem",
                "json_attributes_topic": self.state_topic,
                "json_attributes_template":
                    "{{ {'settings': value_json.restart_required, "
                    "'unsaved': value_json.unsaved_changes} | tojson }}",
                "entity_category": "diagnostic",
            }),
            ("sensor", "remaining", {
                **base,
                "name": "Left in this round",
                "unique_id": f"picframe3_{uid}_remaining",
                "value_template": "{{ value_json.playlist_remaining | default(0) }}",
                "state_class": "measurement",
                "icon": "mdi:counter",
                "entity_category": "diagnostic",
            }),
            # -- the Pi itself --------------------------------------------
            # Heat and a marginal power supply are what actually kill a frame
            # on a wall, and both are invisible from the picture on screen.
            *self._health_entities(base, uid),
            *self._network_entities(base, uid),
            # -- what has been taken out of the library ------------------
            # Replaces the external watchdog script the pi3d frame needed to
            # count a folder: the frame already knows, so it says so itself.
            ("sensor", "removed", {
                **base,
                "name": "Pictures removed",
                "unique_id": f"picframe3_{uid}_removed",
                "value_template": "{{ value_json.removed.count | default(0) }}",
                "state_class": "measurement",
                "json_attributes_topic": self.state_topic,
                "json_attributes_template":
                    "{{ {'last': value_json.removed.last_basename, "
                    "'last_removed': value_json.removed.last_removed_iso, "
                    "'came_from': value_json.removed.last_folder, "
                    "'folder': value_json.removed.folder, "
                    "'journal': value_json.removed.journal} | tojson }}",
                "icon": "mdi:image-off",
            }),
            ("sensor", "last_removed", {
                **base,
                "name": "Last removed",
                "unique_id": f"picframe3_{uid}_last_removed",
                "value_template":
                    "{{ value_json.removed.last_basename | default('', true) }}",
                "icon": "mdi:image-remove",
                "entity_category": "diagnostic",
            }),
            # -- which pictures are in the running ------------------------
            # picframe's Home Assistant card had these four boxes and they are
            # the reason people put the frame in Home Assistant at all: "only
            # the holiday pictures", "only this Christmas", said from the
            # sofa.  Each one sends just itself; the frame merges.
            ("select", "folder_filter", {
                **base,
                "name": "Folder",
                "unique_id": f"picframe3_{uid}_folder_filter",
                "command_topic": self.entity_topic("folder_filter"),
                # folder_choice, not subfolder: a select warns on every state
                # it has no option for, and the folder can also be set to a
                # free-text fragment from the settings page.
                "value_template":
                    "{{ value_json.filters.folder_choice | default('', true) or '"
                    + ANYTHING + "' }}",
                "options": [ANYTHING, ANY_OTHER, *self._folder_options()],
                "icon": "mdi:folder-multiple-image",
            }),
            ("text", "tags_filter", {
                **base,
                "name": "Tags filter",
                "unique_id": f"picframe3_{uid}_tags_filter",
                "command_topic": self.entity_topic("tags_filter"),
                "value_template": "{{ value_json.filters.tags_text | default('', true) }}",
                "max": 255,
                "icon": "mdi:tag-search",
            }),
            ("switch", "tags_match_all", {
                **base,
                "name": "Match all tags",
                "unique_id": f"picframe3_{uid}_tags_match_all",
                "command_topic": self.entity_topic("tags_match_all"),
                "value_template":
                    "{{ 'ON' if value_json.filters.tags_match_all else 'OFF' }}",
                "payload_on": "on", "payload_off": "off",
                "state_on": "ON", "state_off": "OFF",
                "icon": "mdi:set-center",
                "entity_category": "config",
            }),
            ("text", "location_filter", {
                **base,
                "name": "Place filter",
                "unique_id": f"picframe3_{uid}_location_filter",
                "command_topic": self.entity_topic("location_filter"),
                "value_template":
                    "{{ value_json.filters.location_contains | default('', true) }}",
                "max": 255,
                "icon": "mdi:map-search",
            }),
            ("text", "date_from", {
                **base,
                "name": "Pictures from",
                "unique_id": f"picframe3_{uid}_date_from",
                "command_topic": self.entity_topic("date_from"),
                "value_template":
                    "{{ value_json.filters.date_from_text | default('', true) }}",
                # An empty box means no limit, which is why the pattern lets
                # the whole thing be empty rather than demanding a date.
                "pattern": r"^(\d{4}-\d{2}-\d{2})?$",
                "max": 10,
                "icon": "mdi:calendar-start",
            }),
            ("text", "date_to", {
                **base,
                "name": "Pictures until",
                "unique_id": f"picframe3_{uid}_date_to",
                "command_topic": self.entity_topic("date_to"),
                "value_template":
                    "{{ value_json.filters.date_to_text | default('', true) }}",
                "pattern": r"^(\d{4}-\d{2}-\d{2})?$",
                "max": 10,
                "icon": "mdi:calendar-end",
            }),
            ("sensor", "selected", {
                **base,
                "name": "Selected pictures",
                "unique_id": f"picframe3_{uid}_selected",
                "value_template": "{{ value_json.playlist_size | default(0) }}",
                "state_class": "measurement",
                # The whole filter rides along, so one card can show what the
                # count is a count of.
                "json_attributes_topic": self.state_topic,
                "json_attributes_template": "{{ value_json.filters | tojson }}",
                "icon": "mdi:image-filter-center-focus",
            }),
            ("binary_sensor", "filtered", {
                **base,
                "name": "Filter active",
                "unique_id": f"picframe3_{uid}_filtered",
                "value_template":
                    "{{ 'ON' if value_json.filters.active else 'OFF' }}",
                "payload_on": "ON", "payload_off": "OFF",
                "icon": "mdi:filter-check",
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
            ("restart", "Restart the frame", "mdi:restart"),
            ("delete", "Remove current picture", "mdi:delete"),
            ("clear_filters", "Show everything again", "mdi:filter-remove"),
        ) + tuple(
            # The date windows, as buttons.  A select entity would have to hold
            # a state, and this filter has no state worth holding: it is a rule
            # the frame re-resolves every day, so "press it again" is the whole
            # interaction.
            (f"dates_{name}", label, DATE_WINDOW_ICONS.get(name, "mdi:calendar"))
            for name, label in DATE_WINDOWS.items()
        ):
            entities.append(("button", action, {
                "device": device,
                "availability_topic": self.availability_topic,
                "name": label,
                "unique_id": f"picframe3_{uid}_{action}",
                "command_topic": self.entity_topic(action),
                "payload_press": "press",
                "icon": icon,
                "entity_category": "config" if action in ("rescan", "restart") else None,
            }))

        return entities
