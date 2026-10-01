"""Unofficial Spotify Canvas lookup using an authenticated Spotify web session.

Spotify does not expose Canvas through its supported Web API. This module is
therefore deliberately isolated: callers can disable it and retain the normal
cover-art path if Spotify changes the undocumented endpoints.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import struct
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SECRETS_URL = (
    "https://raw.githubusercontent.com/xyloflake/spot-secrets-go/"
    "refs/heads/main/secrets/secretDict.json"
)
TOKEN_URL = "https://open.spotify.com/api/token"
SERVER_TIME_URL = "https://open.spotify.com/api/server-time"
SEARCH_URL = "https://api.spotify.com/v1/search"
PARTNER_SEARCH_URL = "https://api-partner.spotify.com/pathfinder/v2/query"
# Current web-player persisted query. Spotify may rotate this without notice.
PARTNER_SEARCH_HASH = "b50ebd72524415b132ddaca04158fd7aca529da28be322c9924643c0633df5bd"
CANVAS_URL = "https://spclient.wg.spotify.com/canvaz-cache/v0/canvases"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


class SpotifyCanvasError(RuntimeError):
    pass


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"\s*[\[(](feat\.?|ft\.?|with)\b.*?[\])]", "", value, flags=re.I)
    value = re.sub(
        r"\s*[-–—]\s*(\d{4}\s*)?(remaster(ed)?|radio edit|single version)\b.*$",
        "",
        value,
        flags=re.I,
    )
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _varint(value: int) -> bytes:
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise SpotifyCanvasError("Malformed protobuf varint")


def _protobuf_fields(data: bytes):
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        field_number, wire_type = key >> 3, key & 7
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 1:
            value = data[offset : offset + 8]
            offset += 8
        elif wire_type == 2:
            length, offset = _read_varint(data, offset)
            value = data[offset : offset + length]
            offset += length
        elif wire_type == 5:
            value = data[offset : offset + 4]
            offset += 4
        else:
            raise SpotifyCanvasError(f"Unsupported protobuf wire type {wire_type}")
        if offset > len(data):
            raise SpotifyCanvasError("Truncated protobuf response")
        yield field_number, wire_type, value


def _canvas_request(track_uri: str) -> bytes:
    encoded_uri = track_uri.encode("utf-8")
    track = b"\x0a" + _varint(len(encoded_uri)) + encoded_uri
    return b"\x0a" + _varint(len(track)) + track


def _canvas_urls(payload: bytes):
    for field, wire_type, canvas in _protobuf_fields(payload):
        if field != 1 or wire_type != 2:
            continue
        result = {}
        for nested_field, nested_wire_type, value in _protobuf_fields(canvas):
            if nested_wire_type == 2 and nested_field in (2, 5):
                result[nested_field] = value.decode("utf-8", errors="replace")
        if 2 in result:
            yield result.get(5), result[2]


class SpotifyCanvasClient:
    """Find a track and retrieve its Canvas URL.

    The sp_dc cookie is a credential. Keep it out of source control and logs.
    """

    def __init__(self, sp_dc: str, timeout: float = 10.0):
        if not sp_dc:
            raise ValueError("An sp_dc cookie is required")
        self.sp_dc = sp_dc
        self.timeout = timeout
        self._token = None
        self._token_expiry = 0.0
        self._totp_secret = None
        self._totp_version = None
        self._totp_refresh_at = 0.0

    def _request(self, url, *, data=None, headers=None):
        request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        request = Request(url, data=data, headers=request_headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.read(), response.headers
        except HTTPError as error:
            detail = error.read(300).decode("utf-8", errors="replace")
            raise SpotifyCanvasError(f"Spotify HTTP {error.code}: {detail}") from error
        except (URLError, TimeoutError) as error:
            raise SpotifyCanvasError(f"Spotify request failed: {error}") from error

    def _json(self, url, *, headers=None):
        body, _ = self._request(url, headers=headers)
        return json.loads(body)

    def _refresh_totp_secret(self):
        if self._totp_secret is not None and time.time() < self._totp_refresh_at:
            return
        secrets = self._json(SECRETS_URL)
        version = str(max(int(key) for key in secrets))
        transformed = "".join(
            str(value ^ ((index % 33) + 9))
            for index, value in enumerate(secrets[version])
        )
        self._totp_secret = transformed.encode("utf-8")
        self._totp_version = version
        self._totp_refresh_at = time.time() + 3600

    def _totp(self, timestamp_ms: int) -> str:
        counter = timestamp_ms // 1000 // 30
        digest = hmac.new(
            self._totp_secret, struct.pack(">Q", counter), hashlib.sha1
        ).digest()
        offset = digest[-1] & 0x0F
        code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
        return f"{code % 1_000_000:06d}"

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        self._refresh_totp_secret()
        cookie_headers = {
            "Cookie": f"sp_dc={self.sp_dc}",
            "Origin": "https://open.spotify.com",
            "Referer": "https://open.spotify.com/",
        }
        local_ms = int(time.time() * 1000)
        try:
            server_seconds = int(self._json(SERVER_TIME_URL, headers=cookie_headers)["serverTime"])
            server_ms = server_seconds * 1000
        except (KeyError, TypeError, ValueError, SpotifyCanvasError):
            server_ms = local_ms
        query = urlencode(
            {
                "reason": "init",
                "productType": "mobile-web-player",
                "totp": self._totp(local_ms),
                "totpVer": self._totp_version,
                "totpServer": self._totp(server_ms),
            }
        )
        token_data = self._json(f"{TOKEN_URL}?{query}", headers=cookie_headers)
        self._token = token_data.get("accessToken") or token_data.get("access_token")
        if not self._token:
            raise SpotifyCanvasError("Spotify did not return an access token")
        expiry_ms = token_data.get("accessTokenExpirationTimestampMs")
        self._token_expiry = (expiry_ms / 1000) if expiry_ms else time.time() + 300
        return self._token

    def _partner_tracks(self, title: str, artist: str):
        token = self._access_token()
        body = json.dumps(
            {
                "variables": {
                    "query": f"{title} {artist}",
                    "limit": 10,
                    "numberOfTopResults": 10,
                    "offset": 0,
                    "includeAuthors": False,
                    "includeAlbumPreReleases": True,
                    "includeEpisodeContentRatingsV2": False,
                },
                "operationName": "searchSuggestions",
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": PARTNER_SEARCH_HASH,
                    }
                },
            }
        ).encode("utf-8")
        payload, _ = self._request(
            PARTNER_SEARCH_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        result = json.loads(payload)
        if result.get("errors"):
            raise SpotifyCanvasError(f"Spotify partner search failed: {result['errors'][0]}")
        search = result.get("data", {}).get("searchV2", {})
        wrappers = [
            entry.get("item", entry)
            for entry in search.get("topResultsV2", {}).get("itemsV2", [])
        ]
        wrappers.extend(
            entry.get("item", entry)
            for entry in search.get("tracksV2", {}).get("items", [])
        )
        tracks = []
        for wrapper in wrappers:
            data = wrapper.get("data", wrapper)
            if data.get("__typename") != "Track":
                continue
            tracks.append(
                {
                    "name": data.get("name", ""),
                    "uri": data.get("uri"),
                    "artists": [
                        {"name": entry.get("profile", {}).get("name", "")}
                        for entry in data.get("artists", {}).get("items", [])
                    ],
                }
            )
        return tracks

    def _official_tracks(self, title: str, artist: str):
        token = self._access_token()
        query = urlencode(
            {"q": f'track:"{title}" artist:"{artist}"', "type": "track", "limit": 5}
        )
        result = self._json(
            f"{SEARCH_URL}?{query}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        return result.get("tracks", {}).get("items", [])

    def _search_tracks(self, title: str, artist: str):
        try:
            return self._partner_tracks(title, artist)
        except SpotifyCanvasError as partner_error:
            try:
                return self._official_tracks(title, artist)
            except SpotifyCanvasError:
                raise partner_error

    def find_track_uri(self, title: str, artist: str) -> str | None:
        expected_title = _normalise(title)
        expected_artist = _normalise(artist)
        best = None
        for item in self._search_tracks(title, artist):
            title_score = SequenceMatcher(None, expected_title, _normalise(item.get("name", ""))).ratio()
            artist_score = max(
                (
                    SequenceMatcher(None, expected_artist, _normalise(entry.get("name", ""))).ratio()
                    for entry in item.get("artists", [])
                ),
                default=0,
            )
            score = title_score * 0.65 + artist_score * 0.35
            if title_score >= 0.65 and score >= 0.72 and (best is None or score > best[0]):
                best = score, item.get("uri")
        return best[1] if best else None

    def canvas_url(self, track_uri: str) -> str | None:
        token = self._access_token()
        body, _ = self._request(
            CANVAS_URL,
            data=_canvas_request(track_uri),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/protobuf",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Spotify/9.0.34.593 iOS/18.4 (iPhone15,3)",
            },
        )
        for response_track_uri, url in _canvas_urls(body):
            if not response_track_uri or response_track_uri == track_uri:
                return url
        return None

    def find_canvas(self, title: str, artist: str) -> tuple[str | None, str | None]:
        track_uri = self.find_track_uri(title, artist)
        if not track_uri:
            return None, None
        return track_uri, self.canvas_url(track_uri)
