import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Background,
  Controls,
  MarkerType,
  ReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { ExternalLink, Search } from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { getLineage } from "@/lib/api";
import type { LineageEdge, LineageObjectType, LineageResponse } from "@/lib/types";

const OBJECT_TYPES: Array<{ value: LineageObjectType; label: string }> = [
  { value: "trace", label: "Trace" },
  { value: "artifact_revision", label: "ArtifactRevision" },
  { value: "evidence_dossier", label: "EvidenceDossier" },
  { value: "evaluation_dossier", label: "EvaluationDossier" },
  { value: "score", label: "Score" },
  { value: "experiment", label: "Experiment" },
  { value: "candidate", label: "Candidate" },
  { value: "release", label: "Release" },
];
const STAGES: Record<LineageObjectType, number> = {
  trace: 0,
  artifact_revision: 1,
  evidence_dossier: 2,
  evaluation_dossier: 3,
  score: 4,
  candidate: 4,
  experiment: 5,
  release: 6,
};

export default function LineagePage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const initialType = parseObjectType(searchParams.get("type")) || "trace";
  const [objectType, setObjectType] = useState<LineageObjectType>(initialType);
  const [objectId, setObjectId] = useState(searchParams.get("id") || "");
  const [graph, setGraph] = useState<LineageResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (type: LineageObjectType, id: string) => {
    const normalizedId = id.trim();
    if (!normalizedId) return;
    setLoading(true);
    try {
      const result = await getLineage(type, normalizedId);
      setGraph(result);
      setObjectType(type);
      setObjectId(normalizedId);
      setSearchParams({ type, id: normalizedId });
      setError(null);
    } catch (caught) {
      setGraph(null);
      setError(caught instanceof Error ? caught.message : "血缘查询失败");
    } finally {
      setLoading(false);
    }
  }, [setSearchParams]);

  useEffect(() => {
    const id = searchParams.get("id");
    const type = parseObjectType(searchParams.get("type"));
    if (id && type) void load(type, id);
  }, []); // URL 只用于页面初始定位，后续由 load 同步。

  const flow = useMemo(() => buildFlow(graph), [graph]);

  return (
    <div className="trace-workbench lineage-workbench">
      <header className="workbench-header">
        <div><h1>血缘</h1><p>Writer 固定因果链</p></div>
        {graph?.object.type === "trace" && (
          <button className="secondary-command" onClick={() => navigate(`/traces/${graph.object.id}`)}>
            <ExternalLink size={14} />打开 Trace
          </button>
        )}
      </header>

      <form className="lineage-search" onSubmit={(event) => {
        event.preventDefault();
        void load(objectType, objectId);
      }}>
        <select value={objectType} onChange={(event) => setObjectType(event.target.value as LineageObjectType)} aria-label="对象类型">
          {OBJECT_TYPES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select>
        <input value={objectId} onChange={(event) => setObjectId(event.target.value)} placeholder="对象 ID" aria-label="对象 ID" />
        <button className="primary-command" type="submit" disabled={!objectId.trim() || loading}>
          <Search size={15} />{loading ? "查询中" : "查询"}
        </button>
      </form>

      {error && <div className="inline-error">{error}</div>}
      {!graph && !error && <div className="workbench-empty lineage-empty">输入持久对象 ID</div>}

      {graph && (
        <>
          <section className="workbench-section lineage-graph-section">
            <div className="section-heading">
              <h2>{objectLabel(graph.object.type)}</h2><code>{graph.object.id}</code>
            </div>
            <div className="lineage-canvas">
              <ReactFlow
                nodes={flow.nodes}
                edges={flow.edges}
                fitView
                fitViewOptions={{ padding: 0.22 }}
                minZoom={0.4}
                maxZoom={1.5}
                nodesDraggable={false}
                nodesConnectable={false}
                onNodeClick={(_, node) => {
                  const data = node.data as { objectType: LineageObjectType; objectId: string };
                  void load(data.objectType, data.objectId);
                }}
              >
                <Background gap={24} size={1} color="var(--line-strong)" />
                <Controls showInteractive={false} />
              </ReactFlow>
            </div>
          </section>

          <section className="workbench-section">
            <div className="section-heading"><h2>事实边</h2><span>{graph.incoming.length + graph.outgoing.length}</span></div>
            {graph.incoming.length + graph.outgoing.length === 0 ? (
              <div className="workbench-empty">无关联事实</div>
            ) : (
              <div className="run-table-wrap">
                <table className="workbench-table lineage-table">
                  <thead><tr><th>来源</th><th>关系</th><th>目标</th><th>记录时间</th></tr></thead>
                  <tbody>{[...graph.incoming, ...graph.outgoing].map((edge) => (
                    <tr key={edge.id}>
                      <td><LineageObjectButton type={edge.from_type} id={edge.from_id} onOpen={load} /></td>
                      <td><span className="relation-label">{edge.relation}</span></td>
                      <td><LineageObjectButton type={edge.to_type} id={edge.to_id} onOpen={load} /></td>
                      <td className="run-time-cell">{formatDateTime(edge.created_at)}</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}

function buildFlow(graph: LineageResponse | null): { nodes: Node[]; edges: Edge[] } {
  if (!graph) return { nodes: [], edges: [] };
  const allEdges = [...graph.incoming, ...graph.outgoing];
  const objects = new Map<string, { type: LineageObjectType; id: string }>();
  const add = (type: LineageObjectType, id: string) => objects.set(`${type}:${id}`, { type, id });
  add(graph.object.type, graph.object.id);
  allEdges.forEach((edge) => { add(edge.from_type, edge.from_id); add(edge.to_type, edge.to_id); });
  const stageCounts = new Map<number, number>();
  const nodes = [...objects.values()].map((object) => {
    const stage = STAGES[object.type];
    const row = stageCounts.get(stage) || 0;
    stageCounts.set(stage, row + 1);
    const selected = object.type === graph.object.type && object.id === graph.object.id;
    return {
      id: `${object.type}:${object.id}`,
      position: { x: stage * 240, y: row * 120 },
      data: {
        label: `${objectLabel(object.type)}\n${shortId(object.id)}`,
        objectType: object.type,
        objectId: object.id,
      },
      className: `lineage-node${selected ? " selected" : ""}`,
      style: { width: 190 },
    };
  });
  const edges = allEdges.map((edge: LineageEdge) => ({
    id: String(edge.id),
    source: `${edge.from_type}:${edge.from_id}`,
    target: `${edge.to_type}:${edge.to_id}`,
    label: edge.relation,
    markerEnd: { type: MarkerType.ArrowClosed },
    className: "lineage-edge",
  }));
  return { nodes, edges };
}

function LineageObjectButton({
  type, id, onOpen,
}: {
  type: LineageObjectType;
  id: string;
  onOpen: (type: LineageObjectType, id: string) => Promise<void>;
}) {
  return (
    <button className="lineage-object-link" onClick={() => void onOpen(type, id)} title={id}>
      <span>{objectLabel(type)}</span><code>{shortId(id)}</code>
    </button>
  );
}

function parseObjectType(value: string | null): LineageObjectType | null {
  return OBJECT_TYPES.some((item) => item.value === value) ? value as LineageObjectType : null;
}

function objectLabel(type: LineageObjectType): string {
  return OBJECT_TYPES.find((item) => item.value === type)?.label || type;
}

function shortId(id: string): string {
  return id.length > 24 ? `${id.slice(0, 21)}…` : id;
}

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}
