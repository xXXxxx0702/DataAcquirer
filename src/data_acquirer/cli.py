"""Command-line interface: headless data pulls for scripts and scheduled tasks.

``data-acquirer`` / ``python run.py`` / ``python -m data_acquirer`` with no
arguments still launch the GUI (so double-clicking 启动.bat keeps working);
with a subcommand they run headless::

    data-acquirer pull -c config/presets/example.json
    data-acquirer pull --bookmark 电厂A --last 24h --point "B5_ZQWD,Float" -o out.csv
    data-acquirer test -c config/presets/example.json
    data-acquirer points --bookmark 电厂A --filter B5_

Design notes:
  * Configuration is layered: built-in defaults < ``-c`` preset JSON <
    ``--bookmark`` < individual flags (``--host``, ``--start``, …).
  * Progress/log lines go to stderr; only real results (the ``points``
    catalogue, the ``test`` verdict, the ``--dry-run`` plan) go to stdout,
    so output can be piped or redirected.
  * ``--point`` / ``--points-file`` use the same ``名称,类型,备注`` format as
    the GUI's clipboard paste (tabs are treated as commas too).
  * Exit codes: 0 success, 2 bad arguments/config, 3 connection/query/write
    failure, 4 interrupted with Ctrl+C.
"""

from __future__ import annotations

import argparse
import codecs
import locale
import re
import sys
from pathlib import Path

from . import __version__
from .config import AcquireConfig, PointSpec
from .paths import BOOKMARKS_PATH

_STAMP_FMT = "%Y%m%d-%H%M%S"
_TIME_FMT = "%Y-%m-%d %H:%M:%S"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_RUNTIME = 3
EXIT_INTERRUPTED = 4


