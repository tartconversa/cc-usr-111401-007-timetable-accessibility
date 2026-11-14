# 资料与现有计算约定

## 修订快照

schema_version为整数1，metadata为业务方案字典，collections为业务集合字典。每条记录有非空字符串id和正整数revision。
同集合相同id取最高revision；同id同revision内容不同即冲突，即便存在更高修订也拒绝。deleted=true为撤回，之后更高修订可恢复。
撤回记录不要求携带业务字段。输入顺序与相同记录重试不改变语义；非有限数字拒绝。
Snapshot.rows返回独立副本；input_digest绑定有效修订和全部metadata。输出data_version沿用metadata，不由程序自造资料版本。
既有摘要入口为app.service.inspect(payload)，输出schema_version、input_digest、data_version和summary。

## 业务资料

walks 含from、to、minutes，均为有向边，minutes非负整数。trips 含service_day、stops，stops按班次顺序给node、arrive、depart，时刻为相对service_day零点的分钟，可超过1440。机会含node、open、close、weight，窗口时刻是研究统一分钟轴；同一id是同一机会。metadata.day_offsets 将服务日映射到统一分钟轴。min_transfer 是非首次上车的最短换乘分钟；步行不重置已经上车的事实。baseline.analyze 仅按步行图从origin计算距离。

各集合必要字段：{"walks": ["from", "to", "minutes"], "trips": ["service_day", "stops"], "opportunities": ["node", "open", "close", "weight"]}。

## 当前能力

app.baseline.analyze(snapshot)产生已有摘要，app.service.inspect负责绑定输入身份；新增分析可以增设独立入口，已有接口及上述数据语义保持兼容。

## 时刻可达性扩展（app.reach / app.reach_service）

### 时间表示

统一分钟轴（非负整数）。班次绝对时刻 = metadata.day_offsets[service_day] + 相对分钟，可超过1440，跨午夜服务日由此自然表示；机会open/close与查询窗口同轴。出发范围按整数分钟闭区间处理，可行比例 = 可行分钟数 / 范围分钟数。

### 查询

{"depart_range": [T0,T1], "arrive_window": [A0,A1], "min_stay": 分钟, "budget": 分钟, "max_boardings": 可选默认3, "opportunities": 可选id过滤}。停留约束：max(到达时刻, open) + min_stay <= close；到达窗口为硬约束，提前到达不计入窗口，但到达后在机会门口等待开门计入行程。总耗时：回到原点时刻 - 出发时刻 <= budget。换乘：非首次上车需满足min_transfer，步行不重置已上车状态。

### 配对语义

去程选项 = (到达机会时刻a, 最晚出发时刻L, 实际行程)；回程选项 = (回到原点时刻h, 最晚就绪时刻R, 实际行程)。出发t可行当且仅当存在配对：t <= L、ready <= R、h <= t + budget，其中ready = max(a, open) + min_stay。逐分钟精确求并集：不以全天最短一次行程代表全天，也不把去程最优与回程最优拼成不可能的往返。多个站点通向同一机会时按并集计算，同一机会只计一次。每个可行区间给出见证行程：实际班次、步行、等待、换乘构成。

### 损失分类

unreachable 不可达（无任何去程路径）；closed 机会关闭（有去程但无可行停留窗口）；one_way 单向服务（有可行停留但无任何回程）；missed 漏车或超预算（双向资源都存在但配对失败）。分钟级原因：no_outbound 去程赶不上、missed_return 回程赶不上、over_budget 超出总耗时预算。汇总输出各状态的机会数与权重、各原因损失的分钟数，以及任一机会可行的出发分钟并集。

### 批量、身份与增量更新

Network一次构建、多查询复用；去/回程选项缓存与查询参数无关，窗口/预算只在配对时进入。结果绑定input_digest与query_digest，固定资料版本的结果不因后来的停运记录改变（修订产生新digest、新结果，旧结果对象不被修改）。diff_snapshots比较两个快照；plan_update圈定重估范围：纯移除（停运、封站、撤回）按缓存行程的资源引用精确圈定——未引用被移除资源的机会其帕累托选项完整，结果必然不变；新增或恢复资源只可能影响未完全可行的机会；metadata的origin/min_transfer/day_offsets变化触发全量重估。merge_results把增量结果并入旧结果并返回新对象。

### 抽样近似

--sample 步长：仅在抽样点上精确求值。相邻抽样点之间若可能含有可行性跳变点（由班次、步行与预算导出的候选断点超集判定），该区间计入不确定范围，输出可行比例的[下界, 上界]。离散取整点只是带误差界的近似，不能当成整个出发范围的精确覆盖率；精确结果以默认模式为准。
