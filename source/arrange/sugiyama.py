# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from itertools import chain, pairwise
from statistics import fmean

import bpy
import networkx as nx
from bpy.types import NodeTree
from mathutils import Vector

from .. import config
from ..utils import abs_loc, group_by, move_to
from .graph import Cluster, GNode, GNodeType, Socket
from .ordering import minimize_crossings
from .placement.bk import bk_assign_y_coords
from .placement.linear_segments import Segment, linear_segments_assign_y_coords
from .ranking import compute_ranks

# -------------------------------------------------------------------


def precompute_links(ntree: NodeTree) -> None:

    # Precompute links to ignore invalid/hidden links, and avoid `O(len(ntree.links))` time

    for link in ntree.links:
        if not link.is_hidden and link.is_valid:
            config.linked_sockets[link.to_socket].add(link.from_socket)
            config.linked_sockets[link.from_socket].add(link.to_socket)


def get_multidigraph() -> nx.MultiDiGraph:
    parents = {n.parent: Cluster(n.parent) for n in config.selected}
    for c in parents.values():
        if c.node:
            c.cluster = parents[c.node.parent]

    G = nx.MultiDiGraph()
    G.add_nodes_from([
      GNode(n, parents[n.parent]) for n in config.selected if n.bl_idname != 'NodeFrame'])
    for u in G:
        for i, from_output in enumerate(u.node.outputs):
            for to_input in config.linked_sockets[from_output]:
                if not to_input.node.select:
                    continue

                v = next(v for v in G if v.node == to_input.node)
                j = to_input.node.inputs[:].index(to_input)
                G.add_edge(u, v, from_socket=Socket(u, i, True), to_socket=Socket(v, j, False))

    return G


def get_nesting_relations(v: GNode | Cluster) -> Iterator[tuple[Cluster, GNode | Cluster]]:
    if c := v.cluster:
        yield (c, v)
        yield from get_nesting_relations(c)


# -------------------------------------------------------------------


def add_dummy_edge(G: nx.DiGraph, u: GNode, v: GNode) -> None:
    G.add_edge(u, v, from_socket=Socket(u, 0, True), to_socket=Socket(v, 0, False))


def add_dummy_nodes_to_edge(
  G: nx.MultiDiGraph,
  edge: tuple[GNode, GNode, int],
  dummy_nodes: Sequence[GNode],
) -> None:
    if not dummy_nodes:
        return

    for pair in pairwise(dummy_nodes):
        add_dummy_edge(G, *pair)

    u, v, _ = edge
    d = G.edges[edge]

    w = dummy_nodes[0]
    G.add_edge(u, w, from_socket=d['from_socket'], to_socket=Socket(w, 0, False))

    z = dummy_nodes[-1]
    G.add_edge(z, v, from_socket=Socket(z, 0, True), to_socket=d['to_socket'])

    G.remove_edge(*edge)

    if not u.is_real or not v.is_real:
        return

    links = bpy.context.space_data.edit_tree.links
    if d['to_socket'].bpy.is_multi_input:
        target_link = (d['from_socket'].bpy, d['to_socket'].bpy)
        links.remove(next(l for l in links if (l.from_socket, l.to_socket) == target_link))


# -------------------------------------------------------------------


