"""Control surfaces: web, MQTT, evdev input, GPIO buttons and power schedules.

Each module here is a client of :mod:`picframe3.events` and nothing more --
none of them reaches into the renderer or the playlist.
"""
