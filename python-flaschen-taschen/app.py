#!/usr/bin/env python3

# read README.md for pre-reqs, and customize config.yaml

import io
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import paho.mqtt.client as mqtt
from yaml import safe_load
from PIL import Image, ImageDraw

import flaschen
from spotify_canvas import SpotifyCanvasClient, SpotifyCanvasError


volume_clear_timer = None
VOLUME_BAR_TIMEOUT = 2
VOLUME_BAR = True
volume_overlay_until = 0.0
volume_overlay_percent = None

display_clear_timer = None
DISPLAY_CLEAR_TIMEOUT = 20
DISPLAY_LOCK = threading.Lock()

mypath = Path(__file__).resolve().parent
default_image_file = mypath / ".." / "python-flask-socketio-server" / "static" / "img" / "default-inverted.png"
print(f"Using default cover image file {default_image_file}")

config_file = mypath / "config.yaml"
print(f"Using config file {config_file}")
with config_file.open() as f:
    config = safe_load(f)

MQTT_CONF = config["mqtt"]
TOPIC_ROOT = MQTT_CONF["topic"]
print(TOPIC_ROOT)

FLASCHEN_CONF = config["flaschen"]
FLASCHEN_SERVER = FLASCHEN_CONF.get("server", "localhost")
FLASCHEN_PORT = FLASCHEN_CONF.get("port", 1337)
FLASCHEN_ROWS = FLASCHEN_CONF.get("led-rows", 32)
FLASCHEN_COLS = FLASCHEN_CONF.get("led-columns", 32)
FLASCHEN_SIZE = (FLASCHEN_COLS, FLASCHEN_ROWS)

CANVAS_CONF = config.get("canvas", {})
CANVAS_MODE = str(CANVAS_CONF.get("mode", "cover")).lower()
if CANVAS_MODE not in ("cover", "canvas"):
    raise ValueError("canvas.mode must be 'cover' or 'canvas'")
CANVAS_COOKIE_ENV = CANVAS_CONF.get("cookie-env", "SPOTIFY_SP_DC")
CANVAS_COOKIE = os.environ.get(CANVAS_COOKIE_ENV) or CANVAS_CONF.get("sp-dc")
CANVAS_FPS = max(1.0, min(float(CANVAS_CONF.get("fps", 8)), 20.0))
CANVAS_FIT = str(CANVAS_CONF.get("fit", "cover")).lower()
CANVAS_LOOKUP_DELAY = max(0.0, float(CANVAS_CONF.get("lookup-delay", 1.0)))
CANVAS_MAX_BYTES = int(float(CANVAS_CONF.get("max-download-mb", 20)) * 1024 * 1024)

flaschen_client = None
DEFAULT_IMAGE = None
SAVED_INFO = {}

known_play_metadata_types = {
    "songalbum": "songalbum",
    "volume": "volume",
    "client_ip": "client_ip",
    "active_start": "active_start",
    "active_end": "active_end",
    "play_start": "play_start",
    "play_end": "play_end",
    "play_flush": "play_flush",
    "play_resume": "play_resume",
}
known_core_metadata_types = {
    "artist": "showArtist",
    "album": "showAlbum",
    "title": "showTitle",
    "genre": "showGenre",
    "cover": "showCoverArt",
}
known_remote_commands = [
    "command", "beginff", "beginrew", "mutetoggle", "nextitem", "previtem",
    "pause", "playpause", "play", "stop", "playresume", "shuffle_songs",
    "volumedown", "volumeup",
]


def _form_subtopic_topic(subtopic):
    return TOPIC_ROOT + "/" + subtopic


def flaschenSendThumbnailImage(client, image):
    """Send one complete frame without interleaving concurrent writers."""
    with DISPLAY_LOCK:
        for x in range(FLASCHEN_COLS):
            for y in range(FLASCHEN_ROWS):
                color = image.getpixel((x, y))
                client.set(x, y, (color[0], color[1], color[2]))
        client.send()