# https://api.semanticscholar.org/CorpusID:14932050
class ClusterGraph:
    G: nx.MultiDiGraph
    T: nx.DiGraph
    S: set[Cluster]
    __slots__ = tuple(__annotations__)

    def __init__(self, G: nx.MultiDiGraph) -> None:
        self.G = G
        self.T = nx.DiGraph(chain(*map(get_nesting_relations, G)))
        self.S = {v for v in self.T if v.type == GNodeType.CLUSTER}

    def remove_nodes_from(self, nodes: Iterable[GNode]) -> None:
        ntree = bpy.context.space_data.edit_tree

        for v in nodes:
            self.G.remove_node(v)
            self.T.remove_node(v)
            if v.col:
                v.col.remove(v)

            if not v.is_real:
                continue

            sockets = {*v.node.inputs, *v.node.outputs}

            for socket in sockets:
                config.linked_sockets.pop(socket, None)

            for val in config.linked_sockets.values():
                val -= sockets

            config.selected.remove(v.node)
            ntree.nodes.remove(v.node)

    def insert_dummy_nodes(self) -> None:
        G = self.G
        T = self.T

        # -------------------------------------------------------------------

        long_edges = [(u, v, k) for u, v, k in G.edges(keys=True) if v.rank - u.rank > 1]
        pairs = {(u, v) for u, v, _ in long_edges if u.cluster != v.cluster}
        lca = dict(nx.tree_all_pairs_lowest_common_ancestor(T, pairs=pairs))

        for u, v, k in long_edges:
            c = lca[(u, v)] if (u, v) in pairs else u.cluster
            dummy_nodes = []
            for i in range(u.rank + 1, v.rank):
                w = GNode(None, c, GNodeType.DUMMY, i)
                T.add_edge(c, w)
                dummy_nodes.append(w)

            add_dummy_nodes_to_edge(G, (u, v, k), dummy_nodes)

        # -------------------------------------------------------------------

        for c in self.S:
            if not c.node:
                continue

            ranks = sorted({v.rank for v in nx.descendants(T, c) if v in G})
            for i, j in pairwise(ranks):
                if j - i == 1:
                    continue

                u = None
                for k in range(i + 1, j):
                    v = GNode(None, c, GNodeType.VERTICAL_BORDER, k)
                    T.add_edge(c, v)

                    if u:
                        add_dummy_edge(G, u, v)
                    else:
                        G.add_node(v)

                    u = v

    def add_vertical_border_nodes(self) -> None:
        T = self.T
        G = self.G
        columns = G.graph['columns']
        for c in self.S:
            if not c.node:
                continue

            nodes = [v for v in nx.descendants(T, c) if v in G]
            lower_border_nodes = []
            upper_border_nodes = []
            for subcol in group_by(nodes, key=lambda v: columns.index(v.col), sort=True):
                col = subcol[0].col
                indices = [col.index(v) for v in subcol]

                lower_v = GNode(None, c, GNodeType.VERTICAL_BORDER)
                col.insert(max(indices) + 1, lower_v)
                lower_v.col = col
                T.add_edge(c, lower_v)
                lower_border_nodes.append(lower_v)

                upper_v = GNode(None, c, GNodeType.VERTICAL_BORDER)
                col.insert(min(indices), upper_v)
                upper_v.col = col
                T.add_edge(c, upper_v)
                upper_border_nodes.append(upper_v)

            G.add_nodes_from(lower_border_nodes + upper_border_nodes)
            for p in *pairwise(lower_border_nodes), *pairwise(upper_border_nodes):
                add_dummy_edge(G, *p)


# -------------------------------------------------------------------


def get_reroute_paths(G: nx.DiGraph, function: Callable | None = None) -> list[list[GNode]]:
    reroutes = [v for v in G if v.is_reroute and (not function or function(v))]
    SG = nx.DiGraph(G.subgraph(reroutes))
    for v in SG:
        if G.out_degree(v) > 1:
            SG.remove_edges_from(tuple(SG.out_edges(v)))

    indicies = {v: i for i, v in enumerate(nx.topological_sort(G)) if v in reroutes}
    paths = [sorted(c, key=indicies.get) for c in nx.weakly_connected_components(SG)]
    paths.sort(key=lambda p: indicies[p[0]])
    return paths


def is_safe_to_remove(v: GNode) -> bool:
    if not v.is_real:
        return True

    return all(
      s.node.select for s in chain(
      config.linked_sockets[v.node.inputs[0]],
      config.linked_sockets[v.node.outputs[0]],
      ))


def get_reroute_segments(CG: ClusterGraph) -> list[list[GNode]]:
    reroute_paths = get_reroute_paths(CG.G, is_safe_to_remove)
    order = tuple(chain(*reroute_paths))

    reroute_clusters = {#
      c for c in CG.S
      if all(v.is_reroute for v in CG.T[c] if isinstance(v, GNode))}
    reroute_segments = []
    for segment in map(Segment, reroute_paths):
        nodes = segment.nodes.copy()
        for children, cluster in group_by(segment, key=lambda v: v.cluster).items():
            if cluster not in reroute_clusters:
                continue

            s1 = segment.split(children[0])
            reroute_segments.append(s1)
            if children[-1] != nodes[-1]:
                reroute_segments.append(s1.split(nodes[nodes.index(children[-1]) + 1]))

        if segment.nodes:
            reroute_segments.append(segment)

    return sorted(map(list, reroute_segments), key=lambda s: order.index(s[0]))


