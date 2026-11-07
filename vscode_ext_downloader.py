#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VSCode 扩展离线包（VSIX）下载器。

作者：马小战
版本：1.0.2

只用 Python 标准库，不装任何第三方包，Windows / Linux / WSL 直接可跑。
默认不带参数运行会打印帮助信息。

VSCode Extension VSIX offline downloader.

Author : 马小战
Version: 1.0.2

Pure standard library, zero third-party dependency. Runs on Windows / Linux / WSL
with a plain `python` or `python3`. Running it without arguments prints the help.
"""

import argparse
import gzip
import io
import json
import os
import ssl
import sys
import time
import zipfile
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

# ---------------------------------------------------------------------------
# 常量区 / Constants
# ---------------------------------------------------------------------------

__version__ = "1.0.2"
__author__ = "马小战"

# 扩展市场查询接口 / The marketplace query endpoint
GALLERY_QUERY_URL = (
    "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
)
# 查询接口要求的 API 版本头 / Required API version header for the gallery
GALLERY_API_VERSION = "application/json;api-version=3.0-preview.1"

# extensionquery 的 flags：要版本列表、文件列表、分类标签、版本属性、资源地址、统计
# extensionquery flags: versions, files, categories, version properties, asset uri, stats
QUERY_FLAGS = 1 | 2 | 4 | 16 | 128 | 256

# VSIX 包在 files 数组里的 assetType 名称 / assetType name of the vsix payload
VSIX_ASSET_TYPE = "Microsoft.VisualStudio.Services.VSIXPackage"

# 统一的请求 UA，缺失会被市场接口拒绝 / Shared UA, the gallery rejects empty UA
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "VSCode-Ext-Downloader/" + __version__
)

BANNER = r"""
 __     __ ____   ____  ___  ____
 \ \   / // ___| / ___|/ _ \|  _ \
  \ \ / / \___ \| |   | | | | | | |
   \ V /   ___) | |___| |_| | |_| |
    \_/   |____/ \____|\___/|____/
"""

# 终端着色开关，非交互环境自动关闭 / Color switch, auto-disabled for non-tty
USE_COLOR = False
COLOR_CODES = {
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "dim": "\033[2m",
}
COLOR_RESET = "\033[0m"


# ---------------------------------------------------------------------------
# 基础工具 / Basic helpers
# ---------------------------------------------------------------------------


def setup_stdout_encoding():
    """把标准输出切到 UTF-8，避免 Windows 控制台用 GBK 打印中文或符号时报错。

    做什么：尽量将 stdout/stderr 的编码重设为 utf-8，失败就静默跳过。
    为什么：脚本含中文提示与 ASCII 艺术字，Windows 默认代码页可能直接抛异常中断。

    Switch stdout/stderr to UTF-8 to survive legacy Windows console code pages.

    What: best-effort reconfigure of stdout/stderr encoding to utf-8.
    Why : the script prints Chinese text and ASCII art, which can crash on GBK
          consoles if the encoding is left untouched.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except Exception:
            pass


def setup_color():
    """判断当前终端是否适合输出 ANSI 颜色，并记录到全局开关。

    做什么：检测 stdout 是否为交互终端，是则打开颜色。
    为什么：重定向到文件或日志时夹杂转义字符会污染输出内容。

    Decide whether ANSI colors are safe for the current terminal.

    What: enable color only when stdout looks like an interactive tty.
    Why : escape sequences pollute logs when output is redirected to a file.
    """
    global USE_COLOR
    try:
        USE_COLOR = bool(sys.stdout.isatty())
    except Exception:
        USE_COLOR = False


def paint(text, color):
    """按颜色名包装文本，终端不支持颜色时原样返回。

    做什么：把文本包上 ANSI 颜色码，关闭颜色时直接返回原文。
    为什么：让所有输出点统一走一个函数，避免到处判断平台与 tty。

    Wrap text with an ANSI color when coloring is enabled.

    What: prepend/append ANSI codes when USE_COLOR is on, else return as is.
    Why : centralizes the tty check so call sites stay short and readable.
    """
    if not USE_COLOR or color not in COLOR_CODES:
        return text
    return COLOR_CODES[color] + text + COLOR_RESET


