"""按出发时刻查询的就业与研学机会往返可达性分析。

时间表示：统一分钟轴（非负整数）。班次绝对时刻 = metadata.day_offsets[service_day] + 相对分钟，
可超过1440（跨午夜服务日由此自然表示）；机会 open/close 与查询 depart_range/arrive_window 同轴。
出发范围按整数分钟闭区间处理，可行比例 = 可行分钟数 / 范围分钟数。

配对语义：去程选项为（到达机会时刻 a，最晚出发时刻 L，实际行程），回程选项为
（回到原点时刻 h，最晚就绪时刻 R，实际行程）。出发时刻 t 可行当且仅当存在配对满足
t <= L、ready <= R、h <= t + budget，其中 ready = max(a, open) + min_stay。
逐分钟精确求并集：不以全天最短一次行程代表全天，也不把去程最优与回程最优拼成不可能的往返。
多个站点通向同一机会时按并集计算，同一机会只计一次。

损失分类：unreachable 不可达（无任何去程路径）；closed 机会关闭（有去程但无可行停留窗口）；
one_way 单向服务（有可行停留但无任何回程）；missed 漏车或超预算（双向资源都存在但配对失败）。
分钟级原因：no_outbound 去程赶不上、missed_return 回程赶不上、over_budget 超出总耗时预算。

批量与更新：Network 一次构建、多查询复用；去/回程选项缓存与查询参数无关。
结果绑定 input_digest 与 query_digest，固定资料版本的结果不因后来的停运记录改变。
纯移除（停运、封站）按缓存行程的资源引用精确圈定受影响机会；新增资源只可能影响
未完全可行的机会；metadata 的 origin/min_transfer/day_offsets 变化触发全量重估。
"""
import copy
import hashlib
import heapq
from .catalog import canonical

_REASONS = ('unreachable', 'closed', 'one_way', 'no_outbound', 'missed_return', 'over_budget')
_MAX_RANGE_MINUTES = 200000
_MAX_STATES = 200000


def _int(value, name, lo=0):
    if type(value) is not int or value < lo:
        raise ValueError(f'{name}应为不小于{lo}的整数')
    return value


def check_query(query):
    """校验并规范化查询：出发范围、到达窗口、最短停留、总耗时预算。"""
    if not isinstance(query, dict):
        raise ValueError('查询应为对象')

    def pair(name):
        v = query.get(name)
        if not (isinstance(v, list) and len(v) == 2):
            raise ValueError(f'{name}应为[起, 止]二元数组')
        a, b = _int(v[0], name), _int(v[1], name)
        if a > b:
            raise ValueError(f'{name}起点不应晚于终点')
        return a, b

    depart = pair('depart_range')
    arrive = pair('arrive_window')
    stay = _int(query.get('min_stay'), 'min_stay')
    budget = _int(query.get('budget'), 'budget', 1)
    boardings = _int(query.get('max_boardings', 3), 'max_boardings', 1)
    if boardings > 8:
        raise ValueError('max_boardings不应超过8')
    if depart[1] - depart[0] + 1 > _MAX_RANGE_MINUTES:
        raise ValueError('出发范围超出分析上限')
    only = query.get('opportunities')
    if only is not None:
        if not isinstance(only, list) or any(not isinstance(x, str) or not x for x in only):
            raise ValueError('opportunities应为机会id数组')
        only = sorted(set(only))
    return {'depart_range': list(depart), 'arrive_window': list(arrive), 'min_stay': stay,
            'budget': budget, 'max_boardings': boardings, 'opportunities': only}


