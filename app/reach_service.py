"""发布带资料身份与查询身份的往返可达性分析结果。

用法：
    python -m app.reach_service 资料.json 查询.json              # 精确逐分钟并集
    python -m app.reach_service 资料.json 查询.json --sample 60  # 抽样近似（带误差界）
    python -m app.reach_service --update 旧资料.json 新资料.json 查询.json
        # 停运/封站等变更后的受影响范围圈定与增量重估
"""
import copy
import json
import sys
from pathlib import Path
from .catalog import load
from .service import SCHEMAS
from . import reach


def inspect_roundtrip(payload, query, sample_step=None):
    """对单次查询求值，输出绑定input_digest与query_digest的结果。"""
    snapshot = load(payload, SCHEMAS)
    net = reach.build_network(snapshot)
    if sample_step is not None:
        result = reach.evaluate_sample(net, query, sample_step)
    else:
        result = reach.evaluate(net, query)
    return {'schema_version': 1, **result}


def update_roundtrip(old_payload, new_payload, query):
    """资料变更后的增量重估：圈定受影响机会，只重估这些机会并合并。

    旧结果（绑定旧input_digest）不被修改；合并结果绑定新资料的input_digest。
    """
    old_snapshot = load(old_payload, SCHEMAS)
    new_snapshot = load(new_payload, SCHEMAS)
    old_net = reach.build_network(old_snapshot)
    old_result = reach.evaluate(old_net, query)
    diff = reach.diff_snapshots(old_snapshot, new_snapshot)
    scope = reach.plan_update(old_net, diff, old_result=old_result,
                              max_boardings=old_result['query']['max_boardings'])
    new_net = reach.build_network(new_snapshot)
    if isinstance(query, dict) and query.get('opportunities'):
        gone = sorted(set(query['opportunities']) - set(new_net.opps))
        if gone:
            raise ValueError(f'查询引用的机会在新资料中已撤回: {gone}')
    if scope['full_recompute']:
        merged = reach.evaluate(new_net, query)
        reused = []
    else:
        partial = reach.evaluate(new_net, query,
                                 only=set(scope['opportunities']) & set(new_net.opps))
        merged = reach.merge_results(old_result, partial,
                                     removed=scope['removed_opportunities'])
        reused = sorted(set(merged['opportunities']) - set(scope['opportunities']))
    return {'schema_version': 1,
            'diff': diff,
            'update_scope': scope,
            'reused_opportunities': reused,
            'result': merged}


def _read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def main():
    args = sys.argv[1:]
    if not args:
        raise SystemExit('用法: python -m app.reach_service 资料.json 查询.json [--sample 步长分钟]\n'
                         '     python -m app.reach_service --update 旧资料.json 新资料.json 查询.json')
    if args[0] == '--update':
        if len(args) != 4:
            raise SystemExit('--update 需要 旧资料.json 新资料.json 查询.json')
        output = update_roundtrip(_read_json(args[1]), _read_json(args[2]), _read_json(args[3]))
    else:
        if len(args) < 2:
            raise SystemExit('请指定资料JSON路径与查询JSON路径')
        sample_step = None
        rest = args[2:]
        if rest:
            if rest[0] == '--sample' and len(rest) == 2:
                sample_step = int(rest[1])
            else:
                raise SystemExit('无法识别的参数')
        output = inspect_roundtrip(_read_json(args[0]), _read_json(args[1]), sample_step)
    print(json.dumps(output, ensure_ascii=False, allow_nan=False, indent=2))


if __name__ == '__main__':
    main()
