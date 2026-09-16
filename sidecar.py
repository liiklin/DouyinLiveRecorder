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


DOUYIN_LIVE_HOST = re.compile(r"https?://live\.douyin\.com/(\d+)", re.IGNORECASE)
# 抖音会按入口改路径：follow/live（关注页）、root/live（新版分享/推荐卡片）都带房间号。
DOUYIN_LIVE_PATH = re.compile(r"https?://(?:[\w\-]+\.)?douyin\.com/(?:[^?#]*/)?live/(\d+)", re.IGNORECASE)
# 直播分享链接/接口链接会把房间号放在查询参数里：
#   ?room_id=<19 位>            webcast reflow 链接
#   ?live_web_rid=<web_rid>     live.douyin.com/?live_web_rid=... 分享链接
DOUYIN_ROOM_ID_PARAM = re.compile(r"[?&]room_id=(\d+)", re.IGNORECASE)
DOUYIN_WEB_RID_PARAM = re.compile(r"[?&]live_web_rid=(\d+)", re.IGNORECASE)


def _douyin_digits_kind(digits):
    if len(digits) > DOUYIN_WEB_RID_MAX_DIGITS:
        return "room_id", digits
    return "web_rid", digits


def douyin_room_reference(url):
    """Classify a Douyin link into ("web_rid"|"room_id"|"app", value).

    live.douyin.com/<web_rid>            -> web 接口（12 位 web_rid）
    douyin.com/root|follow/live/<id>     -> 12 位走 web 接口，19 位走 app reflow
    ...?room_id=<id>                     -> 同上，按位数判断
    ...?live_web_rid=<web_rid>           -> web 接口（分享链接）
    v.douyin.com/<短码>、douyin.com/user/<sec_uid> -> app 分享/主页链接
    """
    text = (url or "").strip()
    for pattern in (DOUYIN_ROOM_ID_PARAM, DOUYIN_WEB_RID_PARAM, DOUYIN_LIVE_PATH, DOUYIN_LIVE_HOST):
        match = pattern.search(text)
        if match:
            return _douyin_digits_kind(match.group(1))
    return "app", text


def normalize_douyin_url(url):
    """Return the bare room reference for links that are not plain room URLs."""
    kind, value = douyin_room_reference(url)
    if kind == "app":
        return url
    return f"https://live.douyin.com/{value}"


KUAISHOU_PROFILE_PATTERN = re.compile(r"https?://live\.kuaishou\.com/profile/([\w-]+)", re.IGNORECASE)
# 我方支持解析的快手直播间/主页地址（其余形态才该报"链接形态不受支持"）。
KUAISHOU_ROOM_PATTERN = re.compile(r"https?://(?:live\.)?kuaishou\.com/(?:u|profile)/[\w-]+", re.IGNORECASE)
KUAISHOU_SHARE_PATTERN = re.compile(r"https?://(?:v|www)\.kuaishou\.com/", re.IGNORECASE)
# 抖音的分享/主页链接（v.douyin.com、douyin.com/user/...）：解析库认得，只是没在播。
DOUYIN_SHARE_PATTERN = re.compile(r"https?://(?:v\.)?douyin\.com/", re.IGNORECASE)


def kuaishou_link_supported(url):
    """链接形态是否是我方支持解析的快手地址（直播间/主页/分享短链）。"""
    text = (url or "").strip()
    if not text:
        return False
    return bool(KUAISHOU_ROOM_PATTERN.match(text) or KUAISHOU_SHARE_PATTERN.match(text))


def kuaishou_page_error(url, proxy_addr="", cookies=""):
    """取快手房间页自身给出的 errorType 文本（标题+说明），取不到返回 ""。

    解析库只在页面是错误页时返回没有主播名的 is_live=False，而"错误页"既可能是
    "房间不存在"，也可能是"直播已结束/未开播"。把页面原话取回来才能区分。
    """
    try:
        from src import spider
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
            "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        }
        if cookies:
            headers["Cookie"] = cookies
        html_str = asyncio.run(spider.async_req(url=url, proxy_addr=proxy_addr, headers=headers))
    except Exception:  # noqa: BLE001 - 取不到就当作"没说"
        return ""
    block = re.search(r'"errorType"\s*:\s*\{(.*?)\}', html_str, re.DOTALL)
    if not block:
        return ""
    parts = []
    for key in ("title", "content"):
        found = re.search(r'"' + key + r'"\s*:\s*"([^"]*)"', block.group(1))
        if found and found.group(1).strip():
            parts.append(found.group(1).strip())
    return " ".join(parts)



def douyin_link_supported(url):
    """链接形态是否是我方支持解析的抖音地址。

    直播间引用（web_rid/room_id）必然可解析；"app" 形态里只有分享/主页链接算支持，
    其余（例如随手粘的一段文字）才该报解析失败。
    """
    text = (url or "").strip()
    if not text:
        return False
    if douyin_room_reference(text)[0] != "app":
        return True
    return bool(DOUYIN_SHARE_PATTERN.match(text))