def log_info(message):
    """打印普通信息行。

    做什么：以统一的 `[信息]` 前缀输出一行普通日志。
    为什么：让用户能一眼区分进度、成功、警告与错误。

    Print an informational line with a uniform prefix.

    What: writes one `[info]`-prefixed line to stdout.
    Why : gives users a visual rhythm between progress, success and errors.
    """
    print(paint("[信息] ", "cyan") + message)


def log_ok(message):
    """打印成功信息行。

    做什么：以统一的 `[完成]` 前缀输出一行成功日志。
    为什么：下载结束后用户需要明确的成功信号，而不是自己去看文件。

    Print a success line with a uniform prefix.

    What: writes one `[ok]`-prefixed line to stdout.
    Why : users need an explicit success signal instead of guessing.
    """
    print(paint("[完成] ", "green") + message)


def log_warn(message):
    """打印警告信息行（输出到标准错误）。

    做什么：以统一的 `[警告]` 前缀把内容写到 stderr。
    为什么：警告与错误不该混进正常结果流，便于重定向时分离。

    Print a warning line to stderr.

    What: writes one `[warn]`-prefixed line to stderr.
    Why : keeps warnings out of the normal result stream for redirection.
    """
    sys.stdout.flush()
    sys.stderr.write(paint("[警告] ", "yellow") + message + "\n")


def log_error(message):
    """打印错误信息行（输出到标准错误）。

    做什么：以统一的 `[错误]` 前缀把内容写到 stderr。
    为什么：错误必须显式可见且不污染标准输出的机器可读部分。

    Print an error line to stderr.

    What: writes one `[error]`-prefixed line to stderr.
    Why : errors must stay visible without polluting stdout.
    """
    sys.stdout.flush()
    sys.stderr.write(paint("[错误] ", "red") + message + "\n")


def human_size(num_bytes):
    """把字节数转成带单位的可读字符串。

    做什么：按 1024 进制换算成 B/KB/MB/GB，保留一位小数。
    为什么：下载进度里裸字节数没有直观尺度感。

    Convert a byte count into a human readable string.

    What: scales the value by 1024 into B/KB/MB/GB with one decimal.
    Why : raw byte counts in progress output carry no intuitive scale.
    """
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return "{0:.0f} {1}".format(size, unit)
            return "{0:.1f} {1}".format(size, unit)
        size /= 1024.0
    return "{0:.1f} TB".format(size)


def build_ssl_context(insecure):
    """构造 HTTPS 请求使用的 SSL 上下文。

    做什么：默认返回校验证书的上下文，insecure 为真时返回不校验的上下文。
    为什么：企业环境常见自签名代理证书，需要一条明确的降级通道。

    Build the SSL context used for HTTPS requests.

    What: returns a verifying context, or an unverified one when asked.
    Why : corporate proxies with self-signed certificates need an escape hatch.
    """
    if insecure:
        return ssl._create_unverified_context()
    try:
        return ssl.create_default_context()
    except Exception:
        return None


def apply_proxy(opener_handlers, proxy):
    """按需给 urllib 处理器列表追加代理配置。

    做什么：把 --proxy 参数转换成 ProxyHandler 并加入处理器列表。
    为什么：国内网络访问市场接口常常需要显式代理。

    Append a proxy handler to the urllib handler list when requested.

    What: turns the --proxy value into a ProxyHandler and appends it.
    Why : the marketplace endpoint often needs an explicit proxy in CN networks.
    """
    if not proxy:
        return opener_handlers
    handler = urlrequest.ProxyHandler({"http": proxy, "https": proxy})
    opener_handlers.append(handler)
    return opener_handlers


def make_opener(proxy, insecure):
    """组装一个带 UA、超时与可选代理的 urllib opener。

    做什么：合并 HTTPSHandler 与可选 ProxyHandler，返回 OpenerDirector。
    为什么：后续所有请求复用一个 opener，避免重复配置且行为一致。

    Assemble a urllib opener with UA, timeout and optional proxy support.

    What: combines an HTTPSHandler with an optional ProxyHandler.
    Why : one shared opener keeps every request configured identically.
    """
    handlers = [urlrequest.HTTPSHandler(context=build_ssl_context(insecure))]
    apply_proxy(handlers, proxy)
    return urlrequest.build_opener(*handlers)