class Network:
    """一次构建、多查询复用的时刻网络：步行最短路、班次事件、机会表与行程选项缓存。"""

    def __init__(self, snapshot):
        meta = snapshot.metadata
        origin = meta.get('origin')
        if not isinstance(origin, str) or not origin:
            raise ValueError('metadata.origin应为非空字符串')
        offsets = meta.get('day_offsets')
        if not isinstance(offsets, dict) or not offsets:
            raise ValueError('metadata.day_offsets应为非空对象')
        for key, value in offsets.items():
            _int(value, f'day_offsets.{key}')
        self.snapshot = snapshot
        self.origin = origin
        self.min_transfer = _int(meta.get('min_transfer', 0), 'min_transfer')
        self.data_version = meta.get('data_version')
        self.digest = snapshot.digest
        self.walk_graph = {}
        for row in snapshot.rows('walks'):
            u, v = row['from'], row['to']
            if not isinstance(u, str) or not u or not isinstance(v, str) or not v:
                raise ValueError('步行边端点应为非空字符串')
            minutes = _int(row['minutes'], '步行耗时')
            self.walk_graph.setdefault(u, []).append((v, minutes, row['id']))
        self.trips = {}
        self.stop_events = []  # (绝对出发时刻, 班次id, 经停序号)，按出发时刻排序
        for row in snapshot.rows('trips'):
            day = row['service_day']
            if day not in offsets:
                raise ValueError(f'班次{row["id"]}的服务日{day}不在day_offsets中')
            base = offsets[day]
            stops = row['stops']
            if not isinstance(stops, list) or not stops:
                raise ValueError('班次stops应为非空数组')
            events = []
            prev_depart = None
            for st in stops:
                node = st.get('node')
                if not isinstance(node, str) or not node:
                    raise ValueError('班次经停点应为非空字符串')
                arr = _int(st.get('arrive'), '到达时刻')
                dep = _int(st.get('depart'), '出发时刻')
                if arr > dep:
                    raise ValueError('同一经停到达不应晚于出发')
                if prev_depart is not None and arr < prev_depart:
                    raise ValueError('班次时刻应沿经停顺序不减')
                prev_depart = dep
                events.append({'node': node, 'arrive': base + arr, 'depart': base + dep})
            self.trips[row['id']] = {'id': row['id'], 'service_day': day, 'events': events}
            for seq, ev in enumerate(events):
                self.stop_events.append((ev['depart'], row['id'], seq))
        self.stop_events.sort()
        self.opps = {}
        for row in snapshot.rows('opportunities'):
            node = row['node']
            if not isinstance(node, str) or not node:
                raise ValueError('机会节点应为非空字符串')
            open_ = _int(row['open'], 'open')
            close = _int(row['close'], 'close')
            if open_ > close:
                raise ValueError('机会open不应晚于close')
            weight = row['weight']
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise ValueError('机会weight应为数字')
            self.opps[row['id']] = {'id': row['id'], 'node': node,
                                    'open': open_, 'close': close, 'weight': weight}
        self._dist = {}
        self._prev = {}
        self._out_cache = {}   # (机会id, max_boardings) -> {到达时刻: 去程选项}
        self._ret_cache = {}   # (机会id, max_boardings) -> {回城时刻: 回程选项}
        self._walk_resources = {}  # 机会id -> 纯步行往返使用的步行边id集合

    # ---------- 步行最短路 ----------

    def _dijkstra(self, source):
        dist = {source: 0}
        prev = {}
        queue = [(0, source)]
        while queue:
            d, u = heapq.heappop(queue)
            if d != dist[u]:
                continue
            for v, minutes, edge_id in self.walk_graph.get(u, []):
                candidate = d + minutes
                if candidate < dist.get(v, float('inf')):
                    dist[v] = candidate
                    prev[v] = (u, edge_id)
                    heapq.heappush(queue, (candidate, v))
        return dist, prev

    def distances(self, source):
        if source not in self._dist:
            dist, prev = self._dijkstra(source)
            self._dist[source] = dist
            self._prev[source] = prev
        return self._dist[source]

    def walk_leg(self, u, v):
        """构造u到v的最短步行腿；同节点或不可达返回None（调用方先用distances判断）。"""
        if u == v:
            return None
        self.distances(u)
        prev = self._prev[u]
        if v not in prev:
            return None
        path = [v]
        edges = []
        node = v
        while node != u:
            parent, edge_id = prev[node]
            path.append(parent)
            edges.append(edge_id)
            node = parent
        path.reverse()
        edges.reverse()
        return {'kind': 'walk', 'from': u, 'to': v, 'minutes': self._dist[u][v],
                'path': path, 'edge_ids': edges}

    # ---------- 行程枚举 ----------

    def journey_options(self, start, goal, max_boardings):
        """枚举start到goal的全部帕累托行程选项。

        选项 = {'arrive': 到达goal的绝对时刻, 'latest_start': 最晚从start出发的时刻,
                'legs': 步行/班次/换乘等待腿序列, 'boardings': 上车次数,
                'resources': (班次id集合, 步行边id集合)}。
        同一到达时刻只保留最晚出发的选项（对其余选项全面占优），不同到达时刻全部保留：
        到达窗口是查询参数，提前到达的选项在某些窗口下不可替代。
        """
        dist_start = self.distances(start)
        options = {}
        labels = {}  # (节点, 已上车次数) -> [(时刻, 最晚出发)] 帕累托标签
        stack = []
        # 首次上车：从start步行到任一经停点登车，无换乘下限
        for _, trip_id, seq in self.stop_events:
            trip = self.trips[trip_id]
            ev = trip['events'][seq]
            walk = dist_start.get(ev['node'])
            if walk is None:
                continue
            latest = ev['depart'] - walk
            first_legs = []
            if ev['node'] != start:
                first_legs.append(self.walk_leg(start, ev['node']))
            for later in trip['events'][seq + 1:]:
                leg = {'kind': 'trip', 'trip': trip_id, 'service_day': trip['service_day'],
                       'board': ev['node'], 'alight': later['node'],
                       'depart': ev['depart'], 'arrive': later['arrive']}
                stack.append((later['node'], later['arrive'], 1, latest,
                              tuple(first_legs) + (leg,)))
        guard = 0
        while stack:
            node, time, boardings, latest, legs = stack.pop()
            guard += 1
            if guard > _MAX_STATES:
                raise ValueError('行程组合超出分析上限，请降低max_boardings或缩小资料规模')
            key = (node, boardings)
            kept = labels.setdefault(key, [])
            if any(t2 <= time and l2 >= latest for t2, l2 in kept):
                continue
            kept[:] = [(t2, l2) for t2, l2 in kept if not (time <= t2 and latest >= l2)]
            kept.append((time, latest))
            # 到达目标（含同节点零步行）
            walk_goal = self.distances(node).get(goal)
            if walk_goal is not None:
                arrive = time + walk_goal
                final_legs = legs
                if walk_goal:
                    final_legs = legs + (self.walk_leg(node, goal),)
                current = options.get(arrive)
                if current is None or current['latest_start'] < latest:
                    options[arrive] = {'arrive': arrive, 'latest_start': latest,
                                       'legs': final_legs, 'boardings': boardings,
                                       'resources': _resources(final_legs)}
            # 换乘后续班次：非首次上车，步行不重置已上车状态，均需min_transfer
            if boardings < max_boardings:
                dist_node = self.distances(node)
                for _, trip_id, seq in self.stop_events:
                    trip = self.trips[trip_id]
                    ev = trip['events'][seq]
                    walk = dist_node.get(ev['node'])
                    if walk is None:
                        continue
                    if ev['depart'] < time + walk + self.min_transfer:
                        continue
                    transfer_legs = list(legs)
                    if ev['node'] != node or walk:
                        transfer_legs.append(self.walk_leg(node, ev['node']))
                    wait = ev['depart'] - (time + walk)
                    if wait:
                        transfer_legs.append({'kind': 'wait', 'at': ev['node'],
                                              'minutes': wait, 'reason': 'transfer'})
                    for later in trip['events'][seq + 1:]:
                        leg = {'kind': 'trip', 'trip': trip_id, 'service_day': trip['service_day'],
                               'board': ev['node'], 'alight': later['node'],
                               'depart': ev['depart'], 'arrive': later['arrive']}
                        stack.append((later['node'], later['arrive'], boardings + 1, latest,
                                      tuple(transfer_legs) + (leg,)))
        return options

    def prepare(self, opp_ids=None, max_boardings=3):
        """为指定（或全部）机会构建去/回程选项缓存，供批量查询复用。"""
        ids = opp_ids if opp_ids is not None else sorted(self.opps)
        for oid in ids:
            node = self.opps[oid]['node']
            key = (oid, max_boardings)
            if key not in self._out_cache:
                self._out_cache[key] = self.journey_options(self.origin, node, max_boardings)
                self._ret_cache[key] = self.journey_options(node, self.origin, max_boardings)
                edges = set()
                for leg in (self.walk_leg(self.origin, node), self.walk_leg(node, self.origin)):
                    if leg:
                        edges.update(leg['edge_ids'])
                self._walk_resources[oid] = edges

    def out_options(self, oid, max_boardings):
        self.prepare([oid], max_boardings)
        return self._out_cache[(oid, max_boardings)]

    def ret_options(self, oid, max_boardings):
        self.prepare([oid], max_boardings)
        return self._ret_cache[(oid, max_boardings)]

    def cache_stats(self):
        return {'outbound_options': len(self._out_cache), 'return_options': len(self._ret_cache)}