def dissolve_reroute_edges(G: nx.DiGraph, path: list[GNode]) -> None:
    if not G[path[-1]]:
        return

    try:
        u, _, o = next(iter(G.in_edges(path[0], data='from_socket')))
    except StopIteration:
        return

    succ_inputs = [e[2] for e in G.out_edges(path[-1], data='to_socket')]

    # Check if a reroute has been used to link the same output to the same multi-input multiple
    # times
    for *_, d in G.out_edges(u, data=True):
        if d['from_socket'] == o and d['to_socket'] in succ_inputs:
            path.clear()
            return

    links = bpy.context.space_data.edit_tree.links
    for i in succ_inputs:
        G.add_edge(u, i.owner, from_socket=o, to_socket=i)
        links.new(o.bpy, i.bpy)


def remove_reroutes(CG: ClusterGraph) -> None:
    reroute_clusters = {c for c in CG.S if all(v.is_reroute for v in CG.T[c] if not CG.T[v])}
    for path in get_reroute_segments(CG):
        if path[0].cluster in reroute_clusters:
            if len(path) > 2:
                u, *between, v = path
                add_dummy_edge(CG.G, u, v)
                CG.remove_nodes_from(between)
        else:
            dissolve_reroute_edges(CG.G, path)
            CG.remove_nodes_from(path)


# -------------------------------------------------------------------


def add_columns(G: nx.DiGraph) -> None:
    columns = [list(c) for c in group_by(G, key=lambda v: v.rank, sort=True)]
    G.graph['columns'] = columns
    for col in columns:
        for v in col:
            v.col = col


# -------------------------------------------------------------------

_EDGE_SPACING = 10
_MIN_X_DIFF = 7
_MIN_Y_DIFF = 3.5
_FRAME_PADDING = 29.8
_COL_SPACE_FAC = 0.4


def align_reroutes_with_sockets(G: nx.DiGraph) -> None:
    reroute_paths: dict[tuple[GNode, ...], list[Socket]] = {}
    for path in get_reroute_paths(G):
        for subpath in group_by(path, key=lambda v: v.y):
            inputs = G.in_edges(subpath[0], data='from_socket')
            outputs = G.out_edges(subpath[-1], data='to_socket')
            reroute_paths[subpath] = [e[2] for e in (*inputs, *outputs)]

    while True:
        changed = False
        for path, foreign_sockets in tuple(reroute_paths.items()):
            y = path[0].y
            foreign_sockets.sort(key=lambda s: abs(y - s.y))

            if not foreign_sockets or y - foreign_sockets[0].y == 0:
                del reroute_paths[path]
                continue

            movement = y - foreign_sockets[0].y
            y -= movement
            if movement < 0:
                above_y_vals = [
                  (w := v.col[v.col.index(v) - 1]).y - w.height for v in path if v != v.col[0]]
                if above_y_vals and y > min(above_y_vals):
                    continue
            else:
                below_y_vals = [v.col[v.col.index(v) + 1].y for v in path if v != v.col[-1]]
                if below_y_vals and max(below_y_vals) > y - path[0].height:
                    continue

            for v in path:
                v.y -= movement

            changed = True

        if not changed:
            if reroute_paths:
                for path, foreign_sockets in reroute_paths.items():
                    del foreign_sockets[0]
            else:
                break


def route_edges(
  G: nx.MultiDiGraph,
  v: GNode,
  bend_points: defaultdict[tuple[GNode, GNode, int], list[GNode]],
) -> None:
    col_right = max([w.x + w.width for w in v.col])
    for u, w, k, d in *G.in_edges(v, data=True, keys=True), *G.out_edges(v, data=True, keys=True):
        socket = d['from_socket'] if v == u else d['to_socket']
        z = GNode(type=GNodeType.DUMMY)
        z.x = col_right if socket.is_output else v.x

        if abs(socket.x() - z.x) <= _MIN_X_DIFF:
            continue

        z.y = socket.y

        other_socket = next(s for s in d.values() if s != socket)
        if abs(other_socket.y - z.y) <= _MIN_Y_DIFF:
            continue

        if socket.is_output:
            bend_points[u, w, k].insert(0, z)
        else:
            bend_points[u, w, k].append(z)


