"""实例方法测试，覆盖 _call_and_reply 异常分流 (#14) 与 _seg_to_bytes (#13)。"""
import asyncio
import base64

import aiohttp

from main import AstrBotConfig, Context, ResponseParseError, ScdnImgBedPlugin, _seg_to_bytes


def _make_plugin() -> ScdnImgBedPlugin:
    cfg = AstrBotConfig(
        {
            "api_base_url": "https://example/api",
            "default_cdn_domain": "img.scdn.io",
            "default_storage": "local",
            "default_output_format": "auto",
            "timeout": 10,
        }
    )
    return ScdnImgBedPlugin(Context(), cfg)


class MockEvent:
    def __init__(self):
        self.results = []

    def plain_result(self, text):
        self.results.append(("plain", text))
        return text

    def chain_result(self, chain):
        self.results.append(("chain", chain))
        return chain


def _collect(agen):
    async def _drain():
        return [x async for x in agen]

    return asyncio.run(_drain())


# ---- _call_and_reply 异常分流 (#14) ----


def test_call_and_reply_success_plain():
    p = _make_plugin()
    event = MockEvent()

    async def coro():
        return {"success": True, "url": "https://x"}

    _collect(p._call_and_reply(event, coro(), lambda r: f"ok:{r['url']}", "fail"))
    assert event.results == [("plain", "ok:https://x")]


def test_call_and_reply_success_chain():
    p = _make_plugin()
    event = MockEvent()

    async def coro():
        return {"success": True}

    _collect(p._call_and_reply(event, coro(), lambda r: ["chain"], "fail"))
    assert event.results == [("chain", ["chain"])]


def test_call_and_reply_http_error():
    p = _make_plugin()
    event = MockEvent()

    async def coro():
        raise aiohttp.ClientResponseError(None, None, status=500, message="boom")

    _collect(p._call_and_reply(event, coro(), lambda r: "ok", "fail"))
    assert event.results == [("plain", "图床返回 500：boom")]


def test_call_and_reply_parse_error():
    p = _make_plugin()
    event = MockEvent()

    async def coro():
        raise ResponseParseError("解析响应失败: bad", "<html>")

    _collect(p._call_and_reply(event, coro(), lambda r: "ok", "fail"))
    assert event.results[0][0] == "plain"
    assert "图床响应解析失败" in event.results[0][1]


def test_call_and_reply_unknown_error():
    p = _make_plugin()
    event = MockEvent()

    async def coro():
        raise ValueError("unexpected")

    _collect(p._call_and_reply(event, coro(), lambda r: "ok", "boom-fail"))
    assert event.results == [("plain", "boom-fail")]


def test_call_and_reply_failure_result():
    p = _make_plugin()
    event = MockEvent()

    async def coro():
        return {"success": False, "message": "限额"}

    _collect(p._call_and_reply(event, coro(), lambda r: "ok", "fail"))
    assert event.results == [("plain", "操作失败：限额")]


# ---- _seg_to_bytes (#12/#13) ----


def test_seg_to_bytes_file_cache_returns_none():
    seg = {"type": "image", "data": {"file": "hash.jpg"}}
    assert asyncio.run(_seg_to_bytes(seg)) == (None, None, None)


def test_seg_to_bytes_url():
    seg = {"type": "image", "data": {"url": "https://x/a.png"}}
    assert asyncio.run(_seg_to_bytes(seg)) == ("https://x/a.png", None, None)


def test_seg_to_bytes_base64():
    b64 = base64.b64encode(b"pngdata").decode()
    seg = {"type": "image", "data": {"base64": b64}}
    url, bts, fname = asyncio.run(_seg_to_bytes(seg))
    assert url is None
    assert bts == b"pngdata"
    assert fname == "image.bin"


def test_seg_to_bytes_image_obj_no_b64():
    from main import Image

    seg = Image()  # stub：无 url、convert_to_base64 返回 ""
    assert asyncio.run(_seg_to_bytes(seg)) == (None, None, None)


# ---- ResponseParseError ----


def test_response_parse_error_carries_body():
    e = ResponseParseError("msg", "<body>")
    assert e.body == "<body>"
    assert isinstance(e, RuntimeError)
