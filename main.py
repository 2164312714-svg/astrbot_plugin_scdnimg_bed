import base64
import json
import os
import re
import shlex
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse

import aiohttp
from astrbot.api.all import AstrBotConfig, AstrMessageEvent, Context, Image, Plain, Star, logger
from astrbot.api.event import filter

__version__ = "v1.2.1"

# 下载图片字节的大小上限，防止恶意链接耗尽内存
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
# 图片访问密码长度上限
PASSWORD_MAX_LEN = 128

# 支持的全部 scdn CDN 域名；/图床解析 需识别其中任意一个，而非仅 img.scdn.io
# 注意：新增/删除域名需同步 _conf_schema.json 的 default_cdn_domain.hint 与
#       README.md 的“可选 CDN 域名”章节（见 scripts/check_sync.py 校验）
_SCDN_DOMAINS = (
    "img.scdn.io",
    "cloudflareimg.cdn.sn",
    "edgeoneimg.cdn.sn",
    "esaimg.cdn1.vip",
    "cloudflarecnimg.scdn.io",
    "anycastimg.scdn.io",
    "edgeoneimg.cdn1.vip",
)

# 命令前缀剥离用的别名列表（含带 / 与不带 / 形式）；与 @filter.command 的 alias 保持一致
_UPLOAD_ALIASES = ("/图床上传", "/上传图床", "/scdn-upload", "图床上传", "上传图床", "scdn-upload")
_URL_ALIASES = ("/图床链接", "/上传图床链接", "/scdn-url", "图床链接", "上传图床链接", "scdn-url")
_QUERY_ALIASES = ("/图床查询", "/查询图床", "/scdn-info", "图床查询", "查询图床", "scdn-info")
_PARSE_ALIASES = (
    "/图床解析",
    "/解析图床",
    "/scdn-parse",
    "/scdn-send",
    "图床解析",
    "解析图床",
    "scdn-parse",
    "scdn-send",
)
_MESSAGE_ARTIFACT_MARKERS = ("[引用消息(", "[回复消息(")


class ResponseParseError(RuntimeError):
    """图床响应 JSON 解析失败，携带原始响应体前缀便于排查。"""

    def __init__(self, message: str, body: str):
        super().__init__(message)
        self.body = body


def _get_message_chain(event: AstrMessageEvent) -> list:
    """获取消息链，兼容 event.get_messages() 和 event.message_obj.message。"""
    try:
        messages = event.get_messages()
    except Exception:
        try:
            messages = event.message_obj.message
        except Exception:
            return []
    if isinstance(messages, list):
        return messages
    if isinstance(messages, tuple):
        return list(messages)
    return []


def _is_image_segment(seg) -> bool:
    """判断消息段是否为图片，兼容 isinstance、type 字段和类名判断。"""
    if isinstance(seg, Image):
        return True
    if isinstance(seg, dict):
        return seg.get("type") in ("Image", "image")
    seg_type = getattr(seg, "type", None)
    if seg_type in ("Image", "image"):
        return True
    class_name = getattr(seg, "__class__", type(seg)).__name__
    return class_name == "Image"


def _looks_like_url_or_data(value) -> bool:
    """判断值是否是可直传的 http(s) URL 或 data URI。"""
    return isinstance(value, str) and (
        value.startswith("http://")
        or value.startswith("https://")
        or value.startswith("data:")
    )