def _resources(legs):
    trips = {leg['trip'] for leg in legs if leg['kind'] == 'trip'}
    edges = set()
    for leg in legs:
        if leg['kind'] == 'walk':
            edges.update(leg['edge_ids'])
    return trips, edges


def build_network(snapshot):
    return Network(snapshot)


# ---------- 查询求值 ----------

def _context(net, opp, q):
    """与查询相关的配对上下文：窗口内去程（含ready与最优回程）、全部回程、纯步行参数。"""
    arrive_lo, arrive_hi = q['arrive_window']
    stay = q['min_stay']
    oid = opp['id']
    boardings = q['max_boardings']
    outs_all = net.out_options(oid, boardings)
    rets = sorted(net.ret_options(oid, boardings).values(),
                  key=lambda r: (r['arrive'], r['latest_start']))
    walk_out = net.distances(net.origin).get(opp['node'])
    walk_back = net.distances(opp['node']).get(net.origin)
    open_, close = opp['open'], opp['close']
    visit_ok = open_ + stay <= close
    arrive_cap = min(arrive_hi, close - stay)
    outs = []
    if visit_ok:
        candidates = sorted(outs_all.values(), key=lambda o: (o['arrive'], o['latest_start']))
        for opt in candidates:
            arrive = opt['arrive']
            if not (arrive_lo <= arrive <= arrive_cap):
                continue
            ready = max(arrive, open_) + stay
            best_h = None
            best_ref = None
            for j, ret in enumerate(rets):
                if ret['latest_start'] >= ready and (best_h is None or ret['arrive'] < best_h):
                    best_h = ret['arrive']
                    best_ref = j
            if walk_back is not None:
                home_walk = ready + walk_back
                if best_h is None or home_walk < best_h:
                    best_h = home_walk
                    best_ref = -2  # 回程纯步行
            outs.append({'opt': opt, 'arrive': arrive, 'L': opt['latest_start'],
                         'ready': ready, 'H': best_h, 'ref': best_ref})
    walk_window = None
    if visit_ok and walk_out is not None:
        lo, hi = arrive_lo - walk_out, arrive_cap - walk_out
        if lo <= hi:
            walk_window = (lo, hi)
    return {'outs_all': outs_all, 'rets': rets, 'outs': outs, 'walk_window': walk_window,
            'walk_out': walk_out, 'walk_back': walk_back, 'open': open_, 'close': close}