def overlay_volume_bar(image, volume: int):
    draw = ImageDraw.Draw(image)
    width, height = image.size
    bar_height = 3
    default_corner_radius = 5
    padding = 3
    top = height - padding - bar_height
    bottom = height - padding - 1
    left = padding
    right = width - padding
    draw.rounded_rectangle(
        [left, top, right, bottom], radius=default_corner_radius,
        fill=(32, 32, 32, 255),
    )
    bar_width = int((width - 2 * padding) * (volume / 100))
    if bar_width > 0:
        bar_right = left + bar_width
        corner_radius = min(default_corner_radius, bar_height // 2, bar_width // 2)
        draw.rounded_rectangle(
            [left, top, bar_right, bottom], radius=corner_radius,
            fill=(238, 238, 238, 255),
        )
    return image


def display_image(image):
    """Remember a clean frame and display it with any active volume overlay."""
    SAVED_INFO["display_frame"] = image
    if volume_overlay_percent is not None and time.monotonic() < volume_overlay_until:
        image = overlay_volume_bar(image.copy(), volume_overlay_percent)
    flaschenSendThumbnailImage(flaschen_client, image)


def clear_volume_bar():
    global volume_overlay_percent
    volume_overlay_percent = None
    image = SAVED_INFO.get("display_frame") or SAVED_INFO.get("cover_art", {}).get("data")
    if image:
        flaschenSendThumbnailImage(flaschen_client, image)


def clear_display():
    canvas_controller.stop()
    image = Image.new("RGBA", FLASCHEN_SIZE, (0, 0, 0, 0))
    flaschenSendThumbnailImage(flaschen_client, image)


def createMatrixImage(fileobj):
    with Image.open(fileobj) as image:
        size = FLASCHEN_SIZE
        if hasattr(fileobj, "name"):
            print(fileobj.name, end=" ")
        print(image.format, f"{image.size} x {image.mode}")
        image.thumbnail(size, Image.LANCZOS)
        background = Image.new("RGBA", size, (0, 0, 0, 0))
        background.paste(
            image,
            (int((size[0] - image.size[0]) / 2), int((size[1] - image.size[1]) / 2)),
        )
        return background


class CanvasController:
    """Owns asynchronous Canvas lookup, download, and frame playback."""

    def __init__(self):
        self.enabled = CANVAS_MODE == "canvas" and bool(CANVAS_COOKIE)
        self.client = SpotifyCanvasClient(CANVAS_COOKIE) if self.enabled else None
        self.lock = threading.RLock()
        self.generation = 0
        self.stop_event = None
        self.process = None
        self.timer = None
        self.cache = {}
        if CANVAS_MODE == "canvas" and not CANVAS_COOKIE:
            print(f"Canvas requested but {CANVAS_COOKIE_ENV} is unset; using cover art")
        else:
            print(f"Display mode: {CANVAS_MODE}")

    def metadata_changed(self):
        if not self.enabled:
            return
        with self.lock:
            self.generation += 1
        self._stop_current()
        self.schedule()

    def cover_changed(self):
        if not self.enabled:
            return
        self._stop_current()
        self.schedule()

    def schedule(self):
        if not self.enabled:
            return
        title = SAVED_INFO.get("title")
        artist = SAVED_INFO.get("artist")
        if not title or not artist:
            return
        with self.lock:
            if self.timer:
                self.timer.cancel()
            generation = self.generation
            stop_event = threading.Event()
            if self.stop_event:
                self.stop_event.set()
            self.stop_event = stop_event
            self.timer = threading.Timer(
                CANVAS_LOOKUP_DELAY,
                self._lookup_and_play,
                args=(generation, stop_event, title, artist),
            )
            self.timer.daemon = True
            self.timer.start()

    def stop(self):
        with self.lock:
            self.generation += 1
        self._stop_current()

    def _stop_current(self):
        with self.lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None
            if self.stop_event:
                self.stop_event.set()
            process = self.process
            self.process = None
        if process and process.poll() is None:
            process.terminate()

    def _still_current(self, generation, stop_event):
        with self.lock:
            return generation == self.generation and self.stop_event is stop_event and not stop_event.is_set()

    def _download(self, url, stop_event):
        if urlparse(url).scheme != "https":
            raise SpotifyCanvasError("Canvas URL was not HTTPS")
        request = Request(url, headers={"User-Agent": "Spotify/9.0.34.593 iOS/18.4"})
        temp = tempfile.NamedTemporaryFile(prefix="spotify-canvas-", suffix=".mp4", delete=False)
        try:
            with temp, urlopen(request, timeout=15) as response:
                total = 0
                while not stop_event.is_set():
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > CANVAS_MAX_BYTES:
                        raise SpotifyCanvasError("Canvas download exceeded configured limit")
                    temp.write(chunk)
            if stop_event.is_set():
                os.unlink(temp.name)
                return None
            return temp.name
        except Exception:
            try:
                os.unlink(temp.name)
            except FileNotFoundError:
                pass
            raise

    def _lookup_and_play(self, generation, stop_event, title, artist):
        key = (title.casefold(), artist.casefold())
        try:
            if key in self.cache:
                track_uri, canvas_url = self.cache[key]
            else:
                track_uri, canvas_url = self.client.find_canvas(title, artist)
                self.cache[key] = (track_uri, canvas_url)
            if not self._still_current(generation, stop_event):
                return
            if not track_uri:
                print(f"Canvas: no close Spotify match for {artist} - {title}")
                return
            if not canvas_url:
                print(f"Canvas: track has no Canvas ({track_uri})")
                return
            print(f"Canvas: matched {artist} - {title} ({track_uri})")
            filename = self._download(canvas_url, stop_event)
            if filename and self._still_current(generation, stop_event):
                self._play(filename, generation, stop_event)
        except Exception as error:
            if not stop_event.is_set():
                print(f"Canvas unavailable; keeping cover art: {error}")

    def _play(self, filename, generation, stop_event):
        width, height = FLASCHEN_SIZE
        if CANVAS_FIT == "contain":
            video_filter = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,fps={CANVAS_FPS}"
            )
        else:
            video_filter = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},fps={CANVAS_FPS}"
            )
        command = [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-stream_loop", "-1",
            "-i", filename, "-an", "-vf", video_filter,
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        with self.lock:
            if not self._still_current(generation, stop_event):
                process.terminate()
                os.unlink(filename)
                return
            self.process = process
        frame_bytes = width * height * 3
        frame_interval = 1.0 / CANVAS_FPS
        next_frame = time.monotonic()
        try:
            while self._still_current(generation, stop_event):
                frame = process.stdout.read(frame_bytes)
                if len(frame) != frame_bytes:
                    break
                wait = next_frame - time.monotonic()
                if wait > 0 and stop_event.wait(wait):
                    break
                image = Image.frombytes("RGB", FLASCHEN_SIZE, frame).convert("RGBA")
                display_image(image)
                next_frame = max(next_frame + frame_interval, time.monotonic())
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            try:
                os.unlink(filename)
            except FileNotFoundError:
                pass
            with self.lock:
                if self.process is process:
                    self.process = None


canvas_controller = CanvasController()


def on_connect(client, userdata, flags, rc, properties=None):
    subtopic_list = list(known_core_metadata_types.keys())
    subtopic_list.extend(known_play_metadata_types.keys())
    for subtopic in subtopic_list:
        topic = _form_subtopic_topic(subtopic)
        print("topic", topic, end=" ")
        result, msg_id = client.subscribe(topic, 0)
        print(msg_id)


def _metadata_text(payload):
    value = payload.decode("utf-8", errors="replace").strip()
    return "" if value == "--" else value


def on_message(client, userdata, message, properties=None):
    global volume_clear_timer, display_clear_timer
    global volume_overlay_percent, volume_overlay_until
    topic = message.topic
    payload = message.payload

    if topic in (_form_subtopic_topic("artist"), _form_subtopic_topic("title")):
        name = topic.rsplit("/", 1)[-1]
        value = _metadata_text(payload)
        if SAVED_INFO.get(name) != value:
            SAVED_INFO[name] = value
            canvas_controller.metadata_changed()

    elif topic == _form_subtopic_topic("cover"):
        canvas_controller.cover_changed()
        if payload:
            try:
                image = createMatrixImage(io.BytesIO(payload))
            except Exception as error:
                print(f"Invalid cover art, using default: {error}")
                image = DEFAULT_IMAGE
        else:
            image = DEFAULT_IMAGE
        SAVED_INFO["cover_art"] = {"data": image}
        display_image(image)
        if display_clear_timer is not None:
            display_clear_timer.cancel()

    elif topic == _form_subtopic_topic("active_end"):
        canvas_controller.stop()
        if display_clear_timer is not None:
            display_clear_timer.cancel()
        display_clear_timer = threading.Timer(DISPLAY_CLEAR_TIMEOUT, clear_display)
        display_clear_timer.daemon = True
        display_clear_timer.start()

    elif topic == _form_subtopic_topic("volume") and VOLUME_BAR:
        try:
            channels = [float(x) for x in payload.decode("utf-8").split(",")]
        except ValueError:
            print("Invalid volume payload:", payload)
            return
        volume_db = channels[0]
        min_db, max_db = -30.0, 0.0
        volume_overlay_percent = max(
            0, min(100, int((volume_db - min_db) / (max_db - min_db) * 100))
        )
        volume_overlay_until = time.monotonic() + VOLUME_BAR_TIMEOUT
        image = SAVED_INFO.get("display_frame") or SAVED_INFO.get("cover_art", {}).get("data")
        if image:
            flaschenSendThumbnailImage(
                flaschen_client, overlay_volume_bar(image.copy(), volume_overlay_percent)
            )
        if volume_clear_timer is not None:
            volume_clear_timer.cancel()
        volume_clear_timer = threading.Timer(VOLUME_BAR_TIMEOUT, clear_volume_bar)
        volume_clear_timer.daemon = True
        volume_clear_timer.start()


DEFAULT_IMAGE = createMatrixImage(default_image_file)
print(DEFAULT_IMAGE)
flaschen_client = flaschen.Flaschen(
    FLASCHEN_SERVER, FLASCHEN_PORT, FLASCHEN_COLS, FLASCHEN_ROWS
)

mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
mqttc.on_connect = on_connect
mqttc.on_message = on_message

if MQTT_CONF.get("use_tls"):
    tls_conf = MQTT_CONF.get("tls")
    print("Using TLS config", tls_conf)
    if tls_conf:
        mqttc.tls_set(
            ca_certs=tls_conf["ca_certs_path"],
            certfile=tls_conf["certfile_path"],
            keyfile=tls_conf["keyfile_path"],
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLSv1_2,
            ciphers=None,
        )
        if tls_conf.get("allow_insecure_server_certificate", False):
            mqttc.tls_insecure_set(True)

if MQTT_CONF.get("username"):
    username = MQTT_CONF.get("username")
    print("MQTT username:", username)
    password = MQTT_CONF.get("password")
    mqttc.username_pw_set(username, password=password) if password else mqttc.username_pw_set(username)

if MQTT_CONF.get("logger"):
    print("Enabling MQTT logging")
    mqttc.enable_logger()

mqtt_host = MQTT_CONF["host"]
mqtt_port = MQTT_CONF["port"]
print("Connecting to broker", mqtt_host, "port", mqtt_port)
mqttc.connect(mqtt_host, port=mqtt_port)
mqttc.loop_start()

if __name__ == "__main__":
    while True:
        time.sleep(1)
