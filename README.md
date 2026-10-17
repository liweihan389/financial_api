# Financial API

可由用户导入数据的股市财务与日度行情服务，提供管理网页、REST API 和 21 个 MCP 工具。支持季度财务、PE TTM/静态 PE/PB/PS、历史筛选、含股息前后复权、技术指标、收益回撤、SEC 补充和 CSV/Parquet 导出。

**仓库、镜像和发布包均不预装数据。** 上传自己的 Compustat 季度 CSV/ZIP、CRSP 日度 CSV/ZIP 后使用。公司标识保留字符串，清洗与更新保留原始记录追溯和不可变版本。679 字段的技术字典、计算规则及核验指纹属于程序配置；不包含原始财务数值或行情观测。

## 推荐 Docker Compose 部署（直接使用已构建镜像）

推荐使用仓库中的 [compose.deploy.yaml](compose.deploy.yaml)。这个文件可独立使用，适合云服务器、NAS 和本机 Docker：固定镜像版本，自动选择 amd64/arm64，使用持久卷保存数据，并配置自动重启、正常停机等待和日志轮转。服务以镜像内的非 root 用户运行，继承镜像健康检查。

### 1. 保存部署文件

将以下内容保存为 `compose.deploy.yaml`，或者直接下载仓库同名文件：

```yaml
services:
  financial-api:
    image: ${FINANCIAL_IMAGE:-ghcr.io/liweihan389/financial_api:0.3.1}
    init: true
    ports:
      - "${FINANCIAL_BIND_ADDRESS:-127.0.0.1}:${FINANCIAL_PORT:-8765}:8765"
    environment:
      FINANCIAL_DATA_DIR: /data
      FINANCIAL_API_TOKEN: "${FINANCIAL_API_TOKEN:?Configure FINANCIAL_API_TOKEN in .env}"
      FINANCIAL_PUBLIC_URL: "${FINANCIAL_PUBLIC_URL:-http://127.0.0.1:8765}"
      FINANCIAL_ALLOW_FULL_IMPORT: "${FINANCIAL_ALLOW_FULL_IMPORT:-1}"
      SEC_USER_AGENT: "${SEC_USER_AGENT:-}"
    volumes:
      - financial-data:/data
    restart: unless-stopped
    stop_grace_period: 60s
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
volumes:
  financial-data:
```

`/data` 是应用的数据目录，`financial-data` 是 Docker 管理的持久卷。首次启动为空；上传文件、数据库、SEC 快照、历史记录及导出都会写入该卷。

### 2. 配置同目录的 `.env`

```dotenv
# 必填：填入自己生成的随机令牌，至少 32 个字符。
FINANCIAL_API_TOKEN=
FINANCIAL_IMAGE=ghcr.io/liweihan389/financial_api:0.3.1
FINANCIAL_BIND_ADDRESS=127.0.0.1
FINANCIAL_PORT=8765
FINANCIAL_PUBLIC_URL=http://127.0.0.1:8765
FINANCIAL_ALLOW_FULL_IMPORT=1
# 可选：SEC 应用名称和联系邮箱；留空时不启用联网更新。
SEC_USER_AGENT=
```

可用 `openssl rand -hex 32` 生成令牌，填到 `FINANCIAL_API_TOKEN=` 后面；留空时 Compose 会明确报错。若已克隆仓库，执行 `python3 scripts/configure.py` 即可自动创建包含随机令牌的 `.env`。不要把自己的 `.env` 上传到 GitHub。

**从其他电脑访问云服务器或 NAS**，需要同时修改两项：

```dotenv
FINANCIAL_BIND_ADDRESS=0.0.0.0
FINANCIAL_PUBLIC_URL=http://你的服务器IP:8765
```

然后访问 `http://你的服务器IP:8765/ui`，并确保服务器的网络配置允许访问该端口。使用 HTTPS 反向代理时，`FINANCIAL_PUBLIC_URL` 填实际的 HTTPS 域名；代理与服务同机时可保留 `127.0.0.1` 绑定。更改宿主机端口后，URL 中也要使用相同端口。

### 3. 启动和导入数据

在上述两个文件所在目录执行：

```sh
docker compose -f compose.deploy.yaml config --quiet
docker compose -f compose.deploy.yaml pull
docker compose -f compose.deploy.yaml up -d
docker compose -f compose.deploy.yaml ps
```

默认管理页为 http://127.0.0.1:8765/ui。在“服务连接”中输入 `.env` 的令牌，随后上传并导入自己的季度财务和日度行情文件即可。首次空库会显示导入引导。API 文档在 `/docs`，MCP 地址为 `/mcp`；两者的数据请求使用同一访问令牌。

常用维护命令：

```sh
# 查看日志
docker compose -f compose.deploy.yaml logs -f --tail=100
# 修改 .env 后应用配置，或重建容器（保留数据卷）
docker compose -f compose.deploy.yaml up -d
# 停止并移除容器，保留已导入数据
docker compose -f compose.deploy.yaml down
```

