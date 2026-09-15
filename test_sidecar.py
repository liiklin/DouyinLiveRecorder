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

    def test_classifies_douyin_live_path_variants(self):
        cases = {
            "https://live.douyin.com/499355244796": ("web_rid", "499355244796"),
            "https://www.douyin.com/root/live/795838110856?anchor_id=3729194550826920&is_aweme_tied=1": ("web_rid", "795838110856"),
            "https://www.douyin.com/root/live/7685183051802151680?anchor_id=1": ("room_id", "7685183051802151680"),
            "https://www.douyin.com/follow/live/7685183051802151680?anchor_id=1": ("room_id", "7685183051802151680"),
            "https://webcast.amemv.com/webcast/room/reflow/info/?room_id=7685183051802151680&sec_user_id=": ("room_id", "7685183051802151680"),
            "https://live.douyin.com/?live_web_rid=41421297272": ("web_rid", "41421297272"),
            "https://live.douyin.com/?live_web_rid=7685183051802151680": ("room_id", "7685183051802151680"),
            "https://v.douyin.com/iRnBho6u/": ("app", "https://v.douyin.com/iRnBho6u/"),
        }

        for url, expected in cases.items():
            self.assertEqual(sidecar.douyin_room_reference(url), expected, url)

    def test_extracts_reflow_reference_without_sec_user_id(self):
        from src.room import extract_reflow_reference

        self.assertEqual(
            extract_reflow_reference("https://webcast.amemv.com/webcast/room/reflow/info/?room_id=795838110856&anchor_id=1"),
            ("795838110856", ""),
        )
        self.assertEqual(
            extract_reflow_reference("https://webcast.amemv.com/webcast/room/reflow/info/7685183051802151680?sec_user_id=MS4wLjABAAAA-x&from=share"),
            ("7685183051802151680", "MS4wLjABAAAA-x"),
        )

    def test_normalizes_kuaishou_profile_links(self):
        cases = {
            "https://live.kuaishou.com/profile/3xqzmygz5zsvmd6": "https://live.kuaishou.com/u/3xqzmygz5zsvmd6",
            "https://live.kuaishou.com/u/3xqzmygz5zsvmd6": "https://live.kuaishou.com/u/3xqzmygz5zsvmd6",
        }

        for url, expected in cases.items():
            self.assertEqual(sidecar.normalize_kuaishou_url(url), expected, url)

    def test_unparsable_kuaishou_link_reports_readable_error(self):
        from src import spider

        async def fake_stream_data(**kwargs):
            # 解析库在链接形态不支持/房间不存在时只返回这个（没有 anchor_name）
            return {"type": 1, "is_live": False}

        original = spider.get_kuaishou_stream_data
        spider.get_kuaishou_stream_data = fake_stream_data
        try:
            payload = sidecar.describe_resolution("kuaishou", "https://live.kuaishou.com/u/not-a-real-user")
        finally:
            spider.get_kuaishou_stream_data = original

        self.assertEqual(payload["state"], sidecar.STATE_ERROR)
        self.assertIn("未能解析该快手直播间", payload["message"])
        self.assertNotIn("anchor_name", payload["message"])

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

    def test_serve_answers_each_request_in_order(self):
        requests = io.StringIO('{"id":1,"platform":"douyin","url":"https://live.douyin.com/123"}\n'
                               '{"id":2,"platform":"douyin","url":"https://live.douyin.com/124"}\n')
        replies = io.StringIO()
        original_resolve, original_stdout = sidecar.resolve_stream, sidecar.PROTOCOL_STDOUT
        sidecar.resolve_stream = lambda platform, url, quality, proxy, cookies: (
            {"state": sidecar.STATE_OFFLINE, "message": "room is not live"} if url.endswith("123")
            else {"state": sidecar.STATE_LIVE, "streamUrl": "https://stream.example/live.m3u8"})
        sidecar.PROTOCOL_STDOUT = replies
        try:
            code = sidecar.serve(requests, replies)
        finally:
            sidecar.resolve_stream, sidecar.PROTOCOL_STDOUT = original_resolve, original_stdout

        lines = [json.loads(line) for line in replies.getvalue().splitlines() if line.strip()]
        self.assertEqual(code, sidecar.EXIT_OK)
        self.assertEqual([line["id"] for line in lines], [1, 2])
        self.assertEqual(lines[0]["state"], sidecar.STATE_OFFLINE)
        self.assertEqual(lines[1]["streamUrl"], "https://stream.example/live.m3u8")

    def test_serve_survives_bad_requests_and_stops_on_quit(self):
        requests = io.StringIO('oops\n{"id":7,"platform":"douyin","url":"https://live.douyin.com/7"}\n{"command":"quit"}\n')
        replies = io.StringIO()
        original_resolve = sidecar.resolve_stream
        sidecar.resolve_stream = lambda platform, url, quality, proxy, cookies: {"state": sidecar.STATE_OFFLINE}
        try:
            sidecar.serve(requests, replies)
        finally:
            sidecar.resolve_stream = original_resolve

        lines = [json.loads(line) for line in replies.getvalue().splitlines() if line.strip()]
        self.assertEqual(lines[0]["state"], sidecar.STATE_ERROR)
        self.assertEqual(lines[1]["id"], 7)
        self.assertEqual(len(lines), 2)

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