def _feasible_at(ctx, q, t):
    """出发时刻t是否可行（精确点查询）。"""
    budget = q['budget']
    stay = q['min_stay']
    for out in ctx['outs']:
        if out['L'] >= t and out['H'] is not None and out['H'] <= t + budget:
            return True
    window = ctx['walk_window']
    if window is not None and window[0] <= t <= window[1]:
        ready = max(t + ctx['walk_out'], ctx['open']) + stay
        for ret in ctx['rets']:
            if ret['latest_start'] >= ready and ret['arrive'] <= t + budget:
                return True
        if ctx['walk_back'] is not None and ready + ctx['walk_back'] <= t + budget:
            return True
    return False


def _eval_opp(net, opp, q):
    depart_lo, depart_hi = q['depart_range']
    total = depart_hi - depart_lo + 1
    budget = q['budget']
    stay = q['min_stay']
    open_ = opp['open']
    ctx = _context(net, opp, q)
    outs, rets = ctx['outs'], ctx['rets']
    walk_window = ctx['walk_window']
    walk_out, walk_back = ctx['walk_out'], ctx['walk_back']

    feasible = bytearray(total)
    choice_out = [-1] * total   # 去程：outs下标，-2为纯步行
    choice_ret = [-1] * total   # 回程：rets下标，-2为纯步行

    def mark(lo, hi, co, cr):
        lo = max(lo, depart_lo)
        hi = min(hi, depart_hi)
        for t in range(lo, hi + 1):
            i = t - depart_lo
            if not feasible[i]:
                feasible[i] = 1
                choice_out[i] = co
                choice_ret[i] = cr

    # 班次去程 × 最优回程：可行区间为 [H - budget, L]
    for i, out in enumerate(outs):
        if out['H'] is not None:
            mark(out['H'] - budget, out['L'], i, out['ref'])
    # 纯步行去程：到达时刻 = t + walk_out，ready随t变化，分等待开门与否两段
    if walk_window is not None:
        w_lo, w_hi = walk_window
        for j, ret in enumerate(rets):
            latest_ready, home = ret['latest_start'], ret['arrive']
            # 到达时已开门：ready = t + walk_out + stay
            mark(max(w_lo, open_ - walk_out, home - budget),
                 min(w_hi, latest_ready - walk_out - stay), -2, j)
            # 到达后等待开门：ready = open + stay（与t无关）
            if open_ + stay <= latest_ready:
                mark(max(w_lo, home - budget), min(w_hi, open_ - walk_out - 1), -2, j)
        if walk_back is not None:
            # 纯步行往返：h = ready + walk_back
            if walk_out + stay + walk_back <= budget:
                mark(max(w_lo, open_ - walk_out), w_hi, -2, -2)
            mark(max(w_lo, open_ + stay + walk_back - budget),
                 min(w_hi, open_ - walk_out - 1), -2, -2)

    # 分钟级损失归因
    lost = {}
    for t in range(depart_lo, depart_hi + 1):
        if feasible[t - depart_lo]:
            continue
        if not outs and walk_window is None:
            reason = 'unreachable' if not ctx['outs_all'] and walk_out is None else 'closed'
        elif not rets and walk_back is None:
            reason = 'one_way'
        else:
            catch = walk_window is not None and walk_window[0] <= t <= walk_window[1]
            if not catch:
                catch = any(out['L'] >= t for out in outs)
            if not catch:
                reason = 'no_outbound'
            else:
                compat = any(out['L'] >= t and out['ref'] is not None for out in outs)
                if walk_window is not None and walk_window[0] <= t <= walk_window[1]:
                    ready_t = max(t + walk_out, open_) + stay
                    if walk_back is not None or any(
                            ret['latest_start'] >= ready_t for ret in rets):
                        compat = True
                reason = 'over_budget' if compat else 'missed_return'
        lost[reason] = lost.get(reason, 0) + 1

    # 可行区间（闭区间）与见证行程
    runs = []
    i = 0
    while i < total:
        if feasible[i]:
            j = i
            while j + 1 < total and feasible[j + 1]:
                j += 1
            runs.append([i + depart_lo, j + depart_lo])
            i = j + 1
        else:
            i += 1
    witnesses = []
    for lo, hi in runs[:3]:
        k = hi - depart_lo
        witnesses.append(_witness(net, opp, q, ctx, outs, rets, choice_out[k], choice_ret[k], hi, [lo, hi]))

    count = sum(feasible)
    if count:
        status = 'feasible'
    elif not ctx['outs_all'] and walk_out is None:
        status = 'unreachable'
    elif not outs and walk_window is None:
        status = 'closed'
    elif not rets and walk_back is None:
        status = 'one_way'
    else:
        status = 'missed'

    entry = {
        'id': opp['id'], 'node': opp['node'], 'weight': opp['weight'],
        'open': open_, 'close': opp['close'], 'status': status,
        'feasible_minutes': count, 'feasible_fraction': count / total,
        'feasible_intervals': runs,
        'lost_minutes': {key: lost[key] for key in _REASONS if key in lost},
        'options': {
            'outbound': [{'arrive': out['arrive'], 'latest_depart': out['L'],
                          'ready': out['ready'], 'transfers': out['opt']['boardings'] - 1,
                          'trips': _trip_ids(out['opt']['legs'])} for out in outs],
            'return': [{'arrive_home': ret['arrive'], 'latest_ready': ret['latest_start'],
                        'transfers': ret['boardings'] - 1,
                        'trips': _trip_ids(ret['legs'])} for ret in rets],
        },
        'walk_only': {'outbound_minutes': walk_out, 'return_minutes': walk_back},
        'witnesses': witnesses,
    }
    if len(runs) > len(witnesses):
        entry['witness_note'] = '可行区间较多，仅展示前3个区间的见证行程'
    return entry


