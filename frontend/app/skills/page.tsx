"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  deleteSkill,
  fetchMeOrNull,
  listSkills,
  readSkill,
  updateSkill,
  type SkillDetail,
  type SkillSummary,
} from "../../lib/api";

/**
 * Skill 管理页（D18：查看列表 / 重命名 / 编辑正文 / 删除）。
 * 路由：/skills
 * 最小可用版：列表 + 详情编辑（重命名 + 改正文）+ 删除。
 * 合并（D18c）需语义融合，留作增强（API 已就位 mergeSkills）。
 */
export default function SkillsPage() {
  const router = useRouter();
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [selected, setSelected] = useState<SkillDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [editName, setEditName] = useState("");
  const [editContent, setEditContent] = useState("");
  const [saving, setSaving] = useState(false);

  async function loadSkills() {
    setLoading(true);
    try {
      const me = await fetchMeOrNull();
      if (!me) {
        router.push("/login");
        return;
      }
      const list = await listSkills();
      setSkills(list);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadSkills();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function openSkill(skillId: string) {
    const detail = await readSkill(skillId);
    setSelected(detail);
    setEditName(detail.name);
    setEditContent(detail.content);
  }

  async function handleSave() {
    if (!selected || saving) return;
    setSaving(true);
    try {
      // 重命名（元数据）
      if (editName !== selected.name) {
        await updateSkill(selected.skill_id, { name: editName });
      }
      // 改正文（写文件——后端 updateSkill 只改元数据，正文编辑需走 persist 或直接文件写）
      // 当前后端 updateSkill 不改正文；正文编辑通过 read + 本地编辑 + 调 persist_skill 工具完成。
      // 此处最小版：仅保存元数据改名，正文编辑提示用户"正文由 Agent 维护"。
      await loadSkills();
      setSelected(null);
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(skillId: string) {
    if (!confirm("确定删除这个 Skill？此操作不可撤销。")) return;
    await deleteSkill(skillId);
    if (selected?.skill_id === skillId) setSelected(null);
    await loadSkills();
  }

  if (loading) return <div style={{ padding: 24 }}>加载中...</div>;

  return (
    <div style={{ maxWidth: 900, margin: "0 auto", padding: 24 }}>
      <h1>Skill 管理</h1>
      <p style={{ color: "#64748b", fontSize: 14 }}>
        你的 Skills 自进化库。每个 Skill 是一个场景的方法论，随使用不断进化（D18）。
      </p>

      <div style={{ display: "flex", gap: 24, marginTop: 24 }}>
        {/* 列表 */}
        <div style={{ width: 300 }}>
          <h2 style={{ fontSize: 16 }}>我的 Skill（{skills.length}）</h2>
          {skills.length === 0 ? (
            <p style={{ color: "#94a3b8", fontSize: 14 }}>
              还没有 Skill。完成一次文生图闭环并同意持久化后，这里会出现你的第一个 Skill。
            </p>
          ) : (
            <ul style={{ listStyle: "none", padding: 0 }}>
              {skills.map((sk) => (
                <li
                  key={sk.skill_id}
                  style={{
                    padding: "8px 12px",
                    marginBottom: 4,
                    border: "1px solid #e2e8f0",
                    borderRadius: 6,
                    cursor: "pointer",
                    background: selected?.skill_id === sk.skill_id ? "#eff6ff" : "#fff",
                  }}
                >
                  <div onClick={() => openSkill(sk.skill_id)}>
                    <strong>{sk.name}</strong>
                    {sk.scene_tag ? <span style={{ color: "#64748b", fontSize: 12 }}> · {sk.scene_tag}</span> : null}
                    <div style={{ fontSize: 12, color: "#94a3b8" }}>
                      经 {sk.revision_count} 轮进化
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); handleDelete(sk.skill_id); }}
                    style={{ fontSize: 12, color: "#ef4444", background: "none", border: "none", cursor: "pointer", marginTop: 4 }}
                  >
                    删除
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* 详情/编辑 */}
        <div style={{ flex: 1 }}>
          {selected ? (
            <div>
              <h2 style={{ fontSize: 16 }}>编辑 Skill</h2>
              <label style={{ display: "block", fontSize: 13, margin: "8px 0 4px" }}>名称</label>
              <input
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
                style={{ width: "100%", padding: "6px 8px", borderRadius: 4, border: "1px solid #e2e8f0" }}
              />
              <label style={{ display: "block", fontSize: 13, margin: "12px 0 4px" }}>
                正文（SKILL.md）
              </label>
              <textarea
                value={editContent}
                onChange={(e) => setEditContent(e.target.value)}
                rows={20}
                style={{ width: "100%", padding: "8px", borderRadius: 4, border: "1px solid #e2e8f0", fontFamily: "monospace", fontSize: 13 }}
              />
              <p style={{ fontSize: 12, color: "#94a3b8", marginTop: 4 }}>
                注：正文主要由 Agent 在持久化时生成。手动编辑后需通过 Agent 重新持久化才生效。
              </p>
              <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
                <button
                  type="button"
                  onClick={handleSave}
                  disabled={saving}
                  style={{ padding: "6px 16px", borderRadius: 4, border: "none", background: "#3b82f6", color: "#fff", cursor: "pointer" }}
                >
                  {saving ? "保存中" : "保存名称"}
                </button>
                <button
                  type="button"
                  onClick={() => setSelected(null)}
                  style={{ padding: "6px 16px", borderRadius: 4, border: "1px solid #e2e8f0", background: "#fff", cursor: "pointer" }}
                >
                  关闭
                </button>
              </div>
            </div>
          ) : (
            <p style={{ color: "#94a3b8" }}>从左侧选择一个 Skill 查看详情</p>
          )}
        </div>
      </div>
    </div>
  );
}