def _extract_image_url_or_path(seg) -> str | None:
    """从图片消息段中提取 URL、文件路径或 base64 字符串。

    兼容 aiocqhttp/NapCat 不同版本的图片字段差异：对 data 下字段做多层下钻，
    提取失败时记录 logger.warning 以便定位适配问题。

    注意：file/path 字段在部分平台是本地缓存名（如 {hash}.jpg）而非可下载 URL，
    仅当其形似 http(s)/data 时才返回，避免被当成 URL 误传导致“不支持的图片地址”。
    """
    if isinstance(seg, dict):
        data = seg.get("data")
        if not isinstance(data, dict):
            data = {}
        # 一级：优先取可直接上传的 URL 字段
        for key in ("url", "image_url", "src"):
            value = data.get(key)
            if value:
                return value
        # file/path 仅当形似 URL/data 时才用（避免本地缓存名被误当 URL）
        for key in ("file", "path"):
            value = data.get(key)
            if _looks_like_url_or_data(value):
                return value
        # 二级：部分 NapCat 版本把 url/file 嵌套在 subType 等子对象下
        for sub in data.values():
            if not isinstance(sub, dict):
                continue
            for key in ("url", "image_url", "src"):
                value = sub.get(key)
                if value:
                    return value
            for key in ("file", "path"):
                value = sub.get(key)
                if _looks_like_url_or_data(value):
                    return value
        # 只记录 key 名不取值，避免泄露可能的敏感字段内容
        logger.warning(
            "未能从图片消息段(dict)提取 URL/路径，data 字段 keys：%s",
            list(data.keys()) if isinstance(data, dict) else data,
        )
        return None
    for attr in ("url", "image_url", "src"):
        value = getattr(seg, attr, None)
        if value:
            return value
    for attr in ("file", "path"):
        value = getattr(seg, attr, None)
        if _looks_like_url_or_data(value):
            return value
    logger.warning("未能从图片消息段(%s)提取 URL/路径", type(seg).__name__)
    return None


def _clean_url_or_query(text: str | None) -> str:
    """清理 URL/查询字符串，去除首尾空白、Markdown 反引号、尖括号等常见包裹符号。"""
    if not text:
        return ""
    text = text.strip()
    # 去除成对的反引号、尖括号、方括号、圆括号
    for pair in (("`", "`"), ("<", ">"), ("[", "]"), ('"', '"'), ("'", "'")):
        if text.startswith(pair[0]) and text.endswith(pair[1]):
            text = text[1:-1].strip()
            break
    return text


def _extract_scdn_identifier(text: str) -> str:
    """如果传入的是 scdn 图片 URL，则提取文件名/标识符，否则原样返回。"""
    text = _clean_url_or_query(text)
    try:
        parsed = urlparse(text)
        if parsed.scheme in ("http", "https") and parsed.path:
            # 匹配 /i/<filename> 或 /<filename>
            parts = [p for p in parsed.path.split("/") if p]
            if parts:
                return parts[-1]
    except Exception:
        pass
    return text


def _is_url(text: str) -> bool:
    try:
        parsed = urlparse(text)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def _is_data_uri(text: str) -> bool:
    return isinstance(text, str) and text.lower().startswith("data:")


def _decode_data_uri(data_uri: str) -> bytes | None:
    """解码 base64 data URI，失败返回 None。"""
    meta, sep, encoded = data_uri.partition(",")
    if not sep or not encoded or ";base64" not in meta.lower():
        return None
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except Exception:
        logger.warning("解码图片 data URI 失败", exc_info=True)
        return None
    if len(image_bytes) > MAX_DOWNLOAD_BYTES:
        logger.warning("图片 data URI 超过上限 %s 字节", MAX_DOWNLOAD_BYTES)
        return None
    return image_bytes


def strip_command_prefix(raw_text: str, aliases) -> str:
    """从 raw_text 中剥离命令前缀（兼容带 / 和不带 / 的情况）。"""
    for prefix in aliases:
        if raw_text.startswith(prefix):
            return raw_text[len(prefix):].strip()
    return raw_text


def _plain_text_from_segment(seg) -> str:
    """从 Plain 消息段提取文本，跳过 Reply/Image 等非用户参数段。"""
    if isinstance(seg, Plain):
        return getattr(seg, "text", "") or ""

    if isinstance(seg, dict):
        if seg.get("type") not in ("Plain", "plain", "text"):
            return ""
        data = seg.get("data")
        if isinstance(data, dict):
            return str(data.get("text") or data.get("content") or "")
        return str(data or seg.get("text") or "")

    seg_type = getattr(seg, "type", None)
    class_name = getattr(seg, "__class__", type(seg)).__name__
    if seg_type in ("Plain", "plain", "text") or class_name == "Plain":
        return str(getattr(seg, "text", None) or getattr(seg, "content", None) or "")
    return ""


def _strip_message_artifacts(raw_text: str) -> str:
    """去掉 get_message_str 里混入的引用/回复展示串，避免被当成命令参数。"""
    text = raw_text.strip()
    artifact_positions = [
        pos for marker in _MESSAGE_ARTIFACT_MARKERS if (pos := text.find(marker)) >= 0
    ]
    if artifact_positions:
        text = text[: min(artifact_positions)].strip()
    return text


