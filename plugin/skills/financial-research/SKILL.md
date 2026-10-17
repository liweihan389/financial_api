---
name: financial-research
description: 查询本机季度财务与 CRSP 日度行情、计算 PE 和财务比率、技术指标及收益回撤、核查 SEC 财务补充、按历史时点筛选或导出研究数据时使用。
---

通过 market-fundamentals MCP 查询本地数据库。除状态工具外，工具接受 request 对象。先检查 get_data_status 和 get_market_data_status 的版本、覆盖、scope 和警告；sample/mixed 不能代表完整市场或连续历史，也不是实时行情。

- 公司使用 search_companies 消歧后的字符串 gvkey；证券使用 search_securities 消歧后的字符串 PERMNO。同一公司不同股类、重复使用的代码不能混用。get_field_dictionary 默认财务字典，source=market 查询行情字段。
- 季度原值和指标分别用 get_financials/get_metrics；日度原始价格或分配事件用 get_daily_prices，events=true 单独查询事件。原始 DlyPrc 未复权，DlyFacPrc 是当日因子，不是累计因子；DisOrigCurType 是分配币种，不是报价币种。
- 日度估值用 get_daily_metrics，提供 as_of 或完整 start/end。保留 matching_candidates、matching_status、matching_reason、财务期间、价格日期、披露日来源及生效日。CUSIP8/9 加历史连续性和季末价格/每股尺度核对是研究推断，不能称为官方 CCM 匹配。发行人前六位、歧义和单位未核验候选不能补算 PE/PB/PS；人工关联也不能跳过单位检查。
- 通用前复权、后复权以及普通K线默认包含股息（basis=total_return）。用户提到技术图、技术分析、均线或其他技术指标时默认排除普通现金股息（basis=price_return）。用户明确要求含/不含股息或指定basis时，明确要求优先，不能被用途默认覆盖。
- 通用复权OHLC用get_adjusted_prices，adjustment=forward前复权（默认）或backward后复权；purpose=general默认含股息，purpose=technical默认排除普通现金股息。前复权锚在所请求区间最后实际观察日，后复权锚在首个实际观察日；保留expected_anchor_date、basis_source、adjustment_policy_version、source_return_field与警告。分页不改变锚点，缺失或间断不得拼接。后复权是对首日原价归一化的收益路径；不将复权价格称作历史实际成交价。
- get_technical_indicators 提供 SMA20/60/120/250、EMA12/26、MACD12/26/9、Wilder RSI14/ATR14、布林带20±2倍总体标准差及21/63/126/252日动量/收益/波动率。默认 price_return 排除普通现金股息，以最后实际观察日为复权锚；basis=total_return 可选。保留 anchor_date、parameters、definitions、unavailable，跨缺失或间断不拼接。EMA有预热，复现需相同日期区间与参数。
- get_return_statistics 默认总收益、首个观察日收盘至最后收盘，波动率采用样本标准差乘 sqrt(252)，最大回撤限定查询区间；中断收益返回原因。
- 历史日度筛选用 screen_securities，明确 as_of 和受控 filters；默认普通股/ADR，可选 ETF/CEF/ALL。sample 中多数证券只有快照，技术筛选仅覆盖历史足够的证券。
- export_market_dataset 支持指定证券池的 prices/adjusted_prices/technical/daily_metrics 日度范围，或 as_of 单日行情/筛选结果；export_dataset 保留季度财务导出。返回本地绝对路径或 download_url，并说明 metadata.truncated；每次最多100,000行，股票池最多1,000证券。adjusted_prices导出也接受adjustment、purpose与basis，通用默认含股息；technical导出默认排除普通现金股息，可显式指定basis覆盖。

财务和估值口径：

