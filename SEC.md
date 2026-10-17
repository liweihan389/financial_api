# SEC 财务补充模块（0.3.1）

SEC 使用独立数据库与原始快照，默认补充**财务期末日晚于 2026-02-28**的新期间。Compustat 同公司同财务期整期优先，即使某些字段为空也不回填；显式 `data_source=sec` 可查看被替代的 SEC 记录。原始供应商文件和数据库保持不变，不新增 SEC-only 公司。

## 开始使用

启动应用后打开 http://127.0.0.1:8765/ui 。SEC 面板可查看状态、待核项、证据和导出；上传区域选择 SEC 财务补充，支持 Company Facts、Submissions JSON 和官方多 JSON ZIP。文件不会展开到任意路径。离线导入无需联系信息。

联网先在面板填写“应用名称 + 联系邮箱”，或者设置 `SEC_USER_AGENT="YourApplication contact@your-domain.example"` 后启动。**环境变量优先于网页设置**。未提供有效联系邮箱时，下载与定时任务均未启用。网页设置保存在 `storage/sec/settings.json`，权限为 0600，请求日志隐藏联系信息；ZIP 应用包、插件包均不含配置、数据或凭据。

先选择约 20 家样本进行联网初始化。只有样本获取成功后才开放全范围初始化；样本范围取预设 CIK 与现库可靠身份的交集。初始化先查官方季度申报索引，按公司与日期筛选，再获取 Company Facts/Submissions；历史申报页按日期加载。每日北京时间 20:00 更新已登记公司，重启最多补跑最近一个遗漏时段，任务不重叠。服务必须运行；多实例部署须只启用一个更新服务。初始完整初始化另外手动触发。

请求默认每秒 2 次，30 秒超时，最多 4 次尝试。缓存保存 ETag、原始响应与指纹；成功发布后保存公司检查点，失败公司沿用原版并进入重试列表。429 遵守 Retry-After；403 停止本次来源任务并报告。版本发布采用独立文件校验和原子清单切换，旧文件供正在执行的查询与复现使用。

命令行示例（在应用目录执行）：

```sh
financial-data --data-dir /data import-sec companyfacts.zip
financial-data --data-dir /data import-sec submissions.zip
financial-data --data-dir /data sec-status
financial-data --data-dir /data sync-sec --scope sample
financial-data --data-dir /data sync-sec --scope initialized
```

命令行同步等待后台工作完成并输出任务结果；网页/MCP 同步立即返回后台任务编号。上传后仍需点击导入。官方两个 ZIP 可分次导入，Submissions 到达后会重新清洗对应公司。当前补充日期边界始终固定，不随后续 Compustat 更新移动。

## 清洗与标注

首版对 679 个原字段逐一标注映射状态：39 个可信映射、13 个推断规则、6 个不适用、621 个待核查。标签有语义差异时另按标签降级，例如 IFRS 母公司利润为研究候选。状态说明的是规则覆盖，并不保证申报中一定有该字段。其余字段不能凭名称相似度填数；没有经过核验的标签仍为待核查。普通股权益不由总权益自动替代，期末股数不由加权股数或 DEI 封面股数替代。

金额、股数转为百万，EPS 保留每股值；原 JSON（离线文件保留原字节）、SHA-256、CIK、标准标签、单位、起止日期、申报号、原始数值与推导事实 ID 均保留。原始事实包含边界前的证据，标准化补充记录不包含旧期间。原始文件单个成员上限 128 MiB，ZIP 展开合计上限 40 GiB；默认两个 DuckDB 连接各限制 1 GB。每家公司逐个处理，完整批次最终校验后发布。

季度、半年、累计和全年分别保留期间类型。仅可加总的金额项目可作同标签、同起点、同单位且覆盖新季度的累计差分；同申报号的自洽证据优先。跨申报差分标为推断，因为无法证明重述口径一致。未来才申报的先前累计值不能充当较早可见的差分证据。**EPS 与加权股数不作机械差分**；全年 EPS 只映射完整年度，不把半年当季度或全年。52/53 周财年按距名义季末不超过 7 天处理；不规则财期标为推断。已知数值冲突、单位错误或身份错误不可采用。

10-Q、10-K、20-F、40-F 及修订稿优先。6-K 的定期性无法由 Company Facts 单独证明时，和 8-K 提前业绩一样作为研究候选；可用版本化人工证据配置确认特定 6-K。Submissions 的接受时间按美国东部日期可见；缺少接受时间则取申报日后一天并标注。日度估值继续在可见日期后的下一**已导入观察交易日**生效，未导入交易日不推造。

