"use client";

import { useState } from "react";
import type { ImageReviewInterrupt, ImageReviewResume } from "../../lib/api";
import { imageUrl } from "../../lib/api";

type ImageReviewCardProps = {
  payload: ImageReviewInterrupt;
  onSubmit: (resume: ImageReviewResume) => Promise<void>;
  disabled?: boolean;
};

/**
 * 图像评审卡片（D12/D13）。
 * - 3×2 图片网格（3 版 × 双采样）
 * - Agent 自评分析（D5 第一层，展示给用户参考）
 * - 每版 1-5 星打分 + 文本框（D13）
 * - 整体优化方向 + 继续/停止（D6）
 */
export function ImageReviewCard({ payload, onSubmit, disabled }: ImageReviewCardProps) {
  const { round, versions } = payload;
  const [scores, setScores] = useState<Record<string, number>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [overallDirection, setOverallDirection] = useState("");
  const [submitted, setSubmitted] = useState(false);

  function setScore(versionId: string, score: number) {
    setScores((cur) => ({ ...cur, [versionId]: score }));
  }

  function setNote(versionId: string, note: string) {
    setNotes((cur) => ({ ...cur, [versionId]: note }));
  }

  const allScored = versions.every((v) => scores[v.version_id] != null);

  function buildResume(action: "continue" | "stop"): ImageReviewResume {
    return {
      kind: "image_review",
      round,
      ratings: versions.map((v) => ({
        version_id: v.version_id,
        score: scores[v.version_id] ?? 0,
        note: notes[v.version_id] ?? "",
      })),
      overall_direction: overallDirection.trim(),
      action,
    };
  }

  async function handleSubmit(action: "continue" | "stop") {
    if (disabled || submitted) return;
    if (action === "continue" && !allScored) return;
    setSubmitted(true);
    try {
      await onSubmit(buildResume(action));
    } catch {
      setSubmitted(false);
    }
  }

  return (
    <div className="image-review-card">
      <div className="image-review-header">
        <strong>第 {round} 轮评审</strong>
        <span className="image-review-hint">对 3 个版本打分（1-5 星），可选择继续迭代或定稿</span>
      </div>

      <div className="image-review-versions">
        {versions.map((v, idx) => (
          <div key={v.version_id} className="image-version">
            <div className="image-version-header">
              <span className="image-version-label">版本 {idx + 1}</span>
              <span className="image-version-direction">{v.direction}</span>
            </div>

            <div className="image-version-grid">
              {v.images.map((img) => (
                <div key={img.image_id} className="image-version-cell">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={imageUrl(img.image_id)}
                    alt={`${v.version_id} ${img.image_id}`}
                    className="image-version-img"
                  />
                </div>
              ))}
            </div>

            {v.agent_analysis ? (
              <div className="image-version-analysis">
                <small>Agent 自评：</small>
                <p>{v.agent_analysis}</p>
              </div>
            ) : null}

            <div className="image-version-rating">
              <div className="star-row">
                {[1, 2, 3, 4, 5].map((star) => (
                  <button
                    key={star}
                    type="button"
                    className={`star-btn${(scores[v.version_id] ?? 0) >= star ? " active" : ""}`}
                    onClick={() => setScore(v.version_id, star)}
                    disabled={disabled || submitted}
                    aria-label={`${star} 星`}
                  >
                    ★
                  </button>
                ))}
                <span className="star-score">
                  {scores[v.version_id] ? `${scores[v.version_id]} / 5` : "未评"}
                </span>
              </div>
              <textarea
                className="image-version-note"
                value={notes[v.version_id] ?? ""}
                onChange={(e) => setNote(v.version_id, e.target.value)}
                placeholder="这版的反馈（哪里好/哪里不好/优化方向）"
                rows={2}
                disabled={disabled || submitted}
              />
            </div>
          </div>
        ))}
      </div>

      <div className="image-review-overall">
        <textarea
          className="image-review-direction"
          value={overallDirection}
          onChange={(e) => setOverallDirection(e.target.value)}
          placeholder="整体优化方向（可选，如'颜色太暗，往明亮走'）"
          rows={2}
          disabled={disabled || submitted}
        />
        <div className="image-review-actions">
          <button
            type="button"
            className="image-review-btn continue"
            onClick={() => handleSubmit("continue")}
            disabled={disabled || submitted || !allScored}
          >
            提交打分，继续迭代
          </button>
          <button
            type="button"
            className="image-review-btn stop"
            onClick={() => handleSubmit("stop")}
            disabled={disabled || submitted}
          >
            满意了，定稿
          </button>
        </div>
      </div>
    </div>
  );
}