- 默认 historical_approx；research 使用事后研究数据；strict 不支持。缺失披露日按普通季度90天、Q4 120天估算。日度财务从披露后的下一观察交易日生效；观察交易日来自已导入范围。导入代次不是原始披露版本，历史模式仍存在重述偏差。
- 季末价格时效默认120天；日度价格默认5个自然日且必须有有效成交价。停牌/退市和冲突受限制。财务币种跨境不自动换汇。
- 季度 currency_policy=infer_curcdq 默认按价格行 curcdq 推断；日度按 CRSP 美国股票价格定义推断USD并保留未认证标记。verified_only 仅接受已有精确证据，不能把默认推断当成认证。币种验证不代表所有指标输入均已验证。
- share_policy=infer_compustat 默认将ADR EPS/股数视为 ADS 等价尺度，不再乘除 adrrq；保留 assumed_ads_equivalent、share_basis_verified=false、adr_ratio_applied=false。verified_only 要求已有审核ADR映射。比例缺失、尺度切换、币种冲突、缺季或拆股核对失败等导致空值，不自行补算。
- 已核对 BHP 2024-12-31 EPS 为半年，不能按单季度乘4或四季加总；供应商TTM可用。PBR 2025-09-30 股数混合普通/优先股，PE可用，PB/PS留空。其他公司字段差异保留 issuer_numeric_difference_unresolved，不称为公司公告已验证值。
- 默认稀释EPS、包含非常项目；季度年化PE为机械年化。财务比率返回小数，展示百分数乘100。负盈利PE不进入默认正PE筛选。
- 复现需保留 batch_id、database_generation、market_generation（请求参数 generation）、financial_generation、计算参数及规则/证据版本，且使用相同软件版本。

示例：get_daily_metrics(request={"security":"88490","as_of":"2025-09-30","mode":"research"})；get_technical_indicators(request={"security":"10026","start":"2006-01-05","end":"2006-01-09"})。

本插件提供历史研究数据与计算，不包含实时行情、交易执行或交易策略回测器。管理界面 http://127.0.0.1:8765/ui 支持手动上传、导入任务和请求历史。

SEC 财务补充（0.3.0）：

- 先看 get_sec_status：未配置 SEC_USER_AGENT 时不得声称联网更新已经运行；未初始化时不得声称已补全财务。公司仅限现有可靠 CIK，补充期末日固定晚于 2026-02-28。原始事实可含更早推导证据，但不能当作旧期间补充。
- 普通财务/指标/筛选请求默认 data_source=combined；同财务期 Compustat 整期优先，不能用 SEC 回填其空字段。data_source=compustat 保持供应商查询，sec 可检查独立补充。旧显式 generation/batch_id 请求默认供应商；显式 combined 复现必须固定供应商代次、sec_generation 与 rule_version。日度请求还需市场代次。
- get_sec_facts 读取原始标准标签与申报号，company 参数为 CIK；get_sec_field_mappings 查逐字段状态；get_sec_issues 查看证据。status 中映射覆盖不代表实际申报覆盖，不能承诺补齐 679 列。
- historical_approx 先按当时可见申报筛选，再选择版本，并排除推断字段。缺接受时间用申报日后一天，日度再按下一观察交易日生效。research 可用有规则候选，明确推断依据、全部输入事实和混合来源。未确认 6-K 定期性或提前 8-K 只能研究使用。
- 累计金额差分必须符合明确规则，EPS/加权股数不得机械相减。半年数据不是单季。币种不自动换算；不得填造 SEC 的 PRCCQ/AJEX/ADR 比例。未知 SEC ADS/拆股尺度只在研究模式中假定且标未核实；历史/verified_only 不得自行放宽。
- sync_sec_data 只在用户要求更新时调用，返回后台任务编号；未配置联系信息应提示网页配置。按范围 sample→initialized/full 初始化；全范围需首轮真实联网样本成功。不要把离线合成测试称为真实 SEC 对账。
- 管理页仅供待核项筛选、证据查看、导出与任务管理，不支持网页直接改数。人工规则须提供版本化配置及证据。SEC 只补财务，行情过期时 PE 仍可不可计算；strict 模式不支持。
