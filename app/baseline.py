"""现有业务摘要，不推断未观察到的业务关系。"""
import heapq

def analyze(snapshot):
    graph = {}
    for edge in snapshot.rows('walks'):
        if edge['minutes'] < 0:
            raise ValueError('步行耗时为负')
        graph.setdefault(edge['from'], []).append((edge['to'], edge['minutes']))
    origin = snapshot.metadata['origin']
    distances, queue = {origin: 0}, [(0, origin)]
    while queue:
        d, node = heapq.heappop(queue)
        if d != distances[node]:
            continue
        for target, cost in graph.get(node, []):
            candidate = d + cost
            if candidate < distances.get(target, float('inf')):
                distances[target] = candidate
                heapq.heappush(queue, (candidate, target))
    return dict(sorted(distances.items()))
