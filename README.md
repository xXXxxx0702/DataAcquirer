# DataAcquirer

一个**可视化**的 InfluxDB（v1 / InfluxQL）时序数据拉取工具。它把原始脚本
`loadDataV1` 中写死的连接地址、端口、时间范围、点位等参数，全部搬到了图形界面里，
用户可以在界面上配置并一键拉取数据导出为 CSV。

## 功能

- 图形界面配置 **地址 / 端口 / 用户名 / 密码 / 数据库**
- 配置 **起始 / 终止时间**、**分段大小（小时）**、**UTC 偏移**、取值字段、点位标签列
- 可编辑的**点位表格**：增删改、启用/停用、从剪贴板批量粘贴（`点位,类型,备注`）
- **点位自动联想**：点击「加载点位目录」从数据库读取全部点位后，在点位单元格输入
  部分字符即弹出下拉提示，选中后**自动填入对应的数据类型（measurement）**
- 一键**测试连接**
- 后台线程拉取，**进度条 + 实时日志**，可随时**取消**
- 自动按时间分段查询、去重、排序、合并，导出为 **CSV（UTC+本地时间还原）**
- **保存 / 载入** JSON 配置预设

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

```powershell
python run.py
```

或安装为包后：

```powershell
pip install -e .
data-acquirer        # 或 python -m data_acquirer
```

## 项目结构

```
DataAcquirer/
├── run.py                       # 启动入口
├── pyproject.toml               # 打包/依赖元数据
├── requirements.txt
├── config/
│   └── presets/
│       └── example.json         # 示例配置（武昌5#炉一级减温水）
├── output/                      # 导出的 CSV（运行时生成）
└── src/
    └── data_acquirer/
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
  与原脚本行为一致。
- CSV 使用 `utf-8-sig` 编码，便于 Excel 直接打开中文表头。
- 代理在拉取前会被强制禁用（`NO_PROXY=*`），与原脚本保持一致。
```
