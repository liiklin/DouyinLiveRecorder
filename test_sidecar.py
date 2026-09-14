import io
import json
import unittest

import sidecar


class SidecarTest(unittest.TestCase):
    def test_selects_douyin_record_url(self):
        result = sidecar.stream_response({"is_live": True, "record_url": "https://stream.example/live.m3u8"})

        self.assertEqual(result["streamUrl"], "https://stream.example/live.m3u8")
        self.assertEqual(result["state"], sidecar.STATE_LIVE)

    def test_rejects_offline_room(self):
        with self.assertRaisesRegex(RuntimeError, "not live"):
            sidecar.stream_response({"is_live": False})

    def test_includes_stream_metadata_when_available(self):
        result = sidecar.stream_response({"is_live": True, "record_url": "https://stream.example/live.m3u8", "anchor_name": "主播", "title": "直播标题"})

        self.assertEqual(result["streamerName"], "主播")
        self.assertEqual(result["title"], "直播标题")

    def test_offline_payload_keeps_streamer_metadata(self):
        with self.assertRaises(sidecar.RoomOffline) as raised:
            sidecar.stream_response({"is_live": False, "anchor_name": "主播", "title": "标题"})

        payload = raised.exception.payload()
        self.assertEqual(payload["state"], sidecar.STATE_OFFLINE)
        self.assertEqual(payload["streamerName"], "主播")
        self.assertEqual(payload["title"], "标题")

    def test_classifies_douyin_room_references(self):
        cases = {
            "https://live.douyin.com/499355244796": ("web_rid", "499355244796"),
            "https://live.douyin.com/7685183051802151680": ("room_id", "7685183051802151680"),
            "https://www.douyin.com/follow/live/7685183051802151680?anchor_id=1": ("room_id", "7685183051802151680"),
            "https://v.douyin.com/iRnBho6u/": ("app", "https://v.douyin.com/iRnBho6u/"),
            "https://www.douyin.com/user/MS4wLjABAAAA": ("app", "https://www.douyin.com/user/MS4wLjABAAAA"),
        }

        for url, expected in cases.items():
            self.assertEqual(sidecar.douyin_room_reference(url), expected, url)

    def test_normalize_douyin_url_returns_bare_room_reference(self):
        self.assertEqual(
            sidecar.normalize_douyin_url("https://www.douyin.com/follow/live/7685183051802151680?anchor_id=1"),
            "https://live.douyin.com/7685183051802151680",
        )

    def test_main_reports_offline_room_as_successful_probe(self):
        captured = self.run_main(lambda *args, **kwargs: (_ for _ in ()).throw(
            sidecar.RoomOffline("room is not live", {"anchor_name": "主播", "title": "标题"})))

        self.assertEqual(captured["exit"], sidecar.EXIT_OK)
        self.assertEqual(captured["payload"], {
            "state": sidecar.STATE_OFFLINE,
            "message": "room is not live",
            "streamerName": "主播",
            "title": "标题",
        })

    def test_main_reports_resolver_failure_on_one_stdout_line(self):
        captured = self.run_main(lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("it triggered risk control")))

        self.assertEqual(captured["exit"], sidecar.EXIT_FAILURE)
        self.assertEqual(captured["payload"], {"state": sidecar.STATE_ERROR, "message": "it triggered risk control"})

    def run_main(self, resolve_stream):
        original_resolve, original_stdout = sidecar.resolve_stream, sidecar.PROTOCOL_STDOUT
        buffer = io.StringIO()
        sidecar.resolve_stream = resolve_stream
        sidecar.PROTOCOL_STDOUT = buffer
        try:
            exit_code = sidecar.main(["resolve-stream", "--platform", "douyin", "--url", "https://live.douyin.com/123"])
        finally:
            sidecar.resolve_stream, sidecar.PROTOCOL_STDOUT = original_resolve, original_stdout

        lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, lines)
        return {"exit": exit_code, "payload": json.loads(lines[0])}


if __name__ == "__main__":
    unittest.main()
