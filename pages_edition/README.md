# GitHub 免费版（Actions 定时采集 + Pages 静态看板）

不跑常驻服务：用 **GitHub Actions 每 5 分钟执行一轮采集**，结果写成
`docs/data/stock.json` 提交回仓库，**GitHub Pages** 直接托管静态看板。
全程 0 元，适合公开数据（云服务器库存等）。

与主项目共用同一套监控代码（`code_templates.py` 的内置模板）与
`emit()` 上报约定，迁移监控项只需要填参数。

## 目录结构

```
.github/workflows/pages-monitor.yml   定时采集工作流（必须放在仓库根的 .github/workflows/）
pages_edition/
├── monitors.json                     监控项配置：模板/参数/分组/站点信息
├── runner.py                         采集器：执行监控项 -> docs/data/stock.json
├── state.json                        跨次状态（连续失败/成功率），自动生成
└── README.md                         本文件
docs/
├── index.html                        静态看板（主项目 stock.html 的适配版）
├── favicon.svg
└── data/stock.json                   采集结果（Actions 自动更新）
```

## 部署步骤（一次性）

1. 仓库已包含本目录与工作流，推送到 GitHub 即可；
2. 开启 **Actions**：仓库页 → Settings → Actions → General →
   Actions permissions 选 "Allow all actions and reusable workflows" → Save；
3. 开启 **Pages**：Settings → Pages → Build and deployment →
   Source 选 **Deploy from a branch**，Branch 选 **main**、文件夹选 **/docs** → Save；
4. 等 Actions 跑完第一轮（或手动 Run workflow），
   访问 `https://<用户名>.github.io/web_monitor/` 即可看到看板。

> 注意：仓库必须 **Public** 才有免费 Actions / Pages；
> 私有仓库需要 GitHub Pro 且 Pages 属于付费功能。

## 增删监控项

编辑 `pages_edition/monitors.json`：

- **内置模板**：`"template": "huawei_stock"` 等，id 与主项目「新建监控」里
  的模板一致（http / huawei_stock / aliyun_flash_stock / aliyun_newuser_price /
  tencent_lighthouse_stock / jd_flash_stock / tcp / local_script），
  `"params"` 里填商品 id 等参数；
- **自定义脚本**：`"script": "monitors/my_monitor.py"`（相对 pages_edition/），
  脚本内用 `emit(ok, message, data)` 上报，与主项目监控代码写法一致；
- `"enabled": false` 可临时停用某一项；`"group"` / `"group_icons"` / `"group_order"`
  控制看板分组与图标。

改完提交推送到 GitHub，下一轮 Actions 自动生效（也可手动 Run workflow 立即执行）。

## 与主项目的差异

| | 主项目（常驻服务） | GitHub 免费版 |
| --- | --- | --- |
| 采集间隔 | 最低 3 秒，秒级实时 | 最低 5 分钟（Actions 限制） |
| 管理后台 / 登录 | 有 | 无（纯静态公开） |
| 通知推送 | 多渠道，秒级触发 | 未内置（可在 runner 里自行加） |
| 数据存储 | SQLite，最多 2 天历史 | stock.json 只存最新一轮 + 50 次成功率 |
| 费用 | 需要一台常开设备 | 0 元 |

## 常见问题

- **cron 是 UTC 时间**：北京时间 = UTC + 8（如北京 10:00 = `0 2 * * *`）；
- **60 天不活跃停用**：本项目每轮采集都会提交结果（相当于保持活跃），不会触发；
- **Actions 分钟数**：公开仓库免费不限（合理使用），私有仓库每月 2000 分钟免费额度；
- **仓库必须公开** 才能免费用 Pages；
- 监控脚本里需要的第三方库（如 requests）记得在 workflow 的安装步骤里加上。