再次在同一目录启动会继续使用原卷。保持项目目录/Compose 项目名不变；迁移或改名时，请按 [部署与迁移说明](DEPLOYMENT.md#数据持久化与迁移) 指定原数据卷。只运行一个服务实例。需要升级镜像时，修改 `.env` 的 `FINANCIAL_IMAGE`，再执行 `pull` 和 `up -d`。**`down -v` 会删除数据卷；仅在确实要清空数据时使用。**

## Docker 启动（克隆仓库方式）

```sh
git clone https://github.com/liweihan389/financial_api.git
cd financial_api
python3 scripts/configure.py
docker compose up -d
```

`configure.py` 创建权限为 0600 的 `.env`，随机生成访问令牌，不覆盖已有配置。没有 Python 时，可复制 `.env.example` 为 `.env`，自行填写 `FINANCIAL_API_TOKEN`。**令牌不要提交到 GitHub。**

打开 http://127.0.0.1:8765/ui，将 `.env` 中的令牌填入“服务连接”，点击连接。服务空库也能正常打开，首页会提示导入顺序：

1. 上传 Compustat 季度财务 CSV/ZIP，预检后选择“财务数据”，点击导入。
2. 上传 CRSP 日度 CSV/ZIP，选择“CRSP 日度行情”和“完整读取上传文件”。样本选项只用于主动抽取样本。
3. 按需导入证券关联 CSV 或 SEC JSON/ZIP；查看任务进度、数据条数和请求历史。

任务完成后状态自动刷新，无需重建镜像。数据库、上传文件、SEC 快照、查询历史和导出均保存到 `/data` 持久卷，容器删除重建仍保留。没有行情时仍可查财务；估值或技术指标需要相应数据覆盖。

API 文档：http://127.0.0.1:8765/docs；MCP：http://127.0.0.1:8765/mcp。除网页和健康检查外，接口使用 `Authorization: Bearer <令牌>`。插件目录为 `plugin/`，配置其中 `mcp.json` 的地址及认证后连接；服务地址变化时也要修改插件配置。

## 已构建镜像

镜像地址：`ghcr.io/liweihan389/financial_api:0.3.1`，另有 `latest`；发布流程分别构建 Linux `amd64` 和 `arm64` 并合并多平台标签。

GitHub Release 提供两个架构的完整 Docker 压缩包，适合离线导入，另有源码包、插件包和 `SHA256SUMS`。如果 GHCR 要求登录，也可从公开 Release 下载匹配架构的压缩包：

```sh
# 普通 x86 云服务器：amd64；Apple Silicon / ARM 云服务器：arm64
sha256sum -c SHA256SUMS --ignore-missing
docker load -i financial-api-0.3.1-linux-amd64.tar.gz
docker tag ghcr.io/liweihan389/financial_api:0.3.1-amd64 ghcr.io/liweihan389/financial_api:0.3.1
docker compose up -d --pull never
```

ARM 环境将上述两个 `amd64` 改为 `arm64`。离线加载不需要运行编译工具或访问数据供应商。

## 从源码构建

```sh
python3 scripts/configure.py
docker compose up -d --build
```

或 `docker build -t financial-api:0.3.1 .`。Docker 使用两阶段构建、Python 3.12 和锁定依赖，运行阶段为非 root 用户，不复制测试、插件或任何运行时数据。允许构建上下文使用白名单，避免把误放在目录里的财务文件带入镜像。

## 数据格式与口径

- Compustat：字段名与导出 CSV 一致，至少包含 `gvkey,datadate,fyearq,fqtr,fyr,indfmt,datafmt,consol,curcdq`；为使用对应指标，还需要盈利、股数、价格及资产负债字段。CSV 可在 ZIP 中；排除 macOS 元数据。
- CRSP：使用现有 CIZ 日度导出格式，含 `PERMNO,DlyCalDt,DlyPrc` 以及收益、证券属性等相应字段。`DlyPrc` 是未复权价格；含股息复权需要 `DlyRet`，排除普通股息需要 `DlyRetx`。
- `market-links.template.csv`、`security_mappings.template.csv` 是只有表头的人工核验模板。仅在有证据时填写，不能凭相似代码强配。
- 原始数据不预先复权。通用前后复权默认含股息；技术图和技术指标默认排除普通现金股息，显式 `basis` 优先。
- 默认估值保留币种与 ADR 尺度研究假设；严格核验选项会排除未确认输入。负盈利 PE、陈旧价格、缺季、半年 EPS、股类或单位冲突均返回空值和原因。
- 历史模式为 `historical_approx`，仍有供应商重述与证券映射限制；不支持严格时点回测，不提供实时行情、换汇或交易执行。

与本机版一致的核验约束以字段/记录指纹匹配，发布版不含原价格、EPS、股数、利润或原导入批次。必须导入匹配数据才会命中已有核验；其他数据不会被自动认证。

## SEC 更新

SEC 仅补充现库可靠 CIK 的公司，财务期末日固定晚于 `2026-02-28`。同财务期 Compustat 整期优先，SEC 不回填该期空字段，不补行情。

`SEC_USER_AGENT` 为空时，联网和每日北京时间 20:00 更新都不启用；离线上传可用。先导入财务数据，再在网页填写“应用名称 + 联系邮箱”或在 `.env` 设置 `SEC_USER_AGENT`，进行约 20 家样本联网初始化。真实样本成功后才允许全范围初始化。运行服务内置调度器，每日检查已初始化公司的新申报，不创建外部定时任务。

推断字段只在 `research` 模式采用，历史筛选默认排除推断。字段映射、原始事实、问题证据和代次可查询。首版并不保证 679 字段都有标准 SEC 标签。更多口径见 [SEC.md](SEC.md)。

## 运维与测试

- 查看日志：`docker compose logs -f`；状态：`docker compose ps`。
- 停止：`docker compose down`。**不要使用 `down -v`，除非确实要删除所有已导入数据。**
- 迁移和备份、远程认证、端口配置、CLI 导入见 [DEPLOYMENT.md](DEPLOYMENT.md)。
- 只运行一个实例，避免多个服务同时修改操作历史与导入状态。
- 测试仅生成合成数据，不读取本机数据目录。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt -e .
.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_publication.py
python3 scripts/container_smoke.py --image financial-api:0.3.1
```

GitHub Actions 在发布前运行测试、发布内容检查，以及两个架构各自的真实容器“空库→导入→查询→导出→重建后保留”验收，然后推送 GHCR 和 Release 附件。
