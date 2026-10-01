import unittest

from spotify_canvas import (
    SpotifyCanvasClient,
    _canvas_request,
    _canvas_urls,
    _normalise,
    _varint,
)


def length_field(number, value):
    return bytes([number << 3 | 2]) + _varint(len(value)) + value


class SpotifyCanvasTests(unittest.TestCase):
    def test_canvas_request_contains_track_uri(self):
        track_uri = "spotify:track:123"
        self.assertIn(track_uri.encode(), _canvas_request(track_uri))

    def test_canvas_response_extracts_url_and_track(self):
        track_uri = "spotify:track:123"
        canvas_url = "https://canvaz.scdn.co/example.cnvs.mp4"
        canvas = length_field(2, canvas_url.encode()) + length_field(5, track_uri.encode())
        response = length_field(1, canvas)
        self.assertEqual(list(_canvas_urls(response)), [(track_uri, canvas_url)])

    def test_normalise_ignores_accents_and_feature_suffix(self):
        self.assertEqual(_normalise("Beyoncé (feat. Jay-Z)"), "beyonce")

    def test_search_rejects_unrelated_result(self):
        client = SpotifyCanvasClient("not-used")
        client._search_tracks = lambda *args: [
            {
                "name": "Completely Different Song",
                "uri": "spotify:track:wrong",
                "artists": [{"name": "Someone Else"}],
            }
        ]
        self.assertIsNone(client.find_track_uri("Known Song", "Known Artist"))

    def test_search_accepts_close_result(self):
        client = SpotifyCanvasClient("not-used")
        client._search_tracks = lambda *args: [
            {
                "name": "Known Song - Remastered",
                "uri": "spotify:track:right",
                "artists": [{"name": "Known Artist"}],
            }
        ]
        self.assertEqual(
            client.find_track_uri("Known Song", "Known Artist"),
            "spotify:track:right",
        )


if __name__ == "__main__":
    unittest.main()
