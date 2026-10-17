# 部署与迁移

## 服务配置

首次执行 `python3 scripts/configure.py` 生成 `.env`。默认绑定本机 `127.0.0.1:8765`，镜像中不内置访问令牌。

| 设置 | 用途 |
|---|---|
| `FINANCIAL_API_TOKEN` | 必填，API 与 MCP 访问令牌；与管理页输入一致 |
| `FINANCIAL_IMAGE` | 镜像版本或自行构建的标签 |
| `FINANCIAL_BIND_ADDRESS` | 默认 `127.0.0.1`；远程直接监听用 `0.0.0.0` |
| `FINANCIAL_PORT` | 宿主机端口，默认 `8765` |
| `FINANCIAL_PUBLIC_URL` | 对外完整地址，用于校验来源与生成下载链接 |
| `FINANCIAL_ALLOW_FULL_IMPORT` | 默认 `1`，完整读取上传文件；不代表数据供应商历史必然完整 |
| `SEC_USER_AGENT` | 应用名与联系邮箱；空值禁用联网 SEC 更新 |

远程部署推荐在服务前放置 HTTPS 反向代理，设置 `FINANCIAL_PUBLIC_URL=https://your-host.example`。保持访问令牌，代理保留 Host 和转发协议信息。`/ui` 为无数据静态页面，访问实际数据和上传操作需要认证。`/healthz` 仅提供服务存活信息。

访问文档 `/docs` 的交互请求需要 Bearer 认证；直接使用 REST/MCP 也必须提供 `Authorization: Bearer ...`。修改端口时同步更新对外 URL 和 `plugin/mcp.json`。部署配置与凭据保存在运行环境，不应打进镜像。

## 导入

推荐网页分块上传，默认每块 4 MiB，可以继续未完成上传。依次导入季度财务、日度行情、可选人工关联和 SEC；后台任务完成后即可查询。

已把原始文件放在服务器时，可以只读挂载输入目录，并使用同一数据卷离线导入：

```sh
docker compose run --rm -v /absolute/input:/input:ro financial-api financial-data --data-dir /data import /input/fundamentals.zip
docker compose run --rm -v /absolute/input:/input:ro financial-api financial-data --data-dir /data import-market /input/daily.zip --scope full
docker compose run --rm -v /absolute/input:/input:ro financial-api financial-data --data-dir /data import-links /input/links.csv
docker compose run --rm -v /absolute/input:/input:ro financial-api financial-data --data-dir /data import-sec /input/companyfacts.zip
```

运行中的服务可查询旧版，导入完成后通过不可变代次原子切换。CLI 原始文件必须能被容器用户 UID 10001 读取。只有网页上传是导入后台任务入口；不要启动第二个长期服务实例。原始 CSV/ZIP、数据库版本和导出会共同占用磁盘，更新时还需要一份数据库的临时空间。

技术字段字典已随程序提供，导入原格式文件无需本机 PDF。若另有字段定义 PDF，可在 CLI 导入时使用 `--dictionary /input/dictionary.pdf`。任何数据源版本都不应偷偷合并冲突行。

## 数据持久化与迁移

默认 Compose 管理名为 `<项目名>_financial-data` 的卷；容器内路径固定为 `/data`。首次运行为空，不从镜像复制任何样本。应用在该目录生成财务/行情版本、SEC 快照、上传/导出和操作历史。

如需宿主机目录，将 Compose 的卷挂载改为 `./data:/data`，先创建目录并确保 UID/GID 10001 可写。不要把数据目录放入构建白名单。直接使用既有兼容数据卷/目录也可恢复原有代次；不要求重导入 CSV。

一致备份需先停止服务，再备份整个数据卷，而不是只复制某个活动数据库：

```sh
docker compose stop
# 使用 docker volume ls 找到卷名；以下示例按实际项目名修改。
docker run --rm -v financial_api_financial-data:/data:ro -v "$PWD/backups:/backup" alpine tar czf /backup/financial-data.tar.gz -C /data .
docker compose start
```

恢复到新卷后，用该卷运行相同或兼容版本。恢复前保留已有数据副本。更新应用只替换镜像，不删除数据卷；旧不可变数据库用于复现请求，清理前确认没有引用。

## 镜像与发布

工作流为 `.github/workflows/publish.yml`。`main` 推送或手动运行后，先测试，再分别使用 x86/ARM 原生 Linux runner 构建并做真实容器验收，成功后推送：

- `ghcr.io/liweihan389/financial_api:<版本>-amd64`
- `ghcr.io/liweihan389/financial_api:<版本>-arm64`
- `ghcr.io/liweihan389/financial_api:<版本>` 与 `latest` 多平台索引

GitHub Release 同时上传两种架构的 Docker tar.gz、源码 ZIP、插件 ZIP 和 SHA-256 校验清单。包内没有 `.env`、上传文件或数据库。GHCR 默认访问权限可能需要登录；不方便登录时使用公开 Release 的离线镜像包即可。若希望匿名 `docker pull`，仓库所有者可在 GitHub Packages 的包设置中将可见性设为 Public。

CI 通过仓库的 `GITHUB_TOKEN` 发布包，无需保存个人令牌。后续正式版本请先提高 `pyproject.toml` 和部署/插件版本，再推送代码，避免不同内容复用同一版本标签。

## 边界

空库健康不等于已有数据，状态接口会明确返回 `not_initialized`。金融计算只有在导入所需字段与期间后才可用，无法计算的指标带原因。SEC 首次真实数值对账需要配置联系信息，不会随 Docker 启动自动下载全库。CRSP 样本模式不会被当成完整市场历史。
