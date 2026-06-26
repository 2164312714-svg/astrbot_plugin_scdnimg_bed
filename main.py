import os
import re
import json
import base64
import aiohttp
from typing import Optional, Dict, Any
from urllib.parse import urlparse

from astrbot.api.event import filter
from astrbot.api.all import Star, Context, AstrBotConfig, logger, AstrMessageEvent
from astrbot.api.all import Plain, Image


def _get_message_chain(event: AstrMessageEvent):
    """获取消息链，兼容 event.get_messages() 和 event.message_obj.message。"""
    try:
        return event.get_messages()
    except Exception:
        try:
            return event.message_obj.message
        except Exception:
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


def _extract_image_url_or_path(seg):
    """从图片消息段中提取 URL、文件路径或 base64 字符串。"""
    _URL_KEYS = ("url", "file", "path", "image_url", "src")

    def _search_dict(d):
        if not isinstance(d, dict):
            return None
        for key in _URL_KEYS:
            value = d.get(key)
            if value:
                return value
        # 递归一层以兼容部分适配器的嵌套字段（如 NapCat 的 subType 包裹）
        for value in d.values():
            if isinstance(value, dict):
                found = _search_dict(value)
                if found:
                    return found
        return None

    if isinstance(seg, dict):
        # 优先查 seg 顶层（部分实现把 url 直接放在段上）
        value = _search_dict(seg)
        if value:
            return value
        # 再查 data 子字段
        data = seg.get("data", {})
        return _search_dict(data)
    for attr in _URL_KEYS:
        value = getattr(seg, attr, None)
        if value:
            return value
    return None


def _clean_url_or_query(text: str) -> str:
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