def _trip_ids(legs):
    return sorted({leg['trip'] for leg in legs if leg['kind'] == 'trip'})


def _witness(net, opp, q, ctx, outs, rets, co, cr, t, interval):
    """为可行区间端点t*组装实际行程：班次、步行、等待、换乘构成。"""
    stay = q['min_stay']
    budget = q['budget']
    open_ = ctx['open']
    if co == -2:  # 去程纯步行
        leg = net.walk_leg(net.origin, opp['node'])
        out_legs = [leg] if leg else []
        arrive = t + ctx['walk_out']
        latest_depart = None
    else:
        out = outs[co]
        out_legs = [dict(leg) for leg in out['opt']['legs']]
        first = next(leg for leg in out_legs if leg['kind'] == 'trip')
        walk_before = out_legs[0]['minutes'] if out_legs[0]['kind'] == 'walk' else 0
        wait = first['depart'] - (t + walk_before)
        if wait:
            out_legs.insert(out_legs.index(first),
                            {'kind': 'wait', 'at': first['board'], 'minutes': wait,
                             'reason': 'first_boarding'})
        arrive = out['arrive']
        latest_depart = out['L']
    ready = max(arrive, open_) + stay
    if cr == -2:  # 回程纯步行
        leg = net.walk_leg(opp['node'], net.origin)
        ret_legs = [leg] if leg else []
        home = ready + ctx['walk_back']
        latest_ready = None
    else:
        ret = rets[cr]
        ret_legs = [dict(leg) for leg in ret['legs']]
        first = next(leg for leg in ret_legs if leg['kind'] == 'trip')
        walk_before = ret_legs[0]['minutes'] if ret_legs[0]['kind'] == 'walk' else 0
        wait = first['depart'] - (ready + walk_before)
        if wait:
            ret_legs.insert(ret_legs.index(first),
                            {'kind': 'wait', 'at': first['board'], 'minutes': wait,
                             'reason': 'first_boarding'})
        home = ret['arrive']
        latest_ready = ret['latest_start']
    legs = out_legs + ret_legs
    walk_minutes = sum(leg['minutes'] for leg in legs if leg['kind'] == 'walk')
    wait_minutes = sum(leg['minutes'] for leg in legs if leg['kind'] == 'wait')
    ride_minutes = sum(leg['arrive'] - leg['depart'] for leg in legs if leg['kind'] == 'trip')
    transfers = (max(0, sum(1 for leg in out_legs if leg['kind'] == 'trip') - 1)
                 + max(0, sum(1 for leg in ret_legs if leg['kind'] == 'trip') - 1))
    return {
        'interval': interval, 'depart_at': t,
        'outbound': {'legs': out_legs, 'latest_depart': latest_depart, 'arrive': arrive},
        'visit': {'arrive': arrive, 'open': open_, 'wait_for_open': max(0, open_ - arrive),
                  'stay': stay, 'ready': ready},
        'return': {'legs': ret_legs, 'latest_ready': latest_ready, 'arrive_home': home},
        'totals': {'walk_minutes': walk_minutes, 'wait_minutes': wait_minutes,
                   'ride_minutes': ride_minutes, 'transfers': transfers,
                   'total_minutes': home - t, 'budget': budget,
                   'budget_slack': budget - (home - t)},
    }


