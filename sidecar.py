import argparse
import asyncio
import configparser
import json


def stream_response(stream_info):
    if not stream_info.get("is_live"):
        raise RuntimeError("room is not live")
    stream_url = stream_info.get("record_url") or stream_info.get("m3u8_url") or stream_info.get("flv_url")
    if not stream_url:
        raise RuntimeError("live room has no usable stream URL")
    response = {"streamUrl": stream_url}
    if stream_info.get("anchor_name"):
        response["streamerName"] = stream_info["anchor_name"]
    if stream_info.get("title"):
        response["title"] = stream_info["title"]
    return response


def resolve_stream(platform, url, quality="OD", proxy_addr="", cookies=""):
    from src import spider, stream

    if platform == "douyin":
        data = asyncio.run(spider.get_douyin_web_stream_data(url=url, proxy_addr=proxy_addr, cookies=cookies))
        stream_info = asyncio.run(stream.get_douyin_stream_url(data, quality, proxy_addr))
    elif platform == "kuaishou":
        data = asyncio.run(spider.get_kuaishou_stream_data(url=url, proxy_addr=proxy_addr, cookies=cookies))
        stream_info = asyncio.run(stream.get_kuaishou_stream_url(data, quality))
    else:
        raise RuntimeError(f"unsupported platform: {platform}")
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("resolve-stream")
    parser.add_argument("--platform", choices=("douyin", "kuaishou"), required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--quality", default="")
    parser.add_argument("--proxy-addr", default="")
    parser.add_argument("--cookies", default="")
    parser.add_argument("--config", default="")
    args = parser.parse_args()
    config_quality, config_cookies = config_credentials(args.config, args.platform)
    quality = args.quality or config_quality
    cookies = args.cookies or config_cookies
    print(json.dumps(resolve_stream(args.platform, args.url, quality, args.proxy_addr, cookies), ensure_ascii=False))


if __name__ == "__main__":
    main()
