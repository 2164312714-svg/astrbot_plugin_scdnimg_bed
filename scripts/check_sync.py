#!/usr/bin/env python3
"""校验 scdn 域名列表与版本号在 main.py / _conf_schema.json / README.md / metadata.yaml 间一致。

无外部依赖（不 import main，避免 astrbot 未安装时失败），直接用正则从源文件提取。
用法：python scripts/check_sync.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 域名形状：小写字母/数字/连字符 + 点 + 顶级域，用于过滤掉同步说明里的非域名文本。
_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _domains_from_main() -> list[str]:
    text = _read("main.py")
    m = re.search(r"_SCDN_DOMAINS\s*=\s*\(([^)]*)\)", text, re.S)
    if not m:
        return []
    return re.findall(r'"([^"]+)"', m.group(1))


def _domains_from_schema() -> list[str]:
    text = _read("_conf_schema.json")
    m = re.search(r'"hint":\s*"可选：([^"]+)"', text)
    if not m:
        return []
    # 去掉「（完整列表…）」备注后再按「 / 」拆分，避免末域名与备注粘连。
    domain_part = m.group(1).split("（", 1)[0]
    return [d.strip() for d in domain_part.split("/") if _DOMAIN_RE.match(d.strip())]


def _domains_from_readme() -> list[str]:
    text = _read("README.md")
    m = re.search(r"### 可选 CDN 域名\n.*?(?=\n###|\n## )", text, re.S)
    if not m:
        return []
    # 只取列表项 `- `域名``，跳过同步说明引用块里的行内反引号文本。
    return [d for d in re.findall(r"^\s*-\s*`([^`]+)`", m.group(0), re.M) if _DOMAIN_RE.match(d)]


def _version_from_main() -> str:
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', _read("main.py"), re.M)
    return m.group(1) if m else ""


def _version_from_metadata() -> str:
    m = re.search(r'^version:\s*"?([^"\n]+)"?', _read("metadata.yaml"), re.M)
    return m.group(1).strip() if m else ""


def main() -> int:
    errors: list[str] = []

    main_domains = _domains_from_main()
    schema_domains = _domains_from_schema()
    readme_domains = _domains_from_readme()

    if not main_domains:
        errors.append("未能从 main.py 提取 _SCDN_DOMAINS")
    if not schema_domains:
        errors.append("未能从 _conf_schema.json 提取域名 hint")
    if not readme_domains:
        errors.append("未能从 README.md 提取可选 CDN 域名列表")

    if main_domains and schema_domains and set(main_domains) != set(schema_domains):
        errors.append(
            "main.py 与 _conf_schema.json 域名不一致："
            f"{set(main_domains)} vs {set(schema_domains)}"
        )
    if main_domains and readme_domains and set(main_domains) != set(readme_domains):
        errors.append(
            "main.py 与 README.md 域名不一致："
            f"{set(main_domains)} vs {set(readme_domains)}"
        )

    v_main = _version_from_main()
    v_meta = _version_from_metadata()
    if not v_main:
        errors.append("未能从 main.py 提取 __version__")
    if not v_meta:
        errors.append("未能从 metadata.yaml 提取 version")
    if v_main and v_meta and v_main != v_meta:
        errors.append(f"版本不一致：main.py={v_main} vs metadata.yaml={v_meta}")

    if errors:
        print("同步校验失败：")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(f"同步校验通过：域名 {len(main_domains)} 个，版本 {v_main}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
