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
    def __init__(self, message_str="", messages=None, platform_name=""):
        self.results = []
        self._message_str = message_str
        self._messages = messages or []
        self._platform_name = platform_name

    def plain_result(self, text):
        self.results.append(("plain", text))
        return text

    def chain_result(self, chain):
        self.results.append(("chain", chain))
        return chain

    def get_message_str(self):
        return self._message_str

    def get_messages(self):
        return self._messages

    def get_platform_name(self):
        return self._platform_name


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


def test_request_json_surfaces_scdn_error_body(monkeypatch):
    p = _make_plugin()

    class FakeResponse:
        status = 400
        headers = {}
        request_info = None
        history = ()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            raise aiohttp.ClientResponseError(
                None, (), status=400, message="Bad Request", headers={}
            )

        async def text(self):
            return '{"success":false,"error":"远程图片下载失败"}'

    class FakeSession:
        def request(self, method, url, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(p, "_http_session", lambda: FakeSession())

    async def call():
        try:
            await p._request_json("POST", data={"image_url": "https://x/a.png"})
        except aiohttp.ClientResponseError as e:
            return e
        raise AssertionError("expected ClientResponseError")

    err = asyncio.run(call())
    assert err.status == 400
    assert err.message == "远程图片下载失败"


def test_call_and_reply_parse_error():
    p = _make_plugin()
    event = MockEvent()

    async def coro():
        raise ResponseParseError("解析响应失败: bad", "<html>")

    _collect(p._call_and_reply(event, coro(), lambda r: "ok", "fail"))
    assert event.results[0][0] == "plain"
    assert "图床响应解析失败" in event.results[0][1]


def test_call_and_reply_timeout_error():
    p = _make_plugin()
    event = MockEvent()

    async def coro():
        raise TimeoutError()

    _collect(p._call_and_reply(event, coro(), lambda r: "ok", "fail"))
    assert event.results == [("plain", "上传图床超时，请稍后重试或在插件配置中调大 timeout。")]


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


def test_call_and_reply_formatter_error_is_handled():
    p = _make_plugin()
    event = MockEvent()

    async def coro():
        return {"success": True, "data": []}

    def bad_formatter(_result):
        raise TypeError("bad shape")

    _collect(p._call_and_reply(event, coro(), bad_formatter, "fail"))
    assert event.results == [("plain", "操作成功，但解析返回内容失败。")]


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


def test_seg_to_bytes_ignores_non_http_image_url_and_uses_base64():
    from main import Image

    class LocalImage(Image):
        url = "file:///cache/image.jpg"

        async def convert_to_base64(self):
            return base64.b64encode(b"local-image").decode()

    assert asyncio.run(_seg_to_bytes(LocalImage())) == (None, b"local-image", "image.bin")


def test_extract_first_image_base64_ignores_non_dict_data():
    p = _make_plugin()
    assert p._extract_first_image_base64([{"type": "image", "data": "bad"}]) is None


# ---- command handlers ----


def test_upload_image_accepts_data_uri_arg():
    p = _make_plugin()
    event = MockEvent("/图床上传 data:image/png;base64,cG5n")
    seen = {}

    async def fake_upload_file(file_bytes, filename, extra):
        seen["file_bytes"] = file_bytes
        seen["filename"] = filename
        seen["extra"] = extra
        return {"success": True, "url": "https://img.scdn.io/i/a.png"}

    p._upload_file = fake_upload_file

    _collect(p.upload_image(event))
    assert seen == {"file_bytes": b"png", "filename": "image.bin", "extra": {}}
    assert event.results[-1] == ("plain", "上传成功！\nURL: https://img.scdn.io/i/a.png")


def test_upload_image_downloads_message_url_before_uploading_file():
    p = _make_plugin()
    event = MockEvent("/图床上传", [{"type": "image", "data": {"url": "https://qq.local/a.jpg"}}])
    seen = {}

    async def fake_download(url, log_label="图片"):
        seen["download"] = (url, log_label)
        return b"jpg-bytes"

    async def fake_upload_file(file_bytes, filename, extra):
        seen["upload_file"] = (file_bytes, filename, extra)
        return {"success": True, "url": "https://img.scdn.io/i/a.webp"}

    async def fake_upload_by_url(_image_url, _extra):
        raise AssertionError("message image URL should not be sent directly to SCDN")

    p._download_bytes = fake_download
    p._upload_file = fake_upload_file
    p._upload_by_url = fake_upload_by_url

    _collect(p.upload_image(event))
    assert seen == {
        "download": ("https://qq.local/a.jpg", "图片"),
        "upload_file": (b"jpg-bytes", "image.bin", {}),
    }
    assert event.results[-1] == ("plain", "上传成功！\nURL: https://img.scdn.io/i/a.webp")


def test_upload_image_reply_uses_onebot_get_msg_even_when_platform_name_differs():
    p = _make_plugin()
    event = MockEvent(
        "/图床上传",
        [{"type": "Reply", "data": {"id": "123"}}],
        platform_name="napcat",
    )
    seen = {}

    class FakeApi:
        async def call_action(self, action, **kwargs):
            seen["call_action"] = (action, kwargs)
            return {"message": [{"type": "image", "data": {"url": "https://qq.local/reply.jpg"}}]}

    class FakeBot:
        api = FakeApi()

    async def fake_download(url, log_label="图片"):
        seen["download"] = (url, log_label)
        return b"reply-bytes"

    async def fake_upload_file(file_bytes, filename, extra):
        seen["upload_file"] = (file_bytes, filename, extra)
        return {"success": True, "url": "https://img.scdn.io/i/reply.webp"}

    event.bot = FakeBot()
    p._download_bytes = fake_download
    p._upload_file = fake_upload_file

    _collect(p.upload_image(event))
    assert seen == {
        "call_action": ("get_msg", {"message_id": 123}),
        "download": ("https://qq.local/reply.jpg", "图片"),
        "upload_file": (b"reply-bytes", "image.bin", {}),
    }
    assert event.results[-1] == ("plain", "上传成功！\nURL: https://img.scdn.io/i/reply.webp")


def test_upload_image_url_rejects_data_uri():
    p = _make_plugin()
    event = MockEvent("/图床链接 data:image/png;base64,cG5n")

    _collect(p.upload_image_url(event))
    assert event.results == [
        ("plain", "图床链接仅支持 HTTP/HTTPS 图片链接；data URI 请使用 /图床上传。")
    ]


def test_download_bytes_logs_without_sensitive_url(monkeypatch, caplog):
    p = _make_plugin()
    secret_url = "https://api.telegram.org/file/botSECRET_TOKEN/photos/file.jpg"

    class FailingSession:
        def get(self, url):
            assert url == secret_url
            raise RuntimeError(f"boom {url}")

    monkeypatch.setattr(p, "_http_session", lambda: FailingSession())

    with caplog.at_level("ERROR", logger="scdnimg-bed.test"):
        result = asyncio.run(p._download_bytes(secret_url, "Telegram 图片"))

    assert result is None
    assert "SECRET_TOKEN" not in caplog.text
    assert secret_url not in caplog.text
    assert "RuntimeError" in caplog.text


def test_query_image_extracts_arg_from_message_text():
    p = _make_plugin()
    event = MockEvent("/图床查询 https://img.scdn.io/i/a.webp?x=1")
    seen = {}

    async def fake_query_image(query):
        seen["query"] = query
        return {"success": True, "data": {"filename": "a.webp"}}

    p._query_image = fake_query_image

    _collect(p.query_image(event))
    assert seen == {"query": "a.webp"}
    assert event.results[-1] == ("plain", "图片信息：\n文件名: a.webp")


def test_parse_scdn_link_extracts_arg_from_message_text_and_strips_quote():
    p = _make_plugin()
    raw = "/图床解析 https://img.scdn.io/i/a.webp [引用消息(用户:旧消息)]"
    event = MockEvent(raw)
    seen = {}

    async def fake_query_image(query):
        seen["query"] = query
        return {
            "success": True,
            "data": {"filename": "a.webp", "image_url": "https://img.scdn.io/i/a.webp"},
        }

    p._query_image = fake_query_image

    _collect(p.parse_scdn_link(event))
    assert seen == {"query": "a.webp"}
    assert event.results[0] == ("plain", "正在解析图片链接...")
    assert event.results[1][0] == "chain"


# ---- ResponseParseError ----


def test_response_parse_error_carries_body():
    e = ResponseParseError("msg", "<body>")
    assert e.body == "<body>"
    assert isinstance(e, RuntimeError)