def normalize_kuaishou_url(url):
    """快手主页链接（/profile/<主播号>）等非直播间地址统一成直播间地址（/u/<主播号>）。"""
    text = (url or "").strip()
    match = KUAISHOU_PROFILE_PATTERN.match(text)
    if match:
        return f"https://live.kuaishou.com/u/{match.group(1)}"
    return text


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
        kuaishou_url = normalize_kuaishou_url(url)
        data = asyncio.run(spider.get_kuaishou_stream_data(
            url=kuaishou_url, proxy_addr=proxy_addr, cookies=cookies))
        if not data.get("is_live") and not data.get("anchor_name"):
            # 解析库对"没在播 / 刚被风控挡了一次 / 房间不存在"都返回
            # {"type": 1|2, "is_live": False}（没有 anchor_name，继续往下会漏出
            # KeyError），所以这里得自己区分"没在播"和"链接/房间确实不行"。
            if not kuaishou_link_supported(kuaishou_url):
                raise RuntimeError(
                    "未能解析该快手直播间：链接形态不受支持"
                    "（请使用直播间分享链接，或 live.kuaishou.com/u/<主播号>）")
            if data.get("type") == 2:
                # 页面自己给了错误文案。注意：实测"错误代码22 浏览其他内容"这类
                # 既出现在房间不存在时，也出现在被限流/需要验证时（同一个真实房间
                # 两种都遇到过），所以不能据此报解析失败——按未开播处理，把平台原话
                # 放进 message 供排查，UI 上显示的是正常的"未开播"。
                page_error = kuaishou_page_error(kuaishou_url, proxy_addr, cookies)
                if page_error:
                    raise RoomOffline(f"room is not live (平台返回：{page_error})", data)
            # type == 1（抓取/解析失败，多半是限流或网络抖动）以及"没在播"的页面：
            # 都按未开播处理，监控下一轮继续重试，不再显示成解析失败。
            raise RoomOffline("room is not live", data)
        stream_info = asyncio.run(stream.get_kuaishou_stream_url(data, quality))
    else:
        raise RuntimeError(f"unsupported platform: {platform}")
    if not stream_info.get("is_live") and not stream_info.get("anchor_name"):
        # 同快手：链接形态认得出来，就说明只是没在播（或解析被挡了一次），
        # 属于正常监控状态，不该报解析失败。
        if douyin_link_supported(url):
            raise RoomOffline("room is not live", stream_info)
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


def describe_resolution(platform, url, quality="", proxy_addr="", cookies="", config_path=""):
    """Resolve one request and always return a protocol payload (never raises)."""
    if config_path:
        config_quality, config_cookies = config_credentials(config_path, platform)
        quality = quality or config_quality
        cookies = cookies or config_cookies
    try:
        return resolve_stream(platform, url, quality or "OD", proxy_addr, cookies)
    except RoomOffline as exc:
        return exc.payload()
    except Exception as exc:  # noqa: BLE001 - never leak a traceback into the protocol
        return {"state": STATE_ERROR, "message": error_message(exc)}


def serve(stdin=None, stdout=None):
    """常驻模式：stdin 每行一个 JSON 请求，stdout 每行一个 JSON 应答。

    客户端因此不必每次轮询都重新启动这个（PyInstaller 打包的）可执行文件。
    stdin 读到 EOF 就退出，所以宿主进程被强杀也不会留下孤儿进程。
    """
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else PROTOCOL_STDOUT

    def reply(payload):
        print(json.dumps(payload, ensure_ascii=False), file=stdout, flush=True)

    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            reply({"state": STATE_ERROR, "message": "invalid request line"})
            continue
        if not isinstance(request, dict):
            reply({"state": STATE_ERROR, "message": "invalid request payload"})
            continue
        if request.get("command") == "quit":
            break
        payload = describe_resolution(
            request.get("platform", ""),
            request.get("url", ""),
            request.get("quality", ""),
            request.get("proxyAddr", ""),
            request.get("cookies", ""),
            request.get("config", ""),
        )
        if "id" in request:
            payload = {"id": request["id"], **payload}
        reply(payload)
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("resolve-stream", "serve"))
    parser.add_argument("--platform", choices=("douyin", "kuaishou"))
    parser.add_argument("--url", default="")
    parser.add_argument("--quality", default="")
    parser.add_argument("--proxy-addr", default="")
    parser.add_argument("--cookies", default="")
    parser.add_argument("--config", default="")
    args = parser.parse_args(argv)
    if args.command == "serve":
        return serve()
    if not args.platform or not args.url:
        parser.error("resolve-stream requires --platform and --url")
    payload = describe_resolution(args.platform, args.url, args.quality, args.proxy_addr, args.cookies, args.config)
    print(json.dumps(payload, ensure_ascii=False), file=PROTOCOL_STDOUT)
    return EXIT_OK if payload.get("state") != STATE_ERROR else EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