def assign_x_coords_and_route_edges(G: nx.MultiDiGraph, T: nx.DiGraph) -> None:
    bend_points = defaultdict(list)

    columns = G.graph['columns']
    x = 0
    edge_space_fac = min(1, _EDGE_SPACING / config.MARGIN.x)
    for i, col in enumerate(columns):
        max_width = max([v.width for v in col])

        for v in col:
            v.x = x - (v.width - max_width) / 2

        y_diffs = []
        for v in col:
            y_diffs.extend([
              abs(d['to_socket'].y - d['from_socket'].y) for *_, d in G.out_edges(v, data=True)])
            route_edges(G, v, bend_points)

        max_y_diff = max(y_diffs, default=0)
        x += max_width + _COL_SPACE_FAC * edge_space_fac * max_y_diff + config.MARGIN.x

        if col == columns[-1]:
            continue

        if {v.cluster for v in col} ^ {v.cluster for v in columns[i + 1]}:
            x += _FRAME_PADDING

    # -------------------------------------------------------------------

    edge_of = {w: k for k, v in bend_points.items() for w in v}
    for target, *redundant in group_by(edge_of, key=lambda v: (edge_of[v][0], v.x, v.y)):
        for v in redundant:
            dummy_nodes = bend_points[edge_of[v]]
            dummy_nodes[dummy_nodes.index(v)] = target

    # -------------------------------------------------------------------

    pairs = {(u, v) for u, v, k in bend_points}
    lca = dict(nx.tree_all_pairs_lowest_common_ancestor(T, pairs=pairs))
    for e, dummy_nodes in bend_points.items():
        add_dummy_nodes_to_edge(G, e, dummy_nodes)
        c = lca[*e[:2]]
        for v in dummy_nodes:
            v.cluster = c


# -------------------------------------------------------------------


def simplify_segment(CG: ClusterGraph, aligned: Sequence[GNode], path: list[GNode]) -> None:
    if len(aligned) == 1:
        return

    u, *between, v = aligned
    G = CG.G

    if (s := next(iter(G.in_edges(u, data='from_socket')))[2]).y == u.y:
        G.add_edge(s.owner, v, from_socket=s, to_socket=Socket(v, 0, False))
        between.append(u)
    elif G.out_degree(v) == 1 and v.y == (s := next(iter(G.out_edges(v, data='to_socket')))[2]).y:
        G.add_edge(u, s.owner, from_socket=Socket(u, 0, True), to_socket=s)
        between.append(v)
    else:
        add_dummy_edge(G, u, v)

    CG.remove_nodes_from(between)
    for v in between:
        if v not in G:
            path.remove(v)


def add_reroute(v: GNode) -> None:
    reroute = bpy.context.space_data.edit_tree.nodes.new(type='NodeReroute')
    reroute.parent = v.cluster.node
    config.selected.append(reroute)
    v.node = reroute
    v.type = GNodeType.NODE


def realize_edges(G: nx.DiGraph, v: GNode) -> None:
    links = bpy.context.space_data.edit_tree.links

    if G.pred[v]:
        pred_output = next(iter(G.in_edges(v, data='from_socket')))[2]
        links.new(pred_output.bpy, v.node.inputs[0])

    for _, w, succ_input in G.out_edges(v, data='to_socket'):
        if w.is_real:
            links.new(v.node.outputs[0], succ_input.bpy)


def realize_dummy_nodes(CG: ClusterGraph) -> None:
    for path in get_reroute_segments(CG):
        for aligned in group_by(path, key=lambda v: v.y):
            simplify_segment(CG, aligned, path)

        for v in path:
            if not v.is_real:
                add_reroute(v)

            realize_edges(CG.G, v)


def realize_locations(G: nx.DiGraph, old_center: Vector) -> None:
    new_center = (fmean([v.x for v in G]), fmean([v.y for v in G]))
    offset_x, offset_y = -Vector(new_center) + old_center

    v: GNode
    for v in G:
        # Optimization: avoid using bpy.ops for as many nodes as possible (see `utils.move()`)
        v.node.parent = None
        move_to(v.node, x=v.x + offset_x, y=v.corrected_y() + offset_y)
        v.node.parent = v.cluster.node


# -------------------------------------------------------------------


def sugiyama_layout(ntree: NodeTree) -> None:
    locs = [abs_loc(n) for n in config.selected if n.bl_idname != 'NodeFrame']

    if not locs:
        return

    old_center = Vector(map(fmean, zip(*locs)))

    precompute_links(ntree)
    CG = ClusterGraph(get_multidigraph())

    remove_reroutes(CG)

    compute_ranks(CG)
    CG.insert_dummy_nodes()

    G = CG.G
    add_columns(G)
    minimize_crossings(G, CG.T)

    if len(CG.S) == 1:
        bk_assign_y_coords(G)
    else:
        CG.add_vertical_border_nodes()
        linear_segments_assign_y_coords(CG)
        CG.remove_nodes_from([v for v in G if v.type == GNodeType.VERTICAL_BORDER])

    align_reroutes_with_sockets(G)
    assign_x_coords_and_route_edges(G, CG.T)

    realize_dummy_nodes(CG)
    realize_locations(G, old_center)
