# DataAcquirer

一个**可视化**的 InfluxDB（v1 / InfluxQL）时序数据拉取工具。它把原始脚本
`loadDataV1` 中写死的连接地址、端口、时间范围、点位等参数，全部搬到了图形界面里，
用户可以在界面上配置并一键拉取数据导出为 CSV。

除图形界面外，还提供**命令行模式**（`pull` / `test` / `points` 子命令），可以在
无界面环境下按预设文件或纯参数拉取数据，方便脚本调用与计划任务定时拉取，
详见[命令行用法](#命令行用法无界面拉取)。

## 功能

- 图形界面配置 **地址 / 端口 / 用户名 / 密码 / 数据库**
- 配置 **起始 / 终止时间**、**分段大小（小时）**、**UTC 偏移**、取值字段、点位标签列
- 可编辑的**点位表格**：增删改、启用/停用、从剪贴板批量粘贴（`点位,类型,备注`）
- **点位自动联想**：点击「加载点位目录」从数据库读取全部点位后，在点位单元格输入
  部分字符即弹出下拉提示，选中后**自动填入对应的数据类型（measurement）**
- **自动连接与记忆**：启动时恢复上次关闭前的配置并自动测试连接；连接成功后
  **自动加载点位目录、启用联想**（无需手动点按钮）；修改任意连接字段后，文本框
  失焦或回车即**自动重新测试连接**（带防抖，自动测试不弹窗，仅写日志）
- 一键**测试连接**（手动按钮，失败时弹窗提示）
- **服务器书签**：把多个 InfluxDB 连接保存为命名书签，下拉即可一键切换（切换后自动重连）
- **日期时间选框**：起始/结束时间拆分为年月日、时分秒选框，支持直接输入、上下箭头和鼠标滚轮调整，并自动校正每月有效天数
- **时间快捷范围**：除近1/2/7/14/30天、近3月/6月/1年外，可自行输入“近 N 小时”（支持上下箭头、鼠标滚轮和回车应用）；一键将结束时间设为当前系统时间、开始时间往前推对应时长（月/年按自然日历回推）
- 点位列表支持**一键清空**
- 后台线程拉取，**进度条 + 实时日志**，可随时**取消**
- 自动按时间分段查询、去重、排序、合并，导出为 **CSV（UTC+本地时间还原）**
- **CSV 点位备注**：有备注的点位以 `点位名（备注）` 作为导出列名，无备注点位保持原名；数值数据和单行表头结构不变
- **保存 / 载入** JSON 配置预设
- **命令行模式**：`pull` / `test` / `points` 子命令无界面拉取、测连、导出点位目录；
  支持预设 JSON + 参数覆盖、服务器书签、`--last 24h` 相对时间、输出文件名时间占位符、
  `--dry-run` 预检和明确的退出码，适合脚本与计划任务

## 与原脚本的对应关系

| 原脚本 | 本工具 |
| --- | --- |
| `InfluxDBClient(host, port, user, pwd, db, proxies=…)` | “连接配置”区域 + 自动禁用代理 |
| `points = {点位: 类型}` | “点位列表”表格（点位 → measurement 类型） |
| `start_time` / `end_time` | “时间范围”区域 |
| `24 * (i+1)` 小时分段循环 | “分段(小时)”参数 |
| `timedelta(hours=8)` 时区偏移 | “UTC偏移(小时)”参数 |
| `SELECT "value" FROM "{type}" WHERE ("measurePoint"='{k}') …` | 取值字段 / 点位标签 / 类型 参数化 |
| `df.to_csv(fnm)` | “输出”区域选择 CSV 路径 |

## 安装

需要 Python 3.9+（Tkinter 随官方安装包自带，无需额外安装）。

```powershell
pip install -r requirements.txt
```

## 运行

**最简单：双击 `启动.bat`。** 它会自动检测 Python、首次运行时自动安装依赖，
然后以无终端窗口的方式启动图形界面。

也可以用命令行：

```powershell
python run.py            # 不带参数 = 启动图形界面
python run.py pull -c config/presets/example.json   # 带子命令 = 命令行模式（见下文）
```

或安装为包后：

```powershell
pip install -e .
data-acquirer        # 或 python -m data_acquirer；三种入口用法完全一致
```

## 命令行用法（无界面拉取）

`run.py` / `data-acquirer` / `python -m data_acquirer` 三个入口共用同一套命令行：
**不带参数启动图形界面**（双击 `启动.bat` 不受影响），带子命令则进入无界面模式。
下文以 `python run.py` 为例。

| 子命令 | 作用 |
| --- | --- |
| `pull` | 按配置拉取数据并导出 CSV |
| `test` | 测试连接（ping 服务器并确认数据库存在） |
| `points` | 读取点位目录，按 `名称,measurement` 逐行输出到 stdout |
| `gui` | 显式启动图形界面（等同于不带参数） |

每个子命令都可用 `--help` 查看完整参数，如 `python run.py pull --help`。

### 配置来源与优先级

连接与拉取参数按下面的顺序逐层覆盖（后者覆盖前者）：

1. 内置默认值；
2. `-c/--config` 指定的 JSON 预设（`config/presets/*.json`，与界面「保存/载入配置」
   格式相同；也可以直接用 `config/last_session.json`，即界面上次关闭时的配置）；
3. `--bookmark 名称`：使用界面里保存的服务器书签（`config/bookmarks.json`）覆盖连接五项；
4. 单项参数：`--host` `--port` `--username` `--password` `--database`、
   `--start` `--end` `--last`、`--chunk-hours` `--utc-offset` `--value-field`
   `--measure-tag`、`--point` `--points-file`、`-o/--output`。

### pull 常用参数

| 参数 | 说明 |
| --- | --- |
| `--start` / `--end` | 起止时间（本地时间），如 `"2026-07-01 00:00:00"` |
| `--last N单位` | 拉取「最近一段」：结束取当前系统时间，单位 `h`小时 `d`天 `mo`月 `y`年，如 `24h`、`7d`、`3mo`、`1y`（月/年按自然日历回推）；与 `--start/--end` 互斥 |
| `--point "名称[,类型[,备注]]"` | 追加一个点位，可多次使用，类型缺省 `Float`；与界面粘贴格式一致 |
| `--points-file 文件` | 从文本文件读取点位：每行 `名称,类型,备注`（制表符也可作分隔，`#` 开头为注释；编码自动识别 UTF-8 / UTF-16 / ANSI，记事本与 PowerShell 重定向生成的文件均可直接用）。给出 `--point`/`--points-file` 任意一项后将**整体替换**预设中的点位表 |
| `-o/--output 路径` | 输出 CSV 路径，支持 `{start}` `{end}` `{now}` 占位符（展开为 `YYYYMMDD-HHMMSS`），避免定时任务互相覆盖 |
| `--dry-run` | 只校验配置并打印执行计划（服务器、分段数、点位清单、输出路径），不连接数据库 |
| `-q/--quiet` | 不打印过程日志（错误仍输出，结果以退出码表示） |

### 示例

```powershell
# 用预设文件拉取（等同于在界面载入该预设后点“开始拉取”）
python run.py pull -c config/presets/example.json

# 同一预设，只改时间范围和输出文件
python run.py pull -c config/presets/example.json --start "2026-07-01 00:00:00" --end "2026-07-08 00:00:00" -o output/week.csv

# 不用预设：书签连接 + 最近24小时 + 命令行点位（适合计划任务）
python run.py pull --bookmark 武昌电厂 --last 24h --point "B5_ZQWD,Float,主汽温度" --point B5_ZZQLL -o "output/日报_{end}.csv"

# 先预检配置（不连库），确认无误后再真正拉取
python run.py pull -c config/presets/example.json --dry-run

# 测试连接 / 导出点位目录（--filter 按名称子串过滤，不区分大小写）
python run.py test --bookmark 武昌电厂
python run.py points --bookmark 武昌电厂 --filter B5_ > output/points.csv
```

### 退出码与日志

过程日志输出到 **stderr**；`points` 的目录清单、`--dry-run` 的执行计划输出到
**stdout**，可安全重定向。退出码便于脚本判断结果：

| 退出码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 2 | 参数或配置错误（含 `--dry-run` 校验失败、书签/配置文件不存在） |
| 3 | 连接、查询或写文件失败（如输出 CSV 正被 Excel 占用） |
| 4 | 用户按 Ctrl+C 中断 |

### 配合 Windows 计划任务

计划任务的工作目录不固定，**路径请写绝对路径**（`-c`、`--points-file`、`-o`）：

```powershell
schtasks /Create /TN "DataAcquirer每日拉取" /SC DAILY /ST 06:00 /TR "python D:\Projects\DataAcquirer\run.py pull -c D:\Projects\DataAcquirer\config\presets\example.json --last 24h -o D:\Projects\DataAcquirer\output\daily_{end}.csv -q"
```

> 提示：`--password` 明文参数会留在命令行历史/任务定义中，建议连接信息优先放在
> 预设 JSON 或书签里。

## 项目结构

```
DataAcquirer/
├── 启动.bat                     # 双击启动（自动装依赖 + 无终端启动 GUI）
├── run.py                       # 启动入口（无参数=图形界面，带子命令=命令行模式）
├── pyproject.toml               # 打包/依赖元数据
├── requirements.txt
├── config/
│   ├── last_session.json        # 上次关闭时的配置（启动自动恢复，自动生成）
│   ├── bookmarks.json           # 服务器书签（自动生成）
│   └── presets/
│       └── example.json         # 示例配置（武昌5#炉一级减温水）
├── output/                      # 导出的 CSV（运行时生成）
└── src/
    └── data_acquirer/
        ├── __main__.py          # python -m data_acquirer 入口（转发到 cli）
        ├── cli.py               # 命令行模式（pull / test / points 子命令）
        ├── paths.py             # config 目录等共享路径（GUI 与 CLI 共用）
        ├── bookmarks.py         # 服务器书签的存取（config/bookmarks.json）
        ├── config.py            # AcquireConfig / PointSpec 配置模型 + 校验 + JSON 读写
        ├── core/
        │   └── puller.py        # InfluxDB 拉取逻辑（分段、时区、合并、导出）
        └── ui/
            ├── app.py           # 主窗口
            ├── points_table.py  # 可编辑点位表格
            ├── autocomplete.py  # 点位输入的联想匹配下拉控件
            └── worker.py        # 后台线程 + 消息队列 + 取消（拉取/测连/点位目录）
```

## 说明

- 时间均按**本地时间**输入，工具内部按 `UTC偏移` 减去偏移后查询，结果再加回，
  与原脚本行为一致（图形界面与命令行模式共用同一套拉取逻辑）。
- CSV 使用 `utf-8-sig` 编码，便于 Excel 直接打开中文表头。
- 代理在拉取前会被强制禁用（`NO_PROXY=*`），与原脚本保持一致。
```
