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
