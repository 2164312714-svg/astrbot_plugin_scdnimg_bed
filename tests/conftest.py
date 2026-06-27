"""为测试注入 astrbot 桩模块，使 import main 无需真实 AstrBot 运行环境。

桩只在测试期注入 sys.modules，不影响插件本身在 AstrBot 下的运行。
"""
import logging
import sys
import types


def _install_astrbot_stubs() -> None:
    if "astrbot" in sys.modules:
        return

    # astrbot.api.event：提供 filter 装饰器（透传原函数）
    event_mod = types.ModuleType("astrbot.api.event")

    class _filter:
        @staticmethod
        def command(name, alias=None):
            def deco(func):
                return func
            return deco

        @staticmethod
        def regex(*args, **kwargs):
            def deco(func):
                return func
            return deco

    event_mod.filter = _filter()

    # astrbot.api.all：提供框架基类与消息组件占位
    all_mod = types.ModuleType("astrbot.api.all")

    class Star:
        def __init__(self, context=None, *args, **kwargs):
            self.context = context

    class Context:
        pass

    class AstrBotConfig(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    class AstrMessageEvent:
        pass

    class Plain:
        def __init__(self, text):
            self.text = text

    class Image:
        @classmethod
        def fromURL(cls, url):
            obj = cls()
            obj.url = url
            return obj

        async def convert_to_base64(self):
            return ""

    all_mod.Star = Star
    all_mod.Context = Context
    all_mod.AstrBotConfig = AstrBotConfig
    all_mod.logger = logging.getLogger("scdnimg-bed.test")
    all_mod.AstrMessageEvent = AstrMessageEvent
    all_mod.Plain = Plain
    all_mod.Image = Image

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.event = event_mod
    api.all = all_mod
    astrbot.api = api

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.all"] = all_mod


_install_astrbot_stubs()
