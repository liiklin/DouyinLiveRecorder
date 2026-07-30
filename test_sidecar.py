import unittest

import sidecar


class SidecarTest(unittest.TestCase):
    def test_selects_douyin_record_url(self):
        result = sidecar.stream_response({"is_live": True, "record_url": "https://stream.example/live.m3u8"})

        self.assertEqual(result, {"streamUrl": "https://stream.example/live.m3u8"})

    def test_rejects_offline_room(self):
        with self.assertRaisesRegex(RuntimeError, "not live"):
            sidecar.stream_response({"is_live": False})

    def test_includes_stream_metadata_when_available(self):
        result = sidecar.stream_response({"is_live": True, "record_url": "https://stream.example/live.m3u8", "anchor_name": "主播", "title": "直播标题"})

        self.assertEqual(result["streamerName"], "主播")
        self.assertEqual(result["title"], "直播标题")


if __name__ == "__main__":
    unittest.main()
