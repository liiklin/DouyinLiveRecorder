import argparse
import asyncio
import json


def stream_response(stream_info):
    if not stream_info.get("is_live"):
        raise RuntimeError("room is not live")
    stream_url = stream_info.get("record_url") or stream_info.get("m3u8_url") or stream_info.get("flv_url")
    if not stream_url:
        raise RuntimeError("live room has no usable stream URL")
    return {"streamUrl": stream_url}


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("resolve-stream")
    parser.add_argument("--platform", choices=("douyin", "kuaishou"), required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--quality", default="OD")
    parser.add_argument("--proxy-addr", default="")
    parser.add_argument("--cookies", default="")
    args = parser.parse_args()
    print(json.dumps(resolve_stream(args.platform, args.url, args.quality, args.proxy_addr, args.cookies), ensure_ascii=False))


if __name__ == "__main__":
    main()