# ---------------------------------------------------------------------------
# 扩展标识解析 / Extension id parsing
# ---------------------------------------------------------------------------


def normalize_extension_id(raw):
    """把用户输入规整为 `publisher.name` 形式的扩展标识。

    做什么：既接受纯标识，也接受 marketpace 网页链接，统一提取 itemName。
    为什么：用户习惯直接粘贴浏览器地址栏里的链接，多一步手工提取很烦。

    Normalize user input into a `publisher.name` extension id.

    What: accepts a bare id or a marketplace URL and extracts its itemName.
    Why : users paste browser URLs; forcing manual extraction is needless friction.
    """
    value = (raw or "").strip().strip('"').strip("'")
    if not value:
        raise ValueError("扩展标识为空")

    lowered = value.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        parsed = urlparse.urlparse(value)
        query = urlparse.parse_qs(parsed.query)
        # parse_qs 保留键的原始大小写，所以这里必须逐键忽略大小写比对
        # parse_qs keeps the original key casing, so compare case-insensitively
        item_name = ""
        for key, values in query.items():
            if key.lower() == "itemname" and values:
                item_name = values[0]
                break
        if not item_name:
            raise ValueError("链接中找不到 itemName 参数，请直接给出 publisher.name")
        value = item_name

    value = value.strip()
    if value.lower().startswith("itemname="):
        value = value.split("=", 1)[1]

    parts = value.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("标识格式应为 publisher.name，例如 dnlytras.nord-wave")
    return parts[0] + "." + parts[1]


def split_extension_id(ext_id):
    """把 `publisher.name` 拆成发布者与扩展名两段。

    做什么：按第一个点切分标识，返回 (publisher, name) 元组。
    为什么：拼下载地址和输出文件名都要用这两段。

    Split a `publisher.name` id into its two halves.

    What: returns a (publisher, name) tuple split on the first dot.
    Why : both halves are needed for asset URLs and output file names.
    """
    publisher, _, name = ext_id.partition(".")
    return publisher, name


# ---------------------------------------------------------------------------
# 市场查询 / Marketplace query
# ---------------------------------------------------------------------------


def query_extension(ext_id, opener, timeout):
    """向市场接口查询扩展的元数据与全部版本信息。

    做什么：POST extensionquery，取回 extensions[0] 的完整字典。
    为什么：版本号、平台变体和真实下载地址都只能从这里拿到，硬拼 URL 不可靠。

    Query the marketplace for extension metadata and all published versions.

    What: POSTs to extensionquery and returns the first extension object.
    Why : versions, platform variants and real asset URLs only come from here.
    """
    payload = {
        "filters": [
            {
                "criteria": [
                    {"filterType": 7, "value": ext_id},
                ],
                "pageNumber": 1,
                "pageSize": 1,
                "sortBy": 0,
                "sortOrder": 0,
            }
        ],
        "assetTypes": [],
        "flags": QUERY_FLAGS,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": GALLERY_API_VERSION,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",
    }
    req = urlrequest.Request(GALLERY_QUERY_URL, data=body, headers=headers, method="POST")

    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
    except urlerror.HTTPError as exc:
        raise RuntimeError("查询接口返回 HTTP {0}".format(exc.code))
    except urlerror.URLError as exc:
        raise RuntimeError("无法连接扩展市场：{0}".format(exc.reason))

    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise RuntimeError("市场返回的内容不是合法 JSON")

    results = data.get("results") or []
    extensions = results[0].get("extensions") if results else None
    if not extensions:
        raise RuntimeError("市场里找不到这个扩展：{0}".format(ext_id))
    return extensions[0]


