import unittest
from unittest.mock import patch

import flaschen


class FlaschenFrameTests(unittest.TestCase):
    @patch("flaschen.socket.socket")
    def test_send_rgb_copies_row_major_frame_and_preserves_opaque_black(self, socket_type):
        sock = socket_type.return_value
        client = flaschen.Flaschen("localhost", 1337, 2, 1)

        client.send_rgb(bytes((0, 0, 0, 10, 20, 30)))

        start = client._header_len
        self.assertEqual(
            bytes(client._data[start:start + 6]),
            bytes((1, 1, 1, 10, 20, 30)),
        )
        sock.send.assert_called_once_with(client._data)

    @patch("flaschen.socket.socket")
    def test_send_rgb_rejects_wrong_frame_size(self, socket_type):
        client = flaschen.Flaschen("localhost", 1337, 2, 1)
        with self.assertRaises(ValueError):
            client.send_rgb(b"too short")


if __name__ == "__main__":
    unittest.main()