_SCDN_HOSTS = (
    "img.scdn.io",
    "cloudflareimg.cdn.sn",
    "edgeoneimg.cdn.sn",
    "esaimg.cdn1.vip",
    "cloudflarecnimg.scdn.io",
    "anycastimg.scdn.io",
    "edgeoneimg.cdn1.vip",
)
_SCDN_LINK_RE = re.compile(
    r"https?://(?:" + "|".join(re.escape(h) for h in _SCDN_HOSTS) + r")/i/([^/\s]+)"
)


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
        self.local_upload_enabled: bool = bool(config.get("local_upload_enabled", False))
        self.local_upload_root: str = config.get("local_upload_root", "") or ""
        self.session: Optional[aiohttp.ClientSession] = None

    async def initialize(self) -> None:
        self.session = aiohttp.ClientSession()
        logger.info("scdnimg-bed 插件已初始化")

    async def terminate(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("scdnimg-bed 插件会话已关闭")

    def _http_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    def _build_upload_payload(self, extra: Dict[str, str] = None) -> Dict[str, str]:
        data: Dict[str, str] = {}
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
    def _format_upload_result(result: Dict[str, Any]) -> str:
        url = result.get("url", "")
        data = result.get("data", {})
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
    def _format_query_result(result: Dict[str, Any]) -> str:
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

    async def _upload_file(self, file_bytes: bytes, filename: str, extra: Dict[str, str]) -> Dict[str, Any]:
        data = self._build_upload_payload(extra)
        form = aiohttp.FormData()
        form.add_field("image", file_bytes, filename=filename, content_type="application/octet-stream")
        for k, v in data.items():
            form.add_field(k, v)

        async with self._http_session().post(
            self.api_base_url,
            data=form,
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        ) as resp:
            resp.raise_for_status()
            text = await resp.text()
            try:
                json_data = json.loads(text)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"解析响应失败: {e}\n{text[:500]}") from e
            return json_data

    async def _upload_by_url(self, image_url: str, extra: Dict[str, str]) -> Dict[str, Any]:
        data = self._build_upload_payload(extra)
        data["image_url"] = _clean_url_or_query(image_url)

        async with self._http_session().post(
            self.api_base_url,
            data=data,
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        ) as resp:
            resp.raise_for_status()
            text = await resp.text()
            try:
                json_data = json.loads(text)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"解析响应失败: {e}\n{text[:500]}") from e
            return json_data

    async def _query_image(self, query: str) -> Dict[str, Any]:
        params = {"q": _extract_scdn_identifier(query)}
        async with self._http_session().get(
            self.api_base_url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        ) as resp:
            resp.raise_for_status()
            text = await resp.text()
            try:
                json_data = json.loads(text)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"解析响应失败: {e}\n{text[:500]}") from e
            return json_data

    def _parse_upload_args(self, raw_text: str) -> tuple:
        """解析上传命令参数，返回 (url_or_empty, extra_dict, error_msg)。"""
        tokens = raw_text.strip().split() if raw_text else []
        extra: Dict[str, str] = {}
        url = ""

        for token in tokens:
            if token.startswith("--format="):
                extra["outputFormat"] = token.split("=", 1)[1]
            elif token.startswith("--cdn="):
                extra["cdn_domain"] = token.split("=", 1)[1]
            elif token.startswith("--storage="):
                extra["storage_destination"] = token.split("=", 1)[1]
            elif token.startswith("--password="):
                pwd = token.split("=", 1)[1]
                if pwd:
                    extra["password_enabled"] = "true"
                    extra["image_password"] = pwd
            elif token.startswith("--"):
                return "", {}, f"未知参数: {token}"
            elif not url and self._is_url(_clean_url_or_query(token)):
                url = _clean_url_or_query(token)
            else:
                return "", {}, f"无法识别的参数: {token}"

        return url, extra, ""

    @staticmethod
    def _is_url(text: str) -> bool:
        try:
            parsed = urlparse(text)
            return parsed.scheme in ("http", "https") and bool(parsed.netloc)
        except Exception:
            return False

    async def _extract_reply_image(self, event: AstrMessageEvent) -> tuple:
        """尝试从引用/回复的消息中提取图片。返回 (url, bytes)。
        兼容 aiocqhttp（QQ/NapCat）、Telegram 等常见平台，其余平台会尝试从 raw_message 推断。
        """
        message_chain = _get_message_chain(event)

        reply_id = None
        for seg in message_chain:
            if isinstance(seg, dict):
                seg_type = seg.get("type")
                seg_data = seg.get("data")
                if isinstance(seg_data, dict):
                    seg_id = seg_data.get("id") or seg.get("id")
                else:
                    seg_id = seg.get("id")
            else:
                seg_type = getattr(seg, "type", None)
                seg_id = getattr(seg, "id", None)
            if seg_type in ("Reply", "reply") and seg_id:
                reply_id = seg_id
                break

        platform = event.get_platform_name()

        # aiocqhttp / QQ / NapCat：通过协议端 API 获取原消息
        if reply_id and platform == "aiocqhttp":
            try:
                bot = getattr(event, "bot", None)
                api = getattr(bot, "api", None)
                if api is not None:
                    result = await api.call_action("get_msg", message_id=reply_id)
                    if isinstance(result, dict):
                        reply_message = result.get("message", [])
                        if isinstance(reply_message, str):
                            try:
                                reply_message = json.loads(reply_message)
                            except Exception:
                                reply_message = []
                        url = self._extract_first_image_url(reply_message)
                        if url:
                            return url, None
                        b64 = self._extract_first_image_base64(reply_message)
                        if b64:
                            return None, base64.b64decode(b64)
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
                                    token = getattr(getattr(bot, "session", None), "api_token", None)
                                    if token:
                                        return f"https://api.telegram.org/file/bot{token}/{file_path}", None
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
                            return url, None
                        url = self._extract_first_image_url(replied)
                        if url:
                            return url, None
                    elif isinstance(replied, list):
                        url = self._extract_first_image_url(replied)
                        if url:
                            return url, None
        except Exception:
            logger.error("通用提取引用消息图片失败", exc_info=True)

        return None, None

    @staticmethod
    def _extract_first_image_url(message_chain) -> Optional[str]:
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
    def _extract_first_image_base64(message_chain) -> Optional[str]:
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
                b64 = data.get("base64") or data.get("b64")
                if b64:
                    return b64
            b64 = getattr(seg, "base64", None) or getattr(seg, "b64", None)
            if b64:
                return b64
        return None

    @filter.command("图床上传", alias={"上传图床", "scdn-upload"})
    async def upload_image(self, event: AstrMessageEvent):
        """上传图片到 scdn 图床。支持回复图片、附带图片或提供图片 URL。
        用法：
          /图床上传（必须附带图片或回复图片消息）
          /图床上传 <图片URL> [--format=webp] [--cdn=img.scdn.io] [--storage=local] [--password=密码]
        """
        raw_text = event.get_message_str().strip()
        # 去掉命令本身（兼容带 / 和不带 / 的情况）
        for prefix in ("/图床上传", "/上传图床", "/scdn-upload", "图床上传", "上传图床", "scdn-upload"):
            if raw_text.startswith(prefix):
                raw_text = raw_text[len(prefix):].strip()
                break

        arg_url, extra, error = self._parse_upload_args(raw_text)
        if error:
            yield event.plain_result(f"参数错误：{error}\n用法：/图床上传 [图片URL] [--format=webp] [--cdn=img.scdn.io] [--storage=local] [--password=密码]")
            return

        # 优先从消息链/回复中提取图片
        image_url = None
        image_bytes = None
        image_filename = None

        for seg in _get_message_chain(event):
            if not _is_image_segment(seg):
                continue

            value = _extract_image_url_or_path(seg)
            if value:
                image_url = value
                break

            # 没有 url，尝试 base64 兜底
            if isinstance(seg, Image):
                try:
                    b64 = await seg.convert_to_base64()
                except Exception:
                    logger.debug("convert_to_base64 失败，尝试直接读取 base64 字段", exc_info=True)
                    b64 = None
                if b64:
                    try:
                        image_bytes = base64.b64decode(b64)
                        image_filename = "image.bin"
                        break
                    except Exception:
                        logger.debug("base64 解码失败", exc_info=True)

            # 直接取 base64 字段（dict 或对象均可能）
            if isinstance(seg, dict):
                data = seg.get("data", {})
                if isinstance(data, dict):
                    b64 = data.get("base64") or data.get("b64")
                else:
                    b64 = None
            else:
                b64 = getattr(seg, "base64", None) or getattr(seg, "b64", None)
            if b64:
                try:
                    image_bytes = base64.b64decode(b64)
                    image_filename = "image.bin"
                    break
                except Exception:
                    logger.debug("base64 字段解码失败", exc_info=True)

        # 尝试从引用/回复消息中提取图片
        if image_url is None and image_bytes is None:
            reply_url, reply_bytes = await self._extract_reply_image(event)
            if reply_url:
                image_url = reply_url
            elif reply_bytes:
                image_bytes = reply_bytes
                image_filename = "image.bin"

        if image_url is None and image_bytes is None:
            # 没有附带图片，看看命令参数里是否提供了图片 URL
            if arg_url:
                try:
                    yield event.plain_result("正在通过 URL 上传图片，请稍候...")
                    result = await self._upload_by_url(arg_url, extra)
                    if result.get("success"):
                        yield event.plain_result(self._format_upload_result(result))
                    else:
                        err = result.get("message") or result.get("error") or "未知错误"
                        yield event.plain_result(f"上传失败：{err}")
                except Exception:
                    logger.error("图床 URL 上传失败", exc_info=True)
                    yield event.plain_result("上传失败，请检查网络或图片链接后重试。")
                return

            yield event.plain_result("请发送/回复一张图片，或提供图片 URL。\n用法：/图床上传 [图片URL] [--format=webp] [--cdn=img.scdn.io] [--storage=local] [--password=密码]")
            return

        # 有图片 URL 或二进制
        try:
            yield event.plain_result("正在上传图片，请稍候...")
            if image_url:
                # 如果 URL 是 HTTP(S) 远端地址，直接用 URL 上传接口，避免下载
                if self._is_url(image_url):
                    result = await self._upload_by_url(image_url, extra)
                elif image_url.startswith("data:"):
                    # 部分平台可能给的是 base64 data URI
                    try:
                        encoded = image_url.split(",", 1)[1]
                        image_bytes = base64.b64decode(encoded)
                        image_filename = "image.bin"
                        result = await self._upload_file(image_bytes, image_filename, extra)
                    except Exception:
                        logger.error("解析图片 data URI 失败", exc_info=True)
                        yield event.plain_result("无法解析图片数据，请尝试重新发送图片。")
                        return
                elif os.path.isfile(image_url):
                    # 本地文件路径（受配置限制，防止任意文件读取）
                    if not self.local_upload_enabled:
                        yield event.plain_result("本地文件上传已被禁用，请在插件配置中开启 local_upload_enabled 后再使用。")
                        return
                    abs_path = os.path.abspath(image_url)
                    if self.local_upload_root:
                        root = os.path.abspath(self.local_upload_root)
                        try:
                            common = os.path.commonpath([root, abs_path])
                        except ValueError:
                            common = ""
                        if common != root:
                            yield event.plain_result("该文件路径不在允许的目录范围内。")
                            return
                    try:
                        with open(abs_path, "rb") as f:
                            image_bytes = f.read()
                        image_filename = os.path.basename(abs_path)
                        result = await self._upload_file(image_bytes, image_filename, extra)
                    except Exception:
                        logger.error("读取本地图片文件失败", exc_info=True)
                        yield event.plain_result("读取本地图片失败，请检查文件路径或权限是否正确。")
                        return
                else:
                    yield event.plain_result("不支持的图片地址，请提供 HTTP/HTTPS 链接、本地文件路径或 base64 data URI。")
                    return
            else:
                result = await self._upload_file(image_bytes or b"", image_filename or "image.bin", extra)

            if result.get("success"):
                yield event.plain_result(self._format_upload_result(result))
            else:
                err = result.get("message") or result.get("error") or "未知错误"
                yield event.plain_result(f"上传失败：{err}")
        except Exception:
            logger.error("图床上传失败", exc_info=True)
            yield event.plain_result("上传失败，请检查网络或图片后重试。")

    @filter.command("图床链接", alias={"上传图床链接", "scdn-url"})
    async def upload_image_url(self, event: AstrMessageEvent):
        """通过图片 URL 上传到 scdn 图床。
        用法：/图床链接 <图片URL> [--format=webp] [--cdn=img.scdn.io] [--storage=local]
        """
        raw_text = event.get_message_str().strip()
        # 去掉命令本身（兼容带 / 和不带 / 的情况）
        for prefix in ("/图床链接", "/上传图床链接", "/scdn-url", "图床链接", "上传图床链接", "scdn-url"):
            if raw_text.startswith(prefix):
                raw_text = raw_text[len(prefix):].strip()
                break

        arg_url, extra, error = self._parse_upload_args(raw_text)
        if error:
            yield event.plain_result(f"参数错误：{error}\n用法：/图床链接 <图片URL> [--format=webp] [--cdn=img.scdn.io] [--storage=local]")
            return
        arg_url = _clean_url_or_query(arg_url)
        if not arg_url:
            yield event.plain_result("请提供图片 URL。\n用法：/图床链接 <图片URL> [--format=webp] [--cdn=img.scdn.io] [--storage=local]")
            return

        try:
            yield event.plain_result("正在通过 URL 上传图片，请稍候...")
            result = await self._upload_by_url(arg_url, extra)
            if result.get("success"):
                yield event.plain_result(self._format_upload_result(result))
            else:
                err = result.get("message") or result.get("error") or "未知错误"
                yield event.plain_result(f"上传失败：{err}")
        except Exception:
            logger.error("图床 URL 上传失败", exc_info=True)
            yield event.plain_result("上传失败，请检查网络或图片链接后重试。")

    @filter.command("图床查询", alias={"查询图床", "scdn-info"})
    async def query_image(self, event: AstrMessageEvent, query: str = ""):
        """查询 scdn 图床图片公开元数据。
        用法：/图床查询 <图片ID或文件名>
        """
        query = _extract_scdn_identifier(query)
        if not query:
            yield event.plain_result("请提供图片 ID 或完整文件名。\n用法：/图床查询 <图片ID或文件名>")
            return

        try:
            yield event.plain_result("正在查询图片信息...")
            result = await self._query_image(query)
            if result.get("success"):
                yield event.plain_result(self._format_query_result(result))
            else:
                err = result.get("message") or result.get("error") or "未知错误"
                yield event.plain_result(f"查询失败：{err}")
        except Exception:
            logger.error("图床查询失败", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试。")

    @filter.command("图床解析", alias={"解析图床", "scdn-parse", "scdn-send"})
    async def parse_scdn_link(self, event: AstrMessageEvent, url: str = ""):
        """解析 scdn 图片链接并将图片发送到群里。
        用法：/图床解析 <scdn图片URL>
        """
        url = _clean_url_or_query(url)
        if not url:
            yield event.plain_result("请提供 scdn 图片链接。\n用法：/图床解析 <图片URL>")
            return

        match = _SCDN_LINK_RE.search(url)
        if not match:
            yield event.plain_result("请输入有效的 scdn 图片链接，如：https://img.scdn.io/i/xxx.webp")
            return

        scdn_url = match.group(0)
        identifier = match.group(1)

        try:
            yield event.plain_result("正在解析图片链接...")
            result = await self._query_image(identifier)
            if not result.get("success"):
                err = result.get("message") or result.get("error") or "未知错误"
                yield event.plain_result(f"解析失败：{err}")
                return

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

            yield event.chain_result(chain)
        except Exception:
            logger.error("解析 scdn 链接失败", exc_info=True)
            yield event.plain_result("解析失败，请稍后重试。")

    @filter.command("图床帮助", alias={"scdn-help"})
    async def help_cmd(self, event: AstrMessageEvent):
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
