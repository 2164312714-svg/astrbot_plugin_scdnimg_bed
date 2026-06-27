"""纯函数单测，覆盖 issue #8/#9/#10/#12 的回归。"""
import main
from main import (
    _clean_url_or_query,
    _extract_image_url_or_path,
    _extract_scdn_identifier,
    _is_url,
    _looks_like_url_or_data,
    _parse_upload_args,
    build_scdn_link_re,
    strip_command_prefix,
)

# ---- _clean_url_or_query ----


def test_clean_strips_wrappers():
    assert _clean_url_or_query("  `https://x/a.png`  ") == "https://x/a.png"
    assert _clean_url_or_query("<https://x/a.png>") == "https://x/a.png"
    assert _clean_url_or_query("") == ""
    assert _clean_url_or_query(None) == ""


# ---- _extract_scdn_identifier ----


def test_extract_identifier_from_scdn_url():
    assert _extract_scdn_identifier("https://img.scdn.io/i/abc.webp") == "abc.webp"


def test_extract_identifier_strips_query():
    # #10: identifier 不应包含 query/fragment
    assert _extract_scdn_identifier("https://img.scdn.io/i/abc.webp?x=1") == "abc.webp"
    assert _extract_scdn_identifier("https://img.scdn.io/i/abc.webp#frag") == "abc.webp"


def test_extract_identifier_passthrough():
    assert _extract_scdn_identifier("abc123.png") == "abc123.png"


# ---- _is_url ----


def test_is_url():
    assert _is_url("https://a.com/x") is True
    assert _is_url("http://a.com") is True
    assert _is_url("ftp://a.com") is False
    assert _is_url("not a url") is False
    assert _is_url("") is False


# ---- _parse_upload_args (#8 / #9) ----


def test_parse_format_equals_form():
    url, extra, err = _parse_upload_args("--format=webp https://a.com/a.png")
    assert err == ""
    assert url == "https://a.com/a.png"
    assert extra["outputFormat"] == "webp"


def test_parse_format_space_form():  # #9
    url, extra, err = _parse_upload_args("--format webp https://a.com/a.png")
    assert err == ""
    assert extra["outputFormat"] == "webp"


def test_parse_password_empty_errors():  # #8
    _url, _extra, err = _parse_upload_args("--password= https://a.com/a.png")
    assert err == "密码不能为空"


def test_parse_password_set():
    _url, extra, err = _parse_upload_args("--password=secret https://a.com/a.png")
    assert err == ""
    assert extra["image_password"] == "secret"
    assert extra["password_enabled"] == "true"


def test_parse_password_too_long():
    _url, _extra, err = _parse_upload_args(f"--password={'x' * 200} https://a.com/a.png")
    assert "密码长度超过" in err


def test_parse_unknown_option_errors():
    _url, _extra, err = _parse_upload_args("--foo=bar https://a.com/a.png")
    assert err.startswith("未知参数")


def test_parse_option_missing_value():
    _url, _extra, err = _parse_upload_args("--format")
    assert "缺少值" in err


def test_parse_shlex_quoted_value():  # #9 引号包裹含空格值
    _url, extra, err = _parse_upload_args('--cdn="a.b.c" https://a.com/a.png')
    assert err == ""
    assert extra["cdn_domain"] == "a.b.c"


# ---- build_scdn_link_re (#10) ----


def test_scdn_re_drops_query():
    pat = build_scdn_link_re()
    m = pat.search("https://img.scdn.io/i/abc.webp?x=1#f")
    assert m is not None
    assert m.group(1) == "abc.webp"


def test_scdn_re_matches_all_domains():
    pat = build_scdn_link_re()
    for d in main._SCDN_DOMAINS:
        assert pat.search(f"https://{d}/i/x.png") is not None


def test_scdn_re_rejects_foreign_domain():
    pat = build_scdn_link_re()
    assert pat.search("https://evil.com/i/x.png") is None


# ---- strip_command_prefix (#3) ----


def test_strip_prefix_with_slash():
    assert strip_command_prefix("/图床上传 https://a.com", ("/图床上传", "图床上传")) == "https://a.com"


def test_strip_prefix_without_slash():
    assert strip_command_prefix("图床上传 https://a.com", ("/图床上传", "图床上传")) == "https://a.com"


def test_strip_prefix_no_match():
    assert strip_command_prefix("hello", ("/图床上传",)) == "hello"


# ---- _extract_image_url_or_path (#12) ----


def test_extract_ignores_local_file_cache():  # #12: file 缓存名不当 URL
    seg = {"type": "image", "data": {"file": "abc123hash.jpg"}}
    assert _extract_image_url_or_path(seg) is None


def test_extract_prefers_url_over_file():
    seg = {"type": "image", "data": {"url": "https://x/a.png", "file": "hash.jpg"}}
    assert _extract_image_url_or_path(seg) == "https://x/a.png"


def test_extract_file_when_it_is_url():
    seg = {"type": "image", "data": {"file": "https://x/a.png"}}
    assert _extract_image_url_or_path(seg) == "https://x/a.png"


def test_extract_nested_url():
    seg = {"type": "image", "data": {"subType": {"url": "https://x/a.png"}}}
    assert _extract_image_url_or_path(seg) == "https://x/a.png"


def test_looks_like_url_or_data():
    assert _looks_like_url_or_data("https://x") is True
    assert _looks_like_url_or_data("data:image/png;base64,") is True
    assert _looks_like_url_or_data("hash.jpg") is False
    assert _looks_like_url_or_data(None) is False