def _measure(intervals):
    merged = 0
    current = None
    for lo, hi in sorted(intervals):
        if current is None or lo > current[1] + 1:
            if current is not None:
                merged += current[1] - current[0] + 1
            current = [lo, hi]
        else:
            current[1] = max(current[1], hi)
    if current is not None:
        merged += current[1] - current[0] + 1
    return merged


def _summary(entries, total):
    counts = {}
    weights = {}
    lost = {}
    feasible_weight = 0
    total_weight = 0
    feasible_n = 0
    union = []
    for entry in entries.values():
        status = entry['status']
        counts[status] = counts.get(status, 0) + 1
        weights[status] = weights.get(status, 0) + entry['weight']
        total_weight += entry['weight']
        if status == 'feasible':
            feasible_n += 1
            feasible_weight += entry['weight']
        for key, value in entry['lost_minutes'].items():
            lost[key] = lost.get(key, 0) + value
        union.extend(entry['feasible_intervals'])
    any_minutes = _measure(union)
    return {
        'range_minutes': total,
        'opportunities_total': len(entries),
        'opportunities_feasible': feasible_n,
        'weight_total': total_weight,
        'weight_feasible': feasible_weight,
        'status_counts': counts,
        'weight_by_status': weights,
        'lost_minutes_by_reason': {key: lost[key] for key in _REASONS if key in lost},
        'any_feasible_minutes': any_minutes,
        'any_feasible_fraction': any_minutes / total,
    }