def extract_version_entries(extension):
    """把市场返回的版本数组整理成统一的内部结构。

    做什么：从每个 version 节点取出号、平台、包下载地址与备用地址。
    为什么：原始 JSON 字段名又长又深，先摊平能让后续筛选逻辑干净很多。

    Flatten the marketplace version array into a uniform internal shape.

    What: pulls version, platform, direct asset url and fallback url per entry.
    Why : the raw JSON is deeply nested; flattening keeps later filters simple.
    """
    entries = []
    for node in extension.get("versions") or []:
        files = node.get("files") or []
        direct_url = ""
        for item in files:
            if item.get("assetType") == VSIX_ASSET_TYPE:
                direct_url = item.get("source") or ""
                break
        entries.append(
            {
                "version": node.get("version") or "0.0.0",
                "platform": node.get("targetPlatform") or "",
                "direct_url": direct_url,
                "fallback_url": node.get("assetUri") or "",
                "last_updated": node.get("lastUpdated") or "",
            }
        )
    return entries


def sort_versions(entries):
    """按版本号从新到旧排序版本条目。

    做什么：把版本号切成数字元组做比较，非数字段退化为字符串比较。
    为什么：字符串排序会让 1.10 排在 1.9 前面，用户会以为拿错了版本。

    Sort version entries from newest to oldest.

    What: compares dotted numeric tuples, falling back to string compare.
    Why : plain string sorting puts 1.10 before 1.9 and confuses users.
    """
    def key_of(entry):
        chunks = []
        for piece in entry["version"].split("."):
            digits = "".join(ch for ch in piece if ch.isdigit())
            chunks.append(int(digits) if digits else 0)
        return tuple(chunks)

    return sorted(entries, key=key_of, reverse=True)


def select_versions(entries, want_version, platform, want_all):
    """按用户条件从版本条目里挑出要下载的若干条。

    做什么：依次应用版本号过滤、平台过滤，再决定返回全部还是单条。
    为什么：把筛选集中在一个函数里，主流程读起来就是一条直线。

    Pick the version entries that match the user's request.

    What: filters by version and platform, then returns all or just the newest.
    Why : centralizing the filters keeps the main flow linear and readable.
    """
    ordered = sort_versions(entries)

    if want_version:
        matched = [e for e in ordered if e["version"] == want_version]
        if not matched:
            raise RuntimeError("该扩展没有版本 {0}".format(want_version))
        ordered = matched

    if platform:
        matched = [e for e in ordered if e["platform"] == platform]
        if not matched:
            raise RuntimeError("该扩展没有平台 {0} 的包".format(platform))
        ordered = matched
    else:
        # 未指定平台时优先选通用包（targetPlatform 为空）
        # Without a platform filter, prefer the universal (empty platform) build
        universal = [e for e in ordered if not e["platform"]]
        if universal:
            ordered = universal

    if want_all:
        return ordered
    return ordered[:1]


# ---------------------------------------------------------------------------
# 下载与校验 / Download and verification
# ---------------------------------------------------------------------------


