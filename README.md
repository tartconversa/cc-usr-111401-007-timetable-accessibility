# 城乡双栖的时刻表可达性与往返机会分析

本项目提供服务日及跨午夜班次解释相关资料的现有业务摘要。当前代码执行资料修订读取与基础统计，不包含扩展分析。

app/catalog.py 按集合和记录键解析修订，拒绝同修订冲突；app/baseline.py 承载当前业务计算；app/service.py 输出摘要及输入摘要哈希。
数据字段及现有接口见 [资料契约](docs/data-contract.md)。fixtures/sample.json 是合成小数据，不对应真实村庄或个人。
运行环境为 Python 3.14，现有模块仅使用标准库。

读取并计算当前摘要：

    python -m app.service fixtures/sample.json

按出发时刻查询就业与研学机会的往返可达性（精确逐分钟并集，fixtures/roundtrip.json 为合成小数据）：

    python -m app.reach_service fixtures/roundtrip.json fixtures/query.json

抽样近似（只在整点求值，输出带误差界的可行比例区间）：

    python -m app.reach_service fixtures/roundtrip.json fixtures/query.json --sample 60

停运或封站后的受影响范围圈定与增量重估（旧结果绑定旧资料身份，不被修改）：

    python -m app.reach_service --update 旧资料.json 新资料.json fixtures/query.json

运行当前模块检查：

    python -m unittest discover -s tests -v

检查源码编译：

    python -m compileall -q app tests