def evaluate(net, query, only=None):
    """精确求值：逐分钟配对并集。only给定机会id集合时只评估这些机会（增量重估用）。"""
    q = check_query(query)
    depart_lo, depart_hi = q['depart_range']
    total = depart_hi - depart_lo + 1
    ids = q['opportunities'] if q['opportunities'] is not None else sorted(net.opps)
    unknown = [oid for oid in ids if oid not in net.opps]
    if unknown:
        raise ValueError(f'未知机会id: {unknown}')
    if only is not None:
        keep = set(only)
        ids = [oid for oid in ids if oid in keep]
    entries = {}
    for oid in ids:
        entries[oid] = _eval_opp(net, net.opps[oid], q)
    return {
        'method': 'exact_minute_union',
        'input_digest': net.digest,
        'data_version': net.data_version,
        'query': q,
        'query_digest': hashlib.sha256(canonical(q).encode('utf-8')).hexdigest(),
        'opportunities': entries,
        'summary': _summary(entries, total),
    }


def evaluate_sample(net, query, step):
    """抽样近似：只在抽样点上精确求值，未解析区间给出误差界。

    离散取点不能当成整个出发范围的精确覆盖率：相邻抽样点之间若可能含有
    可行性跳变点（由班次、步行与预算导出的候选断点超集判定），该区间计入
    不确定范围，输出可行比例的[下界, 上界]。精确覆盖率请使用evaluate。
    """
    q = check_query(query)
    step = _int(step, 'sample步长', 1)
    depart_lo, depart_hi = q['depart_range']
    total = depart_hi - depart_lo + 1
    budget = q['budget']
    stay = q['min_stay']
    samples = sorted(set(range(depart_lo, depart_hi + 1, step)) | {depart_hi})
    ids = q['opportunities'] if q['opportunities'] is not None else sorted(net.opps)
    unknown = [oid for oid in ids if oid not in net.opps]
    if unknown:
        raise ValueError(f'未知机会id: {unknown}')
    entries = {}
    for oid in ids:
        opp = net.opps[oid]
        ctx = _context(net, opp, q)
        # 候选断点超集：配对区间端点（及其+1跳变点）均出自这些值
        breaks = {depart_lo, depart_hi + 1}
        for out in ctx['outs']:
            breaks.add(out['L'])
            breaks.add(out['L'] + 1)
            if out['H'] is not None:
                breaks.add(out['H'] - budget)
        for ret in ctx['rets']:
            breaks.add(ret['arrive'] - budget)
            if ctx['walk_out'] is not None:
                edge = ret['latest_start'] - ctx['walk_out'] - stay
                breaks.add(edge)
                breaks.add(edge + 1)
        if ctx['walk_window'] is not None:
            breaks.add(ctx['walk_window'][0])
            breaks.add(ctx['walk_window'][1] + 1)
            breaks.add(ctx['open'] - ctx['walk_out'])
            if ctx['walk_back'] is not None:
                breaks.add(ctx['open'] + stay + ctx['walk_back'] - budget)
        values = [(t, _feasible_at(ctx, q, t)) for t in samples]
        lower = sum(1 for _, ok in values if ok)
        upper = lower
        unresolved = 0
        for (t0, ok), (t1, _) in zip(values, values[1:]):
            gap = t1 - t0 - 1
            if gap <= 0:
                continue
            if any(t0 < b < t1 for b in breaks):
                unresolved += gap
                upper += gap
            elif ok:
                lower += gap
                upper += gap
        entries[oid] = {
            'id': oid, 'node': opp['node'], 'weight': opp['weight'],
            'samples': [{'t': t, 'feasible': ok} for t, ok in values],
            'sample_count': len(values),
            'feasible_samples': sum(1 for _, ok in values if ok),
            'estimate_fraction': sum(1 for _, ok in values if ok) / len(values),
            'fraction_bounds': [lower / total, upper / total],
            'unresolved_minutes': unresolved,
        }
    return {
        'method': 'sampled_approximation',
        'note': '抽样仅为带误差界的近似，不能当成整个出发范围的精确覆盖率；精确结果请使用默认模式',
        'input_digest': net.digest,
        'data_version': net.data_version,
        'query': q,
        'query_digest': hashlib.sha256(canonical(q).encode('utf-8')).hexdigest(),
        'sample_step': step,
        'opportunities': entries,
        'summary': {'range_minutes': total, 'opportunities_total': len(entries)},
    }