class CliError(Exception):
    """A user-facing argument/configuration error (exit code 2)."""


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _stderr_logger(quiet: bool = False):
    if quiet:
        return lambda message: None

    def log(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    return log


def _parse_point(text: str) -> PointSpec:
    """Parse ``名称[,类型[,备注]]`` (tabs allowed, like the GUI paste)."""
    parts = [p.strip() for p in text.replace("\t", ",").split(",", 2)]
    if not parts[0]:
        raise CliError(f"无效的点位定义: {text!r}（格式: 名称,类型,备注）")
    name = parts[0]
    measurement = parts[1] if len(parts) > 1 and parts[1] else "Float"
    note = parts[2] if len(parts) > 2 else ""
    return PointSpec(name=name, measurement=measurement, note=note, enabled=True)


def _read_points_file(path: str) -> list[PointSpec]:
    """Read a points file, auto-detecting the encodings Windows produces.

    UTF-8 (with/without BOM) is the documented format, but PowerShell 5.1's
    ``>`` redirection writes UTF-16 LE and 记事本's "ANSI" is the locale
    codepage (GBK on Chinese Windows) — accept all three.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise CliError(f"无法读取点位文件 {path}: {exc}")
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode(locale.getpreferredencoding(False), errors="replace")
    points = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        points.append(_parse_point(line))
    if not points:
        raise CliError(f"点位文件 {path} 中没有有效点位")
    return points


def _range_from_last(spec: str) -> tuple[str, str]:
    """``24h`` / ``7d`` / ``3mo`` / ``1y`` -> (start, end); end = now.

    Months/years walk the calendar backwards (like the GUI quick-range
    buttons), hours/days are exact offsets.
    """
    import pandas as pd

    match = re.fullmatch(r"(\d+)\s*(h|d|mo|y)", spec.strip(), re.IGNORECASE)
    if not match or int(match.group(1)) <= 0:
        raise CliError(
            f"无法解析 --last {spec!r}，应为 数字+单位（h=小时 d=天 mo=月 y=年），如 24h、7d、3mo、1y"
        )
    amount, unit = int(match.group(1)), match.group(2).lower()
    end = pd.Timestamp.now().floor("s")
    if unit == "h":
        start = end - pd.Timedelta(hours=amount)
    elif unit == "d":
        start = end - pd.Timedelta(days=amount)
    elif unit == "mo":
        start = end - pd.DateOffset(months=amount)
    else:
        start = end - pd.DateOffset(years=amount)
    return start.strftime(_TIME_FMT), end.strftime(_TIME_FMT)


def _expand_output(template: str, cfg: AcquireConfig) -> str:
    """Replace ``{start}`` / ``{end}`` / ``{now}`` with ``YYYYMMDD-HHMMSS``."""
    if not any(key in template for key in ("{start}", "{end}", "{now}")):
        return template
    import pandas as pd

    def stamp(value: str) -> str:
        return pd.to_datetime(value).strftime(_STAMP_FMT)

    return (
        template.replace("{start}", stamp(cfg.start_time))
        .replace("{end}", stamp(cfg.end_time))
        .replace("{now}", pd.Timestamp.now().strftime(_STAMP_FMT))
    )


def _base_config(args: argparse.Namespace) -> AcquireConfig:
    """Build the config from preset file + bookmark + individual overrides."""
    path = getattr(args, "config", None)
    if path:
        try:
            cfg = AcquireConfig.load(path)
        except FileNotFoundError:
            raise CliError(f"配置文件不存在: {path}")
        except (OSError, ValueError, TypeError) as exc:
            raise CliError(f"配置文件无法解析: {path}（{exc}）")
    else:
        cfg = AcquireConfig()

    name = getattr(args, "bookmark", None)
    if name:
        from .bookmarks import BookmarkStore

        store = BookmarkStore(BOOKMARKS_PATH)
        bookmark = store.get(name)
        if bookmark is None:
            known = "、".join(store.names()) or "（还没有保存过书签）"
            raise CliError(f"书签 {name!r} 不存在。已有书签: {known}")
        cfg.host = bookmark.host
        cfg.port = bookmark.port
        cfg.username = bookmark.username
        cfg.password = bookmark.password
        if bookmark.database:
            cfg.database = bookmark.database

    for attr in ("host", "username", "password", "database"):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(cfg, attr, value)
    port = getattr(args, "port", None)
    if port is not None:
        cfg.port = port
    return cfg


def _print_plan(cfg: AcquireConfig) -> None:
    import math

    import pandas as pd

    start = pd.to_datetime(cfg.start_time)
    end = pd.to_datetime(cfg.end_time)
    chunks = math.ceil((end - start) / pd.Timedelta(hours=cfg.chunk_hours))
    points = cfg.enabled_points()
    print("[dry-run] 配置有效，执行计划：")
    print(f"  服务器  : {cfg.username}@{cfg.host}:{cfg.port}  数据库: {cfg.database}")
    print(
        f"  时间范围: {cfg.start_time} ~ {cfg.end_time}"
        f"（{chunks} 段 × {cfg.chunk_hours} 小时，UTC偏移 {cfg.utc_offset_hours:+d} 小时）"
    )
    print(f'  查询    : SELECT "{cfg.value_field}" … WHERE "{cfg.measure_tag}" = \'<点位>\'')
    print(f"  点位    : {len(points)} 个启用")
    for point in points:
        note = f"  {point.note}" if point.note else ""
        print(f"    - {point.name} ({point.measurement}){note}")
    print(f"  输出    : {cfg.output_path}")


# ---------------------------------------------------------------------- #
# Subcommands
# ---------------------------------------------------------------------- #
def cmd_pull(args: argparse.Namespace) -> int:
    cfg = _base_config(args)

    if args.last and (args.start or args.end):
        raise CliError("--last 不能与 --start/--end 同时使用")
    if args.last:
        cfg.start_time, cfg.end_time = _range_from_last(args.last)
    if args.start:
        cfg.start_time = args.start
    if args.end:
        cfg.end_time = args.end
    if args.chunk_hours is not None:
        cfg.chunk_hours = args.chunk_hours
    if args.utc_offset is not None:
        cfg.utc_offset_hours = args.utc_offset
    if args.value_field:
        cfg.value_field = args.value_field
    if args.measure_tag:
        cfg.measure_tag = args.measure_tag

    points: list[PointSpec] = []
    if args.points_file:
        points.extend(_read_points_file(args.points_file))
    for text in args.point or ():
        points.append(_parse_point(text))
    if points:
        cfg.points = points

    if args.output:
        cfg.output_path = args.output

    errors = cfg.validate()
    if errors:
        raise CliError("配置无效:\n  - " + "\n  - ".join(errors))
    cfg.output_path = _expand_output(cfg.output_path, cfg)

    if args.dry_run:
        _print_plan(cfg)
        return EXIT_OK

    from .core.puller import DataPuller

    DataPuller(cfg, log=_stderr_logger(quiet=args.quiet)).run()
    return EXIT_OK


def cmd_test(args: argparse.Namespace) -> int:
    cfg = _base_config(args)
    from .core.puller import DataPuller

    version = DataPuller(cfg).test_connection()
    print(f"连接成功: InfluxDB {version}（{cfg.host}:{cfg.port}，数据库 {cfg.database} 存在）")
    return EXIT_OK


def cmd_points(args: argparse.Namespace) -> int:
    cfg = _base_config(args)
    if args.measure_tag:
        cfg.measure_tag = args.measure_tag

    from .core.puller import DataPuller

    log = _stderr_logger()
    catalog = DataPuller(cfg, log=log).fetch_points()
    needle = (args.filter or "").lower()
    shown = 0
    for name in sorted(catalog):
        if needle and needle not in name.lower():
            continue
        print(f"{name},{catalog[name]}")
        shown += 1
    if needle:
        log(f"过滤后剩余 {shown} 个点位")
    return EXIT_OK


# ---------------------------------------------------------------------- #
# Parser
# ---------------------------------------------------------------------- #
def _add_connection_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("连接（优先级: 默认值 < -c 配置文件 < --bookmark < 单项参数）")
    group.add_argument(
        "-c", "--config", metavar="JSON",
        help="配置预设文件，如 config/presets/example.json 或 config/last_session.json",
    )
    group.add_argument(
        "--bookmark", metavar="名称",
        help="使用 config/bookmarks.json 里保存的服务器书签覆盖连接参数",
    )
    group.add_argument("--host", metavar="地址", help="服务器地址")
    group.add_argument("--port", type=int, metavar="端口", help="服务器端口")
    group.add_argument("--username", metavar="用户名", help="用户名")
    group.add_argument(
        "--password", metavar="密码",
        help="密码（明文参数会留在命令行历史中，建议优先用预设或书签）",
    )
    group.add_argument("--database", metavar="库名", help="数据库名")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="data-acquirer",
        description=(
            "InfluxDB（v1 / InfluxQL）时序数据拉取工具。"
            "不带参数运行时启动图形界面；带子命令时进入无界面的命令行模式，"
            "适合脚本调用与计划任务定时拉取。"
        ),
        epilog=(
            "示例:\n"
            "  data-acquirer                                # 启动图形界面\n"
            "  data-acquirer pull -c config/presets/example.json\n"
            "  data-acquirer pull --help                    # 查看拉取的全部参数\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"data-acquirer {__version__}"
    )
    sub = parser.add_subparsers(dest="command", metavar="子命令")

    sub.add_parser("gui", help="启动图形界面（等同于不带任何参数运行）")

    pull = sub.add_parser(
        "pull",
        help="按配置拉取数据并导出 CSV",
        description="按配置拉取数据并导出 CSV。过程日志输出到 stderr。",
        epilog=(
            "示例:\n"
            "  data-acquirer pull -c config/presets/example.json\n"
            '  data-acquirer pull -c 预设.json --start "2026-07-01 00:00:00" '
            '--end "2026-07-08 00:00:00" -o output/week.csv\n'
            '  data-acquirer pull --bookmark 电厂A --last 24h '
            '--point "B5_ZQWD,Float,主汽温度" -o "output/日报_{end}.csv"\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_connection_args(pull)
    time_group = pull.add_argument_group("时间范围")
    time_group.add_argument(
        "--start", metavar="时间", help='起始时间（本地时间），如 "2026-07-01 00:00:00"'
    )
    time_group.add_argument(
        "--end", metavar="时间", help='终止时间（本地时间），如 "2026-07-08 00:00:00"'
    )
    time_group.add_argument(
        "--last", metavar="N单位",
        help="拉取最近一段时间，结束时间取当前系统时间；单位 h=小时 d=天 mo=月 y=年，"
             "如 24h、7d、3mo、1y（月/年按自然日历回推）。与 --start/--end 互斥",
    )
    behaviour = pull.add_argument_group("查询行为")
    behaviour.add_argument("--chunk-hours", type=int, metavar="N", help="分段大小（小时）")
    behaviour.add_argument(
        "--utc-offset", type=int, metavar="N", help="UTC 偏移（小时），如 8 表示东八区"
    )
    behaviour.add_argument("--value-field", metavar="字段", help='取值字段（默认 "value"）')
    behaviour.add_argument(
        "--measure-tag", metavar="标签", help='点位标签名（默认 "measurePoint"）'
    )
    points_group = pull.add_argument_group("点位（给出任意一项后将整体替换配置文件中的点位表）")
    points_group.add_argument(
        "--point", action="append", metavar="名称[,类型[,备注]]",
        help="追加一个点位，可多次使用；类型缺省为 Float，如 --point \"B5_ZQWD,Float,主汽温度\"",
    )
    points_group.add_argument(
        "--points-file", metavar="文件",
        help="从文本文件读取点位，每行一个: 名称,类型,备注（与界面粘贴格式一致；"
             "支持制表符分隔，# 开头为注释行；编码自动识别 UTF-8 / UTF-16 / ANSI）",
    )
    output_group = pull.add_argument_group("输出")
    output_group.add_argument(
        "-o", "--output", metavar="CSV路径",
        help="输出 CSV 文件路径；支持 {start} {end} {now} 占位符（格式 YYYYMMDD-HHMMSS）",
    )
    pull.add_argument(
        "--dry-run", action="store_true",
        help="只校验配置并打印执行计划，不连接数据库",
    )
    pull.add_argument(
        "-q", "--quiet", action="store_true",
        help="不打印过程日志（错误仍输出到 stderr，结果以退出码表示）",
    )
    pull.set_defaults(func=cmd_pull)

    test = sub.add_parser(
        "test",
        help="测试数据库连接",
        description="测试数据库连接：ping 服务器并确认数据库存在。",
    )
    _add_connection_args(test)
    test.set_defaults(func=cmd_test)

    points = sub.add_parser(
        "points",
        help="列出数据库点位目录（stdout 逐行输出 名称,measurement）",
        description=(
            "从数据库读取全部点位目录，按名称排序后逐行输出 名称,measurement 到 stdout，"
            "可重定向保存；日志输出到 stderr。"
        ),
    )
    _add_connection_args(points)
    points.add_argument(
        "--measure-tag", metavar="标签", help='点位标签名（默认 "measurePoint"）'
    )
    points.add_argument(
        "--filter", metavar="子串", help="只输出名称包含该子串的点位（不区分大小写）"
    )
    points.set_defaults(func=cmd_points)

    return parser


# ---------------------------------------------------------------------- #
# Entry point
# ---------------------------------------------------------------------- #
def _launch_gui() -> int:
    from .ui import main as gui_main

    gui_main()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return _launch_gui()

    parser = _build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:  # "gui" subcommand or nothing
        return _launch_gui()

    try:
        return args.func(args)
    except CliError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("\n已取消（Ctrl+C）", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:  # connection / query / file-write failures
        print(f"失败: {exc}", file=sys.stderr)
        return EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
