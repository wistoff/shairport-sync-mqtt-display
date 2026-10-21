import threading
import unittest

from PIL import Image

from display_transition import DisplayTransition


class DisplayTransitionTests(unittest.TestCase):
    def test_crossfade_emits_intermediate_frames_and_target(self):
        sent = []
        display = DisplayTransition(
            lambda image: sent.append(image.copy()),
            (1, 1),
            duration=0.5,
            fps=4,
            wait=lambda _: False,
        )
        display.show(Image.new("RGBA", (1, 1), (0, 0, 0, 255)))
        sent.clear()

        completed = display.crossfade(Image.new("RGBA", (1, 1), (200, 100, 0, 255)))

        self.assertTrue(completed)
        self.assertEqual([frame.getpixel((0, 0)) for frame in sent], [
            (100, 50, 0, 255),
            (200, 100, 0, 255),
        ])

    def test_first_frame_fades_from_black(self):
        sent = []
        display = DisplayTransition(
            lambda image: sent.append(image.copy()),
            (1, 1),
            duration=0.5,
            fps=4,
            wait=lambda _: False,
        )

        display.crossfade(Image.new("RGBA", (1, 1), (200, 200, 200, 255)))

        self.assertEqual(sent[0].getpixel((0, 0)), (100, 100, 100, 255))
        self.assertEqual(sent[-1].getpixel((0, 0)), (200, 200, 200, 255))

    def test_cancel_event_preserves_last_completed_frame(self):
        sent = []
        cancel = threading.Event()

        def stop_after_first(_):
            cancel.set()
            return False

        display = DisplayTransition(
            lambda image: sent.append(image.copy()),
            (1, 1),
            duration=1,
            fps=4,
            wait=stop_after_first,
        )
        display.show(Image.new("RGBA", (1, 1), (0, 0, 0, 255)))
        sent.clear()

        completed = display.crossfade(
            Image.new("RGBA", (1, 1), (200, 0, 0, 255)),
            cancel_event=cancel,
        )

        self.assertFalse(completed)
        self.assertEqual(len(sent), 1)
        self.assertEqual(display.snapshot().getpixel((0, 0)), (50, 0, 0, 255))

    def test_unrelated_named_cancel_does_not_interrupt_cover_transition(self):
        sent = []
        display = None

        def metadata_arrived(_):
            display.cancel(kind="clear", restore=True)
            return False

        display = DisplayTransition(
            lambda image: sent.append(image.copy()),
            (1, 1),
            duration=1,
            fps=2,
            wait=metadata_arrived,
        )
        display.show(Image.new("RGBA", (1, 1), (0, 0, 0, 255)))
        sent.clear()

        completed = display.crossfade(
            Image.new("RGBA", (1, 1), (200, 200, 200, 255)),
            kind="cover",
        )

        self.assertTrue(completed)
        self.assertEqual(display.snapshot().getpixel((0, 0)), (200, 200, 200, 255))

    def test_named_cancel_restores_frame_before_clear(self):
        sent = []
        display = None

        def playback_resumed(_):
            display.cancel(kind="clear", restore=True)
            return False

        display = DisplayTransition(
            lambda image: sent.append(image.copy()),
            (1, 1),
            duration=1,
            fps=2,
            wait=playback_resumed,
        )
        cover = Image.new("RGBA", (1, 1), (200, 100, 50, 255))
        display.show(cover)
        sent.clear()

        completed = display.crossfade(
            Image.new("RGBA", (1, 1), (0, 0, 0, 255)),
            kind="clear",
        )

        self.assertFalse(completed)
        self.assertEqual(display.snapshot().getpixel((0, 0)), (200, 100, 50, 255))
        self.assertEqual(sent[-1].getpixel((0, 0)), (200, 100, 50, 255))

    def test_immediate_show_supersedes_transition(self):
        sent = []
        display = None
        replacement = Image.new("RGBA", (1, 1), (0, 200, 0, 255))

        def wait(_):
            display.show(replacement)
            return False

        display = DisplayTransition(
            lambda image: sent.append(image.copy()),
            (1, 1),
            duration=1,
            fps=4,
            wait=wait,
        )
        display.show(Image.new("RGBA", (1, 1), (0, 0, 0, 255)))
        sent.clear()

        completed = display.crossfade(Image.new("RGBA", (1, 1), (200, 0, 0, 255)))

        self.assertFalse(completed)
        self.assertEqual(display.snapshot().getpixel((0, 0)), (0, 200, 0, 255))


if __name__ == "__main__":
    unittest.main()