# ---------- 资料变更与增量更新 ----------

def diff_snapshots(old, new):
    """比较两个快照：各集合的移除/新增/内容变化，以及关键metadata变化。"""
    def keyed(snap, collection):
        return {row['id']: canonical(row) for row in snap.rows(collection)}

    diff = {}
    for collection in ('walks', 'trips', 'opportunities'):
        before, after = keyed(old, collection), keyed(new, collection)
        diff[collection] = {
            'removed': sorted(set(before) - set(after)),
            'added': sorted(set(after) - set(before)),
            'changed': sorted(key for key in set(before) & set(after)
                              if before[key] != after[key]),
        }
    old_meta, new_meta = old.metadata, new.metadata
    diff['metadata_changed'] = sorted(
        key for key in ('origin', 'min_transfer', 'day_offsets')
        if canonical(old_meta.get(key)) != canonical(new_meta.get(key)))
    diff['data_version_changed'] = old_meta.get('data_version') != new_meta.get('data_version')
    return diff


def plan_update(old_net, diff, old_result=None, max_boardings=3):
    """圈定资料变更后需要重新评估的机会范围。

    纯移除（停运、封站、撤回）：缓存行程实际引用被移除资源的机会才受影响——
    其余机会的可行并集由未受影响的帕累托选项生成，结果必然不变。
    新增或恢复：可行性只增不减，已完全可行的机会不受影响，其余机会保守重估。
    metadata关键字段变化：全量重估。
    """
    if diff['metadata_changed']:
        return {'full_recompute': True,
                'reasons': [f'metadata变化: {key}' for key in diff['metadata_changed']],
                'opportunities': {}, 'removed_opportunities': [],
                'data_version_changed': diff['data_version_changed']}
    old_net.prepare(max_boardings=max_boardings)
    gone_trips = set(diff['trips']['removed']) | set(diff['trips']['changed'])
    gone_walks = set(diff['walks']['removed']) | set(diff['walks']['changed'])
    additions = bool(diff['trips']['added'] or diff['walks']['added'])
    range_minutes = None
    if old_result is not None:
        range_minutes = old_result['summary']['range_minutes']
    removed_opps = set(diff['opportunities']['removed'])
    scope = {}
    for oid in sorted(old_net.opps):
        if oid in removed_opps:
            continue  # 已撤回的机会不再重估，由removed_opportunities报告
        reasons = []
        used_trips, used_walks = set(), set(old_net._walk_resources.get(oid, ()))
        for cache in (old_net._out_cache, old_net._ret_cache):
            for opt in cache.get((oid, max_boardings), {}).values():
                trips, edges = opt['resources']
                used_trips |= trips
                used_walks |= edges
        if used_trips & gone_trips:
            reasons.append('行程引用了被取消或修改的班次')
        if used_walks & gone_walks:
            reasons.append('行程引用了被撤回或修改的步行边')
        if oid in diff['opportunities']['changed']:
            reasons.append('机会记录本身发生变化')
        if additions:
            entry = (old_result or {}).get('opportunities', {}).get(oid)
            if entry is None or range_minutes is None or entry['feasible_minutes'] < range_minutes:
                reasons.append('新增资源可能改善未完全可行的机会')
        if reasons:
            scope[oid] = reasons
    for oid in diff['opportunities']['added']:
        scope[oid] = ['新增机会']
    return {'full_recompute': False,
            'reasons': [],
            'opportunities': scope,
            'removed_opportunities': diff['opportunities']['removed'],
            'data_version_changed': diff['data_version_changed']}


def merge_results(old_result, partial, removed=()):
    """把增量重估结果并入旧结果，返回新对象；旧结果（绑定旧digest）不被修改。"""
    if old_result['query_digest'] != partial['query_digest']:
        raise ValueError('查询不一致，不能合并')
    merged = copy.deepcopy(old_result)
    merged['input_digest'] = partial['input_digest']
    merged['data_version'] = partial['data_version']
    for oid in removed:
        merged['opportunities'].pop(oid, None)
    merged['opportunities'].update(partial['opportunities'])
    merged['summary'] = _summary(merged['opportunities'],
                                 old_result['summary']['range_minutes'])
    return merged