def download_bytes(opener, url, timeout, retries=3):
    """下载一个 URL 的全部字节，失败时按固定次数重试。

    做什么：分块读取响应体，重试网络抖动，返回 (字节, 内容类型)。
    为什么：市场 CDN 偶发断连，一次失败就退出会让人误以为地址不对。

    Download the full body of a URL with a small retry budget.

    What: reads the response in chunks, retrying transient failures.
    Why : the CDN occasionally drops connections; failing once looks like a bad URL.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    last_error = None

    for attempt in range(1, retries + 1):
        req = urlrequest.Request(url, headers=headers)
        try:
            chunks = []
            with opener.open(req, timeout=timeout) as resp:
                content_type = resp.headers.get("Content-Type", "")
                total = resp.headers.get("Content-Length")
                total = int(total) if total and total.isdigit() else 0
                received = 0
                while True:
                    block = resp.read(65536)
                    if not block:
                        break
                    chunks.append(block)
                    received += len(block)
                    render_progress(received, total)
            print("")
            return b"".join(chunks), content_type
        except (urlerror.URLError, OSError) as exc:
            print("")
            last_error = exc
            if attempt < retries:
                log_warn("第 {0} 次下载失败，2 秒后重试：{1}".format(attempt, exc))
                time.sleep(2)
    raise RuntimeError("下载失败：{0}".format(last_error))


def render_progress(received, total):
    """在终端同一行内刷新下载进度。

    做什么：按百分比或纯字节数重绘进度行，非 tty 环境直接跳过。
    为什么：大包下载需要反馈，重定向到文件时又不该刷屏。

    Repaint a single-line download progress indicator.

    What: redraws a percentage or byte-count line, skipped for non-tty output.
    Why : big packages need feedback, yet redirected output must stay clean.
    """
    if not USE_COLOR:
        return
    if total > 0:
        percent = received * 100.0 / total
        line = "  下载中 {0:5.1f}%  {1} / {2}".format(
            percent, human_size(received), human_size(total)
        )
    else:
        line = "  下载中 {0}".format(human_size(received))
    sys.stdout.write("\r" + line)
    sys.stdout.flush()


def maybe_gunzip(data):
    """如果数据是 gzip 流就解压，否则原样返回。

    做什么：检查前两个字节是否为 gzip 魔数 0x1f8b，是则解压。
    为什么：市场的 vspackage 端点会把 VSIX 再包一层 gzip，不解压得到的文件无法安装。

    Decompress the payload when it is a gzip stream.

    What: checks for the 0x1f8b magic and inflates when present.
    Why : the vspackage endpoint wraps the VSIX in gzip; skipping this yields
          an uninstallable file that looks perfectly fine on disk.
    """
    if len(data) >= 2 and data[0] == 0x1F and data[1] == 0x8B:
        try:
            return gzip.decompress(data)
        except Exception:
            return data
    return data


def verify_vsix(data):
    """校验字节流是否为结构完整的 VSIX 包。

    做什么：用内存中的 zipfile 打开，并要求存在扩展清单文件。
    为什么：静默拿到损坏文件是最伤人的失败，落地前必须验一次。

    Verify that the byte stream is a structurally valid VSIX package.

    What: opens it as an in-memory zip and requires an extension manifest inside.
    Why : a silently corrupted file is the most costly failure mode, so verify
          before touching the disk.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
    except Exception:
        return False, "不是合法的 zip/vsix 结构"

    manifest_candidates = (
        "extension.vsixmanifest",
        "extension/package.json",
    )
    for candidate in manifest_candidates:
        if candidate in names:
            return True, candidate
    return False, "包内缺少 extension.vsixmanifest"


def build_output_name(ext_id, entry):
    """按官方习惯拼出 VSIX 的文件名。

    做什么：生成 publisher.name-version[@platform].vsix 形式的文件名。
    为什么：与官方命名一致，日后与 vscode 安装记录对照时不会错位。

    Build the output file name the way the official tooling does.

    What: produces publisher.name-version[@platform].vsix.
    Why : matching official naming avoids confusion when cross-checking installs.
    """
    if entry["platform"]:
        return "{0}-{1}@{2}.vsix".format(ext_id, entry["version"], entry["platform"])
    return "{0}-{1}.vsix".format(ext_id, entry["version"])


def download_one(entry, ext_id, out_dir, opener, timeout, force):
    """下载单个版本条目并落盘，返回 (文件路径, 是否真的写入)。

    做什么：先试直链再试备用地址，校验通过后写入磁盘。
    为什么：直链最省事，备用地址要处理 gzip，两条路都留才算真的能下到。

    Download one version entry and write it to disk.

    What: tries the direct asset URL first, then the gzip-wrapped fallback,
          returning a (path, written) tuple.
    Why : the direct URL is simplest, but the fallback must handle gunzip so the
          tool still works when the direct link is missing.
    """
    file_name = build_output_name(ext_id, entry)
    target_path = os.path.join(out_dir, file_name)

    if os.path.exists(target_path) and not force:
        log_warn("文件已存在，跳过：{0}".format(target_path))
        return target_path, False

    urls = []
    if entry["direct_url"]:
        urls.append(entry["direct_url"])
    if entry["fallback_url"]:
        urls.append(entry["fallback_url"])
    if not urls:
        raise RuntimeError("扩展 {0} 没有可用的下载地址".format(ext_id))

    last_problem = ""
    for url in urls:
        log_info("下载 {0} -> {1}".format(file_name, url))
        try:
            data, _ = download_bytes(opener, url, timeout)
        except RuntimeError as exc:
            last_problem = str(exc)
            log_warn("该地址失败，尝试下一个：{0}".format(last_problem))
            continue

        data = maybe_gunzip(data)
        ok, detail = verify_vsix(data)
        if not ok:
            last_problem = detail
            log_warn("包结构校验未通过（{0}），尝试下一个地址".format(detail))
            continue

        with open(target_path, "wb") as handle:
            handle.write(data)
        log_ok(
            "已保存 {0}（{1}，校验通过：{2}）".format(
                target_path, human_size(len(data)), detail
            )
        )
        return target_path, True

    raise RuntimeError("所有下载地址都失败了：{0}".format(last_problem))