def _clean_command_arg_text(raw_text: str | None, aliases) -> str:
    """清理一段命令文本：剥离命令前缀和框架消息展示串。"""
    return _strip_message_artifacts(strip_command_prefix((raw_text or "").strip(), aliases))


def _get_command_arg_text(event: AstrMessageEvent, aliases) -> str:
    """提取命令后的纯文本参数，优先使用消息链中的 Plain 段。"""
    plain_parts = [
        text for seg in _get_message_chain(event) if (text := _plain_text_from_segment(seg))
    ]
    raw_text = " ".join(plain_parts).strip()
    if not raw_text:
        try:
            raw_text = event.get_message_str().strip()
        except Exception:
            raw_text = ""
    return _clean_command_arg_text(raw_text, aliases)


def build_scdn_link_re(default_cdn_domain: str = "") -> "re.Pattern[str]":
    """构建匹配 scdn 图片链接的正则，捕获组为标识符（不含 query/fragment）。"""
    domains = set(_SCDN_DOMAINS)
    if default_cdn_domain:
        domains.add(default_cdn_domain)
    domain_alt = "|".join(
        re.escape(d) for d in sorted(domains, key=len, reverse=True)
    )
    return re.compile(rf"https?://(?:{domain_alt})/i/([^/?#\s]+)")


def _parse_upload_args(raw_text: str) -> tuple[str, dict[str, str], str]:
    """解析上传命令参数，返回 (url_or_empty, extra_dict, error_msg)。

    支持 --k=v 与 --k v 两种形式（后者借助 shlex.split 支持引号包裹）。
    """
    try:
        tokens = shlex.split(raw_text) if raw_text else []
    except ValueError as e:
        return "", {}, f"参数解析失败：{e}"

    extra: dict[str, str] = {}
    url = ""
    option_map = {
        "--format": "outputFormat",
        "--cdn": "cdn_domain",
        "--storage": "storage_destination",
    }

    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("--"):
            # --k=v 形式
            if "=" in token:
                key, _, value = token.partition("=")
            else:
                # --k v 形式
                key = token
                if i + 1 >= len(tokens):
                    return "", {}, f"参数 {token} 缺少值"
                value = tokens[i + 1]
                i += 1
            if key == "--password":
                if not value:
                    return "", {}, "密码不能为空"
                if len(value) > PASSWORD_MAX_LEN:
                    return "", {}, f"密码长度超过 {PASSWORD_MAX_LEN}"
                extra["password_enabled"] = "true"
                extra["image_password"] = value
            elif key in option_map:
                if not value:
                    return "", {}, f"{key} 的值不能为空"
                extra[option_map[key]] = value
            else:
                return "", {}, f"未知参数: {token}"
        elif not url and (
            _is_url(_clean_url_or_query(token)) or _is_data_uri(_clean_url_or_query(token))
        ):
            url = _clean_url_or_query(token)
        else:
            return "", {}, f"无法识别的参数: {token}"
        i += 1

    return url, extra, ""


async def _seg_to_bytes(seg) -> tuple[str | None, bytes | None, str | None]:
    """从单个图片段提取 (url, bytes, filename)，先取 URL 再 base64 兜底。

    把“先拿 URL、拿不到再 base64 兜底”的逻辑扁平化，供 upload_image 复用。
    """
    url = _extract_image_url_or_path(seg)
    if url:
        return url, None, None

    b64 = None
    if isinstance(seg, dict):
        data = seg.get("data")
        if isinstance(data, dict):
            b64 = data.get("base64") or data.get("b64")
    else:
        if isinstance(seg, Image):
            try:
                b64 = await seg.convert_to_base64()
            except Exception:
                logger.warning("从图片段转换 base64 失败", exc_info=True)
        if not b64:
            b64 = getattr(seg, "base64", None) or getattr(seg, "b64", None)
    if b64:
        try:
            return None, base64.b64decode(b64), "image.bin"
        except Exception:
            logger.warning("解码图片 base64 失败", exc_info=True)
    return None, None, None


