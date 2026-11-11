"""最大基数、最小总距离的带重复样品编号核对算法。

每行先被看作一个独立节点，因此重复的样品编号不会把不同采样点合并。
候选边由忽略大小写的 Levenshtein 距离（<= 2）决定。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Sequence


MAX_DISTANCE = 2
_INF = 10**9


@dataclass(frozen=True)
class SubmissionRow:
    row_id: str
    sample_no: str
    sampling_point: str


@dataclass(frozen=True)
class InstrumentRow:
    row_id: str
    sample_no: str
    value: float


@dataclass(frozen=True)
class Candidate:
    submission: SubmissionRow
    distance: int


@dataclass(frozen=True)
class InstrumentResolution:
    instrument: InstrumentRow
    candidates: tuple[Candidate, ...]
    possible_unmatched: bool

    @property
    def is_confirmed(self) -> bool:
        """只有唯一候选且不可能未匹配时，才可确定归档。"""

        return len(self.candidates) == 1 and not self.possible_unmatched


@dataclass(frozen=True)
class Analysis:
    max_pairs: int
    min_total_distance: int
    resolutions: tuple[InstrumentResolution, ...]


def normalize_sample_no(value: str) -> str:
    """按要求忽略大小写；输入校验保证只含英文字母和数字。"""

    return value.upper()


def levenshtein(left: str, right: str) -> int:
    """计算两个字符串的 Levenshtein 编辑距离。"""

    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    # 让内层循环使用较短的一侧，减少临时数组长度。
    if len(left) < len(right):
        left, right = right, left

    previous = list(range(len(right) + 1))
    for i, left_ch in enumerate(left, start=1):
        current = [i]
        for j, right_ch in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (left_ch != right_ch),
                )
            )
        previous = current
    return previous[-1]


def candidate_distance(
    submission_sample_no: str,
    instrument_sample_no: str,
) -> int | None:
    """返回可配对边的距离；超过上限时返回 None。"""

    left = normalize_sample_no(submission_sample_no)
    right = normalize_sample_no(instrument_sample_no)
    if abs(len(left) - len(right)) > MAX_DISTANCE:
        return None
    distance = levenshtein(left, right)
    return distance if distance <= MAX_DISTANCE else None


def _min_cost_matching(
    submissions: Sequence[SubmissionRow],
    instruments: Sequence[InstrumentRow],
    active_submissions: Sequence[int],
    active_instruments: Sequence[int],
) -> tuple[int, int]:
    """在给定节点子集上求最大基数、其次最小总距离的匹配。

    使用逐次最短增广路的最小费用最大流。每条边容量均为 1，因此
    每轮增广恰好增加一个配对；流达到最大时，代价即该基数下的最小值。
    """

    sub_count = len(active_submissions)
    inst_count = len(active_instruments)
    source = 0
    first_sub = 1
    first_inst = first_sub + sub_count
    sink = first_inst + inst_count
    node_count = sink + 1

    # 弧格式：[终点, 反向弧下标, 剩余容量, 单位费用]
    graph: list[list[list[int]]] = [[] for _ in range(node_count)]

    def add_arc(start: int, end: int, capacity: int, cost: int) -> None:
        graph[start].append([end, len(graph[end]), capacity, cost])
        graph[end].append([start, len(graph[start]) - 1, 0, -cost])

    for local_sub, original_sub in enumerate(active_submissions):
        add_arc(source, first_sub + local_sub, 1, 0)

    for local_inst, original_inst in enumerate(active_instruments):
        add_arc(first_inst + local_inst, sink, 1, 0)

    for local_sub, original_sub in enumerate(active_submissions):
        submission = submissions[original_sub]
        sub_node = first_sub + local_sub
        for local_inst, original_inst in enumerate(active_instruments):
            instrument = instruments[original_inst]
            distance = candidate_distance(
                submission.sample_no,
                instrument.sample_no,
            )
            if distance is not None:
                add_arc(
                    sub_node,
                    first_inst + local_inst,
                    1,
                    distance,
                )

    flow = 0
    total_cost = 0

    while True:
        # 残量网络里可能有负费用反向弧，故用 SPFA 求最短路。
        distances = [_INF] * node_count
        previous_node = [-1] * node_count
        previous_arc = [-1] * node_count
        in_queue = [False] * node_count

        distances[source] = 0
        queue = deque([source])
        in_queue[source] = True

        while queue:
            node = queue.popleft()
            in_queue[node] = False
            for arc_index, arc in enumerate(graph[node]):
                end, _, capacity, cost = arc
                if capacity <= 0:
                    continue
                new_distance = distances[node] + cost
                if new_distance < distances[end]:
                    distances[end] = new_distance
                    previous_node[end] = node
                    previous_arc[end] = arc_index
                    if not in_queue[end]:
                        in_queue[end] = True
                        queue.append(end)

        if distances[sink] == _INF:
            break

        # 源、汇以及中间弧的容量都是 1，所以每次增广 1 个单位。
        node = sink
        while node != source:
            prior = previous_node[node]
            arc_index = previous_arc[node]
            arc = graph[prior][arc_index]
            arc[2] -= 1
            graph[node][arc[1]][2] += 1
            total_cost += arc[3]
            node = prior

        flow += 1

    return flow, total_cost


def _possible_edge_analysis(
    submissions: Sequence[SubmissionRow],
    instruments: Sequence[InstrumentRow],
    max_pairs: int,
    min_total_distance: int,
) -> tuple[dict[int, list[Candidate]], dict[int, bool]]:
    """判断每条候选边能否出现在某个最优方案中，以及各记录能否未匹配。"""

    all_submissions = tuple(range(len(submissions)))
    all_instruments = tuple(range(len(instruments)))
    candidates: dict[int, list[Candidate]] = {index: [] for index in all_instruments}
    possible_unmatched: dict[int, bool] = {index: False for index in all_instruments}

    for inst_index, instrument in enumerate(instruments):
        remaining_instruments = tuple(
            index for index in all_instruments if index != inst_index
        )

        # 若去掉该记录后仍能达到同样的最优解，就存在“它未匹配”的最优方案。
        rest_pairs, rest_distance = _min_cost_matching(
            submissions,
            instruments,
            all_submissions,
            remaining_instruments,
        )
        if rest_pairs == max_pairs and rest_distance == min_total_distance:
            possible_unmatched[inst_index] = True

        for sub_index, submission in enumerate(submissions):
            distance = candidate_distance(
                submission.sample_no,
                instrument.sample_no,
            )
            if distance is None:
                continue

            # 强制使用这条边，再看剩余图能否补成同一个全局最优解。
            remaining_submissions = tuple(
                index for index in all_submissions if index != sub_index
            )
            rest_pairs, rest_distance = _min_cost_matching(
                submissions,
                instruments,
                remaining_submissions,
                remaining_instruments,
            )
            if (
                rest_pairs + 1 == max_pairs
                and rest_distance + distance == min_total_distance
            ):
                candidates[inst_index].append(
                    Candidate(submission=submission, distance=distance)
                )

    return candidates, possible_unmatched


def analyze_matches(
    submissions: Sequence[SubmissionRow],
    instruments: Sequence[InstrumentRow],
) -> Analysis:
    """先最大化配对数，再最小化总距离，并给出所有最优方案中的稳定性。"""

    all_submissions = tuple(range(len(submissions)))
    all_instruments = tuple(range(len(instruments)))
    max_pairs, min_total_distance = _min_cost_matching(
        submissions,
        instruments,
        all_submissions,
        all_instruments,
    )

    candidates, possible_unmatched = _possible_edge_analysis(
        submissions,
        instruments,
        max_pairs,
        min_total_distance,
    )

    resolutions = tuple(
        InstrumentResolution(
            instrument=instrument,
            candidates=tuple(candidates[index]),
            possible_unmatched=possible_unmatched[index],
        )
        for index, instrument in enumerate(instruments)
    )

    return Analysis(
        max_pairs=max_pairs,
        min_total_distance=min_total_distance,
        resolutions=resolutions,
    )