# ---------------------------------------------------------------------------
# 命令行 / Command line
# ---------------------------------------------------------------------------


def build_parser():
    """构造命令行参数解析器。

    做什么：集中声明所有参数、默认值与帮助文案。
    为什么：参数说明是用户接触脚本的第一入口，集中放置最易维护。

    Build the command line argument parser.

    What: declares every flag, default value and help text in one place.
    Why : the help output is the user's first contact point, so keep it together.
    """
    parser = argparse.ArgumentParser(
        prog="vscode_ext_downloader.py",
        description="下载任意 VSCode 扩展的离线安装包（VSIX），支持指定版本与平台。",
        epilog=(
            "示例 Examples:\n"
            "  python vscode_ext_downloader.py dnlytras.nord-wave\n"
            "  python vscode_ext_downloader.py "
            "https://marketplace.visualstudio.com/items?itemName=dnlytras.nord-wave\n"
            "  python vscode_ext_downloader.py dnlytras.nord-wave --list\n"
            "  python vscode_ext_downloader.py dnlytras.nord-wave -v 0.1.7\n"
            "  python vscode_ext_downloader.py ms-python.python -p win32-x64\n"
            "  python vscode_ext_downloader.py dnlytras.nord-wave -o D:\\\\vsix\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "extension",
        nargs="?",
        help="扩展标识 publisher.name，或直接粘贴市场页面链接",
    )
    parser.add_argument(
        "-v", "--version",
        dest="want_version",
        default="",
        help="指定版本号，缺省为最新版",
    )
    parser.add_argument(
        "-p", "--platform",
        default="",
        help="指定平台变体，如 win32-x64 / linux-x64 / darwin-arm64",
    )
    parser.add_argument(
        "-o", "--output",
        default=".",
        help="保存目录，缺省为当前目录",
    )
    parser.add_argument(
        "-l", "--list",
        action="store_true",
        help="只列出可用版本，不下载",
    )
    parser.add_argument(
        "-a", "--all",
        action="store_true",
        help="下载匹配到的全部版本",
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="目标文件已存在时覆盖",
    )
    parser.add_argument(
        "--proxy",
        default="",
        help="HTTP/HTTPS 代理，如 http://127.0.0.1:7890",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="跳过 HTTPS 证书校验（自签名代理环境使用）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="单次请求超时秒数，缺省 60",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="关闭彩色输出",
    )
    parser.add_argument(
        "--version-info",
        action="store_true",
        help="打印脚本版本信息后退出",
    )
    return parser


def print_banner():
    """打印 ASCII 艺术字与脚本信息块。

    做什么：输出艺术字、作者、版本与一行用途说明。
    为什么：命令行工具的开场需要一眼可辨的标识与版本，便于记录问题。

    Print the ASCII banner with author and version info.

    What: writes the art plus author, version and a one-line purpose.
    Why : a recognizable header with version helps when reporting issues.
    """
    print(paint(BANNER, "cyan"))
    print("  VSCode 扩展离线包下载器 (VSIX Downloader)")
    print("  作者 Author : {0}".format(__author__))
    print("  版本 Version: {0}".format(__version__))
    print("  用途 Usage  : 在无网或内网环境批量获取扩展安装包")
    print("")


def print_version_table(entries, ext_id):
    """把版本条目列表打印成对齐的表格。

    做什么：输出序号、版本号、平台、大小未知时的占位与更新时间。
    为什么：让用户在选择版本前先看清有哪些可选，避免反复试错。

    Print version entries as an aligned table.

    What: shows index, version, platform and last-updated timestamp.
    Why : users should see the available choices before committing to one.
    """
    print("")
    print("扩展 {0} 共 {1} 个版本条目：".format(ext_id, len(entries)))
    print("  {0:<4} {1:<18} {2:<16} {3}".format("序号", "版本", "平台", "更新时间"))
    print("  " + "-" * 62)
    for index, entry in enumerate(entries, start=1):
        print(
            "  {0:<4} {1:<18} {2:<16} {3}".format(
                index,
                entry["version"],
                entry["platform"] or "通用 universal",
                entry["last_updated"][:10] or "-",
            )
        )
    print("")


def main(argv=None):
    """脚本主入口，串联参数解析、查询、筛选与下载。

    做什么：解析命令行，查询市场，按条件筛选版本并逐个下载，返回退出码。
    为什么：把流程集中在一处，方便人类按顺序读懂并手工调整。

    Main entry point wiring arguments, query, filtering and downloads together.

    What: parses args, queries the marketplace, filters versions and downloads.
    Why : keeping the flow in one place makes it easy to read and hand-edit.
    """
    global USE_COLOR

    setup_stdout_encoding()
    setup_color()

    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)

    # 无参数时打印帮助，符合"默认显示帮助信息"的习惯
    # No arguments prints help, matching the "help by default" convention
    if not argv:
        print_banner()
        parser.print_help()
        print("")
        print("提示：最少给一个扩展标识即可开始下载。")
        return 0

    args = parser.parse_args(argv)

    if args.no_color:
        USE_COLOR = False

    if args.version_info:
        print("vscode_ext_downloader.py {0} by {1}".format(__version__, __author__))
        return 0

    print_banner()

    try:
        ext_id = normalize_extension_id(args.extension)
    except ValueError as exc:
        log_error(str(exc))
        return 2

    publisher, name = split_extension_id(ext_id)
    out_dir = os.path.abspath(args.output)
    if not os.path.isdir(out_dir):
        try:
            os.makedirs(out_dir)
            log_info("已创建输出目录：{0}".format(out_dir))
        except OSError as exc:
            log_error("无法创建输出目录 {0}：{1}".format(out_dir, exc))
            return 2

    log_info("解析扩展标识：{0}（发布者 {1} / 扩展 {2}）".format(ext_id, publisher, name))

    opener = make_opener(args.proxy, args.insecure)

    try:
        extension = query_extension(ext_id, opener, args.timeout)
    except RuntimeError as exc:
        log_error(str(exc))
        return 3

    entries = extract_version_entries(extension)
    if not entries:
        log_error("该扩展没有可下载的版本")
        return 3

    if args.list:
        listed = sort_versions(entries)
        if args.want_version:
            listed = [e for e in listed if e["version"] == args.want_version]
        if args.platform:
            listed = [e for e in listed if e["platform"] == args.platform]
        if not listed:
            log_error("没有匹配 --version / --platform 的版本条目")
            return 4
        print_version_table(listed, ext_id)
        return 0

    try:
        targets = select_versions(entries, args.want_version, args.platform, args.all)
    except RuntimeError as exc:
        log_error(str(exc))
        return 4

    log_info(
        "命中 {0} 个版本条目，开始下载到 {1}".format(len(targets), out_dir)
    )

    saved = 0
    for entry in targets:
        try:
            _, written = download_one(
                entry, ext_id, out_dir, opener, args.timeout, args.force
            )
            if written:
                saved += 1
        except RuntimeError as exc:
            log_error(str(exc))
            return 5

    log_ok("全部完成，共保存 {0} 个文件到 {1}".format(saved, out_dir))
    print("")
    print("安装方法：在 VSCode 中按 Ctrl+Shift+P，运行 Extensions: Install from VSIX...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