`research` 可采用有规则的推断字段；`historical_approx` 排除推断输入，逐字段返回排除原因与证据。SEC 股数/EPS 的 ADR 单位可在研究模式中假定为 ADS 等价，不再次套 ADR 比例。未知拆股尺度也须标为未核实；历史估值和 `share_policy=verified_only` 需要股份尺度证据。已知冲突阻止估值。估值使用真实独立行情和供应商校验锚，**SEC 不填造 `prccq`、`ajexq` 或 ADR 比例**。

TTM/同比可以组合可比的 Compustat 与 SEC 季度，保留混合来源、全部实际输入事实与质量。币种不自动换算，缺季、半年 EPS、股类/拆股尺度无法确认会导致对应指标为空。SEC 更新财务不补行情；旧行情超过时效时 PE/PB/PS 仍为空。现有供应商历史快照、证券身份与元数据限制依然存在，`strict` 模式仍不支持。

## 查询、复现与接口

财务、指标、财务筛选及季度/日度导出新增：

- `data_source`: `combined`（普通请求默认）、`compustat`、`sec`。
- `sec_generation`: SEC 不可变文件名；`none` 明确固定无 SEC 版本。
- `rule_version`: 与 SEC 发布清单中的版本对应。

旧的显式 `generation`/`batch_id` 请求默认仍只查询 Compustat。显式合并复现必须提供供应商代次、SEC 代次和规则版本；还需保留计算口径及相同软件版本。响应 release_manifest 固定两源代次及规则版本，导出同时保存该清单。规则的原始配置保存在不可变版本中，改配置后旧版仍可读；`reclean-sec` 发布新的标准化版本。

```json
{"company":"001690","as_of":"2026-05-15","data_source":"combined","mode":"historical_approx","metrics":["debt_ratio","pe_ttm"]}
```

使用 `POST /v1/metrics` 或 MCP `get_metrics(request=...)`。返回财务期、价格日期、申报号、字段质量、推导事实、混合来源和不可计算原因。仅财务比率筛选不要求价格新鲜；PE 条件仍要求有效价格且盈利为正。

新增 REST：`GET /v1/sec/status`、`/facts`、`/mappings`、`/issues`、`/issues/export`；`POST /v1/sec/sync`；`PUT /v1/sec/settings`。事实/问题分页默认 100、最多 1,000，可按 CIK、字段或问题类型筛选。任务进度沿用 `/v1/admin/imports`。远程监听继续强制认证；网页不支持直接改数或逐项审批。

新增 MCP：`get_sec_status`、`get_sec_facts`、`get_sec_field_mappings`、`get_sec_issues`、`sync_sec_data`；加上原有 16 个工具共 21 个。

## 人工规则配置

规则为 `src/fundamentals/sec_rules.json`，仅允许明确标准标签、单位、期间类别、可加总标记和质量。证据配置为 `src/fundamentals/sec_policy.json`，按十位 CIK 保存。允许 `periodic_6k_accessions`（申报号 → 证据引用）和 `share_scale_by_period`（实际期末日 → `ajex_equivalent` 与 `evidence`）。后者是经过核对、与供应商拆股尺度一致的内部因子，**不是申报 AJEX，也不是 ADR 比例**。必须核对股类、ADS 单位及期间拆股后配置，不能默认填 1。网页仅供证据查看。

调整配置后重启服务，再执行：

```sh
financial-data --data-dir /data reclean-sec
```

配置内容指纹进入规则版本，旧版本保留原配置和原始事实。查看 status 的 `configured_rule_version`、`rule_version`、`reclean_required` 可发现配置与活动版本差异。

## 部署与验收

本仓库使用合成样本验证规则，不预装真实 SEC 或 Compustat 数据。先导入自己的季度财务，才能建立可靠 CIK 公司范围。发布流程验证空库启动、上传、后台导入、查询、导出和持久化；并在 Linux amd64 / arm64 分别构建。真实 SEC 数值对账须在配置联系信息后执行，合成样本不能代替正式对账。

字段数量是内置技术字典的规则覆盖，实际可用范围由用户导入字段与 SEC 标准标签决定。公开发布的币种和股份证据仅保留匹配指纹与类别限制，未附带源财务、价格或批次数值。

参考：[SEC API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)、[SEC 访问规则](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)。