class ScdnImgBedPlugin(Star):
    """基于 img.scdn.io 的 AstrBot 图床插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.api_base_url: str = config.get("api_base_url", "https://img.scdn.io/api/v1.php")
        self.default_cdn_domain: str = config.get("default_cdn_domain", "img.scdn.io")
        self.default_storage: str = config.get("default_storage", "local")
        self.default_output_format: str = config.get("default_output_format", "auto")
        self.timeout: int = config.get("timeout", 60)
        self.session: aiohttp.ClientSession | None = None
        # /图床解析 需识别全部已配置 CDN 域名，而不止 img.scdn.io
        self._scdn_link_re = build_scdn_link_re(self.default_cdn_domain)

    def _new_session(self) -> aiohttp.ClientSession:
        """统一创建带连接池、默认 UA 与超时的 ClientSession。"""
        return aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=16),
            headers={"User-Agent": f"scdnimg-bed/{__version__}"},
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        )

    async def initialize(self) -> None:
        # 预热一次 session，统一经 _http_session() 取用
        self.session = self._new_session()
        logger.info("scdnimg-bed 插件已初始化")

    async def terminate(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("scdnimg-bed 插件会话已关闭")

    def _http_session(self) -> aiohttp.ClientSession:
        # 唯一的 session 取用入口：为空或已关闭则重建
        if self.session is None or self.session.closed:
            self.session = self._new_session()
        return self.session

    def _build_upload_payload(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        data: dict[str, str] = {}
        if self.default_cdn_domain:
            data["cdn_domain"] = self.default_cdn_domain
        if self.default_storage:
            data["storage_destination"] = self.default_storage
        if self.default_output_format:
            data["outputFormat"] = self.default_output_format
        if extra:
            # 命令行显式参数覆盖默认值
            data.update(extra)
        return data

    @staticmethod
    def _format_upload_result(result: dict[str, Any]) -> str:
        data = result.get("data", {})
        url = result.get("url") or data.get("url") or data.get("image_url") or ""
        lines = ["上传成功！", f"URL: {url}"]
        filename = data.get("filename")
        if filename:
            lines.append(f"文件名: {filename}")
        storage = data.get("storage_backend")
        if storage:
            lines.append(f"存储: {storage}")
        original = data.get("original_size")
        compressed = data.get("compressed_size")
        ratio = data.get("compression_ratio")
        if original is not None and compressed is not None:
            lines.append(f"大小: {original} -> {compressed}")
        if ratio is not None:
            lines.append(f"压缩比: {ratio}%")
        message = result.get("message") or data.get("message")
        if message and "秒传" in message:
            lines.append(f"提示: {message}")
        return "\n".join(lines)

    @staticmethod
    def _format_query_result(result: dict[str, Any]) -> str:
        data = result.get("data", {})
        lines = ["图片信息："]
        fields = [
            ("ID", "id"),
            ("文件名", "filename"),
            ("原始文件名", "original_filename"),
            ("大小", "size_display"),
            ("上传时间", "upload_date"),
            ("上传者", "uploader_masked"),
            ("归属地", "location"),
            ("标签", "tags"),
            ("画面描述", "content_description"),
            ("存储后端", "storage_backend"),
            ("存储位置", "storage_location"),
            ("图片URL", "image_url"),
            ("CDN域名", "cdn_domain"),
            ("密码保护", "password_protected"),
        ]
        for label, key in fields:
            value = data.get(key)
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                value = "是" if value else "否"
            lines.append(f"{label}: {value}")
        return "\n".join(lines)

    async def _request_json(self, method: str, **kwargs) -> dict[str, Any]:
        """统一发起 HTTP 请求并解析 JSON，超时由 session 级配置统一管理。"""
        async with self._http_session().request(
            method, self.api_base_url, **kwargs
        ) as resp:
            resp.raise_for_status()
            text = await resp.text()
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                logger.error("图床响应 JSON 解析失败，body[:200]=%s", text[:200])
                raise ResponseParseError(f"解析响应失败: {e}", text) from e

    async def _upload_file(
        self, file_bytes: bytes, filename: str, extra: dict[str, str]
    ) -> dict[str, Any]:
        data = self._build_upload_payload(extra)
        form = aiohttp.FormData()
        form.add_field(
            "image", file_bytes, filename=filename, content_type="application/octet-stream"
        )
        for k, v in data.items():
            form.add_field(k, v)
        return await self._request_json("POST", data=form)

    async def _upload_by_url(self, image_url: str, extra: dict[str, str]) -> dict[str, Any]:
        data = self._build_upload_payload(extra)
        data["image_url"] = _clean_url_or_query(image_url)
        return await self._request_json("POST", data=data)

    async def _query_image(self, query: str) -> dict[str, Any]:
        params = {"q": _extract_scdn_identifier(query)}
        return await self._request_json("GET", params=params)

    async def _call_and_reply(
        self,
        event: AstrMessageEvent,
        coro,
        fmt_ok,
        fail_msg: str = "操作失败，请稍后重试。",
    ) -> AsyncGenerator[Any, None]:
        """调用图床 API 并把结果格式化后回复，统一异常分流与错误信息上抛。

        成功时 fmt_ok(result) 返回 str（纯文本）或 list（消息链，走 chain_result）。
        """
        try:
            result = await coro
        except aiohttp.ClientResponseError as e:
            logger.error("图床 HTTP 错误 status=%s", e.status, extra={"status": e.status})
            yield event.plain_result(f"图床返回 {e.status}：{e.message}")
            return
        except ResponseParseError as e:
            logger.error("图床响应解析失败", exc_info=True)
            yield event.plain_result(f"图床响应解析失败：{str(e)[:200]}")
            return
        except Exception:
            logger.error("API 调用失败", exc_info=True)
            yield event.plain_result(fail_msg)
            return
        if result.get("success"):
            ok = fmt_ok(result)
            if isinstance(ok, list):
                yield event.chain_result(ok)
            else:
                yield event.plain_result(ok)
        else:
            err = result.get("message") or result.get("error") or "未知错误"
            yield event.plain_result(f"操作失败：{err}")

    async def _download_bytes(self, url: str) -> bytes | None:
        """下载 URL 内容为字节（含大小封顶），供本地处理后上传。

        用于把含凭据（如 Telegram bot token）的下载链接在本地拉取，
        避免把该 URL 外发给第三方图床 API 造成凭据泄露。
        """
        try:
            async with self._http_session().get(url) as resp:
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(1 << 16):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        logger.warning("下载内容超过上限 %s 字节，已取消", MAX_DOWNLOAD_BYTES)
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except Exception:
            logger.error("下载图片字节失败", exc_info=True)
            return None

    async def _extract_reply_image(
        self, event: AstrMessageEvent
    ) -> tuple[str | None, bytes | None, str | None]:
        """尝试从引用/回复的消息中提取图片。返回 (url, bytes, filename)。

        filename 仅在返回 bytes 时有意义（可为 None，由调用方用默认名）。
        兼容 aiocqhttp（QQ/NapCat）、Telegram 等常见平台，其余平台会尝试从 raw_message 推断。
        """
        message_chain = _get_message_chain(event)

        reply_id = None
        for seg in message_chain:
            # 兼容 dict 形式（aiocqhttp raw message 段）与对象形式
            if isinstance(seg, dict):
                if seg.get("type") not in ("Reply", "reply"):
                    continue
                data = seg.get("data")
                reply_id = data.get("id") if isinstance(data, dict) else seg.get("id")
            else:
                if getattr(seg, "type", None) not in ("Reply", "reply"):
                    continue
                reply_id = getattr(seg, "id", None)
            if reply_id:
                break

        try:
            platform = event.get_platform_name()
        except Exception:
            platform = ""

        # aiocqhttp / QQ / NapCat：通过协议端 API 获取原消息
        if reply_id and platform == "aiocqhttp":
            try:
                bot = getattr(event, "bot", None)
                api = getattr(bot, "api", None)
                if api is not None:
                    # dict 段取到的 id 常为字符串，OneBot v11 get_msg 需 int
                    try:
                        msg_id = int(reply_id)
                    except (TypeError, ValueError):
                        msg_id = reply_id
                    result = await api.call_action("get_msg", message_id=msg_id)
                    if isinstance(result, dict):
                        reply_message = result.get("message", [])
                        if isinstance(reply_message, str):
                            try:
                                reply_message = json.loads(reply_message)
                            except Exception:
                                reply_message = []
                        url = self._extract_first_image_url(reply_message)
                        if url:
                            return url, None, None
                        b64 = self._extract_first_image_base64(reply_message)
                        if b64:
                            return None, base64.b64decode(b64), None
            except Exception:
                logger.error("aiocqhttp 提取引用消息图片失败", exc_info=True)

        # Telegram：从 raw_message.reply_to_message 中提取最大尺寸图片
        if platform == "telegram":
            try:
                raw = getattr(event.message_obj, "raw_message", None)
                if raw and isinstance(raw, dict):
                    reply_to = raw.get("reply_to_message") or {}
                    photos = reply_to.get("photo", [])
                    if photos:
                        largest = max(photos, key=lambda p: p.get("file_size", 0))
                        file_id = largest.get("file_id")
                        if file_id:
                            bot = getattr(event, "bot", None)
                            if bot and hasattr(bot, "get_file"):
                                file_obj = await bot.get_file(file_id)
                                file_path = getattr(file_obj, "file_path", None)
                                if file_path:
                                    bot_session = getattr(bot, "session", None)
                                    token = getattr(bot_session, "api_token", None)
                                    if token:
                                        # 本地下载后上传，避免含 bot token 的 URL 外发给第三方图床
                                        dl_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
                                        image_bytes = await self._download_bytes(dl_url)
                                        if image_bytes:
                                            ext = os.path.splitext(file_path)[1].lower() or ".jpg"
                                            return None, image_bytes, f"telegram{ext}"
            except Exception:
                logger.error("Telegram 提取引用消息图片失败", exc_info=True)

        # 通用兜底：尝试从 raw_message 的常见字段找回复消息中的图片
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            if raw and isinstance(raw, dict):
                for key in ("reply_to_message", "reply", "quoted_message", "source"):
                    replied = raw.get(key)
                    if not replied:
                        continue
                    if isinstance(replied, dict):
                        # 可能是消息对象或消息链
                        url = self._extract_first_image_url(replied.get("message", []))
                        if url:
                            return url, None, None
                        url = self._extract_first_image_url(replied)
                        if url:
                            return url, None, None
                    elif isinstance(replied, list):
                        url = self._extract_first_image_url(replied)
                        if url:
                            return url, None, None
        except Exception:
            logger.error("通用提取引用消息图片失败", exc_info=True)

        return None, None, None

    @staticmethod
    def _extract_first_image_url(message_chain) -> str | None:
        """从消息链（dict/list 混合）中提取第一张图片的 URL 或路径。"""
        if isinstance(message_chain, dict):
            message_chain = message_chain.get("message", [])
        if not isinstance(message_chain, (list, tuple)):
            return None
        for seg in message_chain:
            if not _is_image_segment(seg):
                continue
            value = _extract_image_url_or_path(seg)
            if value:
                return value
        return None

    @staticmethod
    def _extract_first_image_base64(message_chain) -> str | None:
        """从消息链中提取第一张图片的 base64 字符串。"""
        if isinstance(message_chain, dict):
            message_chain = message_chain.get("message", [])
        if not isinstance(message_chain, (list, tuple)):
            return None
        for seg in message_chain:
            if not _is_image_segment(seg):
                continue
            if isinstance(seg, dict):
                data = seg.get("data", {})
                if not isinstance(data, dict):
                    continue
                b64 = data.get("base64") or data.get("b64")
                if b64:
                    return b64
            b64 = getattr(seg, "base64", None) or getattr(seg, "b64", None)
            if b64:
                return b64
        return None

    @filter.command("图床上传", alias={"上传图床", "scdn-upload"})
    async def upload_image(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """上传图片到 scdn 图床。支持回复图片、附带图片或提供图片 URL。
        用法：
          /图床上传（必须附带图片或回复图片消息）
          /图床上传 <图片URL> [--format=webp] [--storage=local]
          [--cdn=域名] [--password=密码]
        """
        raw_text = _get_command_arg_text(event, _UPLOAD_ALIASES)

        arg_url, extra, error = _parse_upload_args(raw_text)
        if error:
            yield event.plain_result(
                f"参数错误：{error}\n用法：/图床上传 [图片URL] [--format=webp] "
                "[--cdn=img.scdn.io] [--storage=local] [--password=密码]"
            )
            return

        # 优先从消息链/回复中提取图片
        image_url = None
        image_bytes = None
        image_filename = None

        for seg in _get_message_chain(event):
            if not _is_image_segment(seg):
                continue
            url, bts, fname = await _seg_to_bytes(seg)
            if url:
                image_url = url
                break
            if bts:
                image_bytes = bts
                image_filename = fname or "image.bin"
                break

        # 尝试从引用/回复消息中提取图片
        if image_url is None and image_bytes is None:
            reply_url, reply_bytes, reply_filename = await self._extract_reply_image(event)
            if reply_url:
                image_url = reply_url
            elif reply_bytes:
                image_bytes = reply_bytes
                image_filename = reply_filename or "image.bin"

        if image_url is None and image_bytes is None:
            # 没有附带图片，看看命令参数里是否提供了图片 URL 或 data URI
            if arg_url:
                if _is_data_uri(arg_url):
                    image_bytes = _decode_data_uri(arg_url)
                    if not image_bytes:
                        yield event.plain_result("无法解析图片 data URI，请检查 base64 数据。")
                        return
                    yield event.plain_result("正在上传图片，请稍候...")
                    async for msg in self._call_and_reply(
                        event,
                        self._upload_file(image_bytes, "image.bin", extra),
                        self._format_upload_result,
                        "上传失败，请检查网络或图片后重试。",
                    ):
                        yield msg
                    return
                yield event.plain_result("正在通过 URL 上传图片，请稍候...")
                async for msg in self._call_and_reply(
                    event,
                    self._upload_by_url(arg_url, extra),
                    self._format_upload_result,
                    "上传失败，请检查网络或图片链接后重试。",
                ):
                    yield msg
                return

            yield event.plain_result(
                "请发送/回复一张图片，或提供图片 URL。\n用法：/图床上传 [图片URL] "
                "[--format=webp] [--cdn=img.scdn.io] [--storage=local] [--password=密码]"
            )
            return

        # 有图片 URL 或二进制
        if image_url:
            if _is_url(image_url):
                yield event.plain_result("正在上传图片，请稍候...")
                async for msg in self._call_and_reply(
                    event,
                    self._upload_by_url(image_url, extra),
                    self._format_upload_result,
                    "上传失败，请检查网络或图片后重试。",
                ):
                    yield msg
            elif image_url.startswith("data:"):
                # 部分平台可能给的是 base64 data URI。
                image_bytes = _decode_data_uri(image_url)
                if not image_bytes:
                    yield event.plain_result("无法解析图片数据，请尝试重新发送图片。")
                    return
                image_filename = "image.bin"
                yield event.plain_result("正在上传图片，请稍候...")
                async for msg in self._call_and_reply(
                    event,
                    self._upload_file(image_bytes, image_filename, extra),
                    self._format_upload_result,
                    "上传失败，请检查网络或图片后重试。",
                ):
                    yield msg
            else:
                yield event.plain_result(
                    "不支持的图片地址，请提供 HTTP/HTTPS 链接或 base64 data URI。"
                )
        else:
            # 无 URL 时必须有有效字节，否则提前报错，避免上传空字节得到无意义 400
            if not image_bytes:
                yield event.plain_result("未获取到图片内容，请重新发送图片。")
                return
            yield event.plain_result("正在上传图片，请稍候...")
            async for msg in self._call_and_reply(
                event,
                self._upload_file(image_bytes, image_filename or "image.bin", extra),
                self._format_upload_result,
                "上传失败，请检查网络或图片后重试。",
            ):
                yield msg

    @filter.command("图床链接", alias={"上传图床链接", "scdn-url"})
    async def upload_image_url(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """通过图片 URL 上传到 scdn 图床。
        用法：/图床链接 <图片URL> [--format=webp] [--cdn=img.scdn.io] [--storage=local]
        """
        raw_text = _get_command_arg_text(event, _URL_ALIASES)

        arg_url, extra, error = _parse_upload_args(raw_text)
        if error:
            yield event.plain_result(
                f"参数错误：{error}\n用法：/图床链接 <图片URL> "
                "[--format=webp] [--cdn=img.scdn.io] [--storage=local]"
            )
            return
        arg_url = _clean_url_or_query(arg_url)
        if not arg_url:
            yield event.plain_result(
                "请提供图片 URL。\n用法：/图床链接 <图片URL> "
                "[--format=webp] [--cdn=img.scdn.io] [--storage=local]"
            )
            return
        if not _is_url(arg_url):
            yield event.plain_result(
                "图床链接仅支持 HTTP/HTTPS 图片链接；data URI 请使用 /图床上传。"
            )
            return

        yield event.plain_result("正在通过 URL 上传图片，请稍候...")
        async for msg in self._call_and_reply(
            event,
            self._upload_by_url(arg_url, extra),
            self._format_upload_result,
            "上传失败，请检查网络或图片链接后重试。",
        ):
            yield msg

    @filter.command("图床查询", alias={"查询图床", "scdn-info"})
    async def query_image(
        self, event: AstrMessageEvent, query: str = ""
    ) -> AsyncGenerator[Any, None]:
        """查询 scdn 图床图片公开元数据。
        用法：/图床查询 <图片ID或文件名>
        """
        query = query or _get_command_arg_text(event, _QUERY_ALIASES)
        query = _extract_scdn_identifier(_clean_command_arg_text(query, _QUERY_ALIASES))
        if not query:
            yield event.plain_result(
                "请提供图片 ID 或完整文件名。\n用法：/图床查询 <图片ID或文件名>"
            )
            return

        yield event.plain_result("正在查询图片信息...")
        async for msg in self._call_and_reply(
            event,
            self._query_image(query),
            self._format_query_result,
            "查询失败，请稍后重试。",
        ):
            yield msg

    @filter.command("图床解析", alias={"解析图床", "scdn-parse", "scdn-send"})
    async def parse_scdn_link(
        self, event: AstrMessageEvent, url: str = ""
    ) -> AsyncGenerator[Any, None]:
        """解析 scdn 图片链接并将图片发送到群里。
        用法：/图床解析 <scdn图片URL>
        """
        url = url or _get_command_arg_text(event, _PARSE_ALIASES)
        url = _clean_command_arg_text(url, _PARSE_ALIASES)
        url = _clean_url_or_query(url)
        if not url:
            yield event.plain_result("请提供 scdn 图片链接。\n用法：/图床解析 <图片URL>")
            return

        match = self._scdn_link_re.search(url)
        if not match:
            yield event.plain_result("请输入有效的 scdn 图片链接，如：https://img.scdn.io/i/xxx.webp")
            return

        scdn_url = match.group(0)
        identifier = match.group(1)

        def _fmt_parse(result: dict[str, Any]) -> list:
            data = result.get("data", {})
            image_url = data.get("image_url") or data.get("url") or scdn_url
            # 构建回复链：图片 + 简要信息
            chain = [Image.fromURL(image_url)]
            caption_parts = []
            filename = data.get("filename")
            if filename:
                caption_parts.append(f"文件名: {filename}")
            size = data.get("size_display")
            if size:
                caption_parts.append(f"大小: {size}")
            if caption_parts:
                chain.insert(0, Plain("\n".join(caption_parts)))
            return chain

        yield event.plain_result("正在解析图片链接...")
        async for msg in self._call_and_reply(
            event,
            self._query_image(identifier),
            _fmt_parse,
            "解析失败，请稍后重试。",
        ):
            yield msg

    @filter.command("图床帮助", alias={"scdn-help"})
    async def help_cmd(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """显示 scdn 图床插件帮助。"""
        help_text = """scdn 图床插件帮助：

/图床上传 [图片URL] [--format=webp] [--cdn=img.scdn.io] [--storage=local] [--password=密码]
  上传图片。可附带图片、回复图片消息，或提供图片 URL。

/图床链接 <图片URL> [--format=webp] [--cdn=img.scdn.io] [--storage=local]
  通过远程 URL 上传图片。

/图床查询 <图片ID或文件名>
  查询图片公开元数据。

/图床解析 <scdn图片URL>
  解析 scdn 图片链接并将图片发送到群里。

可用存储：local / telegram / r2
可用输出格式：auto / jpg / jpeg / png / webp / gif / webp_animated
"""
        yield event.plain_result(help_text)
