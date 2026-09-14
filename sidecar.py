import argparse
import asyncio
import configparser
import json
import re
import sys

# Packaged (PyInstaller) executables on Windows default stdout to the system
# locale (e.g. GBK), which cannot encode non-BMP or emoji characters found in
# streamer names/titles. The Go host always reads UTF-8, so force it here.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


# stdout carries exactly one protocol JSON line. The Go host spawns this script
# with a combined-output reader, and the vendored resolver library prints
# diagnostics to stdout on several failure paths; those prints are forwarded to
# stderr here so they can never corrupt the protocol line.
PROTOCOL_STDOUT = sys.stdout

STATE_LIVE = "live"
STATE_OFFLINE = "offline"
STATE_ERROR = "error"

EXIT_OK = 0
EXIT_FAILURE = 1

# live.douyin.com/<web_rid> embeds the 12-digit web_rid the web enter endpoint
# expects, while follow-page links (www.douyin.com/follow/live/<id>) and app
# share links embed the 19-digit room_id, which that endpoint rejects with
# status_code 4001038.
DOUYIN_WEB_RID_MAX_DIGITS = 15


class _DiagnosticsStream:
    """Forwards library prints to stderr so stdout stays protocol-only."""

    def __init__(self, target):
        self._target = target

    def write(self, text):
        if self._target is None:
            return len(text)
        return self._target.write(text)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        if self._target is None:
            return
        try:
            self._target.flush()
        except (ValueError, OSError):
            pass

    def isatty(self):
        return False

    def reconfigure(self, **_kwargs):
        return None

    def __getattr__(self, name):
        return getattr(self._target, name)


sys.stdout = _DiagnosticsStream(sys.stderr)


class RoomOffline(RuntimeError):
    """The platform was reached and reported that the room is not live.

    This is a normal monitoring state rather than a resolver failure: the host
    keeps polling and starts recording once the room goes live. Streamer
    metadata is carried along so the report stays useful.
    """

    def __init__(self, message="room is not live", stream_info=None):
        super().__init__(message)
        info = stream_info or {}
        self.streamer_name = info.get("anchor_name") or ""
        self.title = info.get("title") or ""

    def payload(self):
        return {
            "state": STATE_OFFLINE,
            "message": str(self),
            "streamerName": self.streamer_name,
            "title": self.title,
        }


def _douyin_digits_kind(digits):
    if len(digits) > DOUYIN_WEB_RID_MAX_DIGITS:
        return "room_id", digits
    return "web_rid", digits


def douyin_room_reference(url):
    """Classify a Douyin link into ("web_rid"|"room_id"|"app", value)."""
    text = (url or "").strip()
    follow_match = re.search(r"https?://(?:www\.)?douyin\.com/follow/live/(\d+)", text)
    if follow_match:
        return _douyin_digits_kind(follow_match.group(1))
    live_match = re.search(r"https?://live\.douyin\.com/(\d+)", text)
    if live_match:
        return _douyin_digits_kind(live_match.group(1))
    # App share links (v.douyin.com/...) and profile links (douyin.com/user/...)
    # are resolved through the app endpooints by the resolver itself.
    return "app", text


def normalize_douyin_url(url):
    """Return the bare room reference for links that are not plain room URLs."""
    kind, value = douyin_room_reference(url)
    if kind == "app":
        return url
    return f"https://live.douyin.com/{value}"


def stream_response(stream_info):
    if not stream_info.get("is_live"):
        raise RoomOffline("room is not live", stream_info)
    stream_url = stream_info.get("record_url") or stream_info.get("m3u8_url") or stream_info.get("flv_url")
    if not stream_url:
        raise RuntimeError("live room has no usable stream URL")
    response = {"state": STATE_LIVE, "streamUrl": stream_url}
    if stream_info.get("anchor_name"):
        response["streamerName"] = stream_info["anchor_name"]
    if stream_info.get("title"):
        response["title"] = stream_info["title"]
    return response


def resolve_stream(platform, url, quality="OD", proxy_addr="", cookies=""):
    from src import spider, stream

    if platform == "douyin":
        kind, value = douyin_room_reference(url)
        if kind == "room_id":
            data = asyncio.run(spider.get_douyin_app_stream_data_by_room_id(
                room_id=value, proxy_addr=proxy_addr, cookies=cookies))
        elif kind == "web_rid":
            data = asyncio.run(spider.get_douyin_web_stream_data(
                url=f"https://live.douyin.com/{value}", proxy_addr=proxy_addr, cookies=cookies))
        else:
            data = asyncio.run(spider.get_douyin_app_stream_data(
                url=value, proxy_addr=proxy_addr, cookies=cookies))
        stream_info = asyncio.run(stream.get_douyin_stream_url(data, quality, proxy_addr))
    elif platform == "kuaishou":
        data = asyncio.run(spider.get_kuaishou_stream_data(url=url, proxy_addr=proxy_addr, cookies=cookies))
        stream_info = asyncio.run(stream.get_kuaishou_stream_url(data, quality))
    else:
        raise RuntimeError(f"unsupported platform: {platform}")
    if not stream_info.get("is_live") and not stream_info.get("anchor_name"):
        raise RuntimeError("failed to resolve live stream (possibly invalid cookies or URL)")
    return stream_response(stream_info)


def config_credentials(config_path, platform):
    if not config_path:
        return "OD", ""
    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf-8-sig")
    settings = config["录制设置"] if "录制设置" in config else {}
    quality_label = settings.get("原画|超清|高清|标清|流畅", "原画")
    quality = {"原画": "OD", "超清": "UHD", "高清": "HD", "标清": "SD", "流畅": "LD"}.get(quality_label, "OD")
    cookies = config["Cookie"].get("抖音cookie" if platform == "douyin" else "快手cookie", "") if "Cookie" in config else ""
    return quality, cookies


def error_message(exc):
    message = str(exc).strip()
    return message or exc.__class__.__name__


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("resolve-stream")
    parser.add_argument("--platform", choices=("douyin", "kuaishou"), required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--quality", default="")
    parser.add_argument("--proxy-addr", default="")
    parser.add_argument("--cookies", default="")
    parser.add_argument("--config", default="")
    args = parser.parse_args(argv)
    config_quality, config_cookies = config_credentials(args.config, args.platform)
    quality = args.quality or config_quality
    cookies = args.cookies or config_cookies
    try:
        payload = resolve_stream(args.platform, args.url, quality, args.proxy_addr, cookies)
    except RoomOffline as exc:
        print(json.dumps(exc.payload(), ensure_ascii=False), file=PROTOCOL_STDOUT)
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - single-line protocol error, no traceback
        print(json.dumps({"state": STATE_ERROR, "message": error_message(exc)}, ensure_ascii=False),
              file=PROTOCOL_STDOUT)
        return EXIT_FAILURE
    print(json.dumps(payload, ensure_ascii=False), file=PROTOCOL_STDOUT)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
