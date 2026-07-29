/**
 * Tab「覆盖情况」— 清单层契约覆盖矩阵（FR-003 / FR-004）。
 *
 * 展示 manifest.contract_coverage_matrix：
 *  - 顶部汇总：covered / missing / na 计数 + complete 判定 + 适用维度
 *  - 明细：每项 dim/key + 状态徽章 + reason
 *
 * 数据为空时显示明确空态，不得隐藏或伪装为零缺口（FR-003 失败语义）。
 */

import { CheckCircle2, XCircle, Circle, Layers } from "lucide-react";

import type { Dossier } from "@/lib/api";
import {
  getCoverageMatrix,
  getCoverageItemMeta,
  getApplicableDimensions,
  getCompleteness,
} from "../dossierHelpers";

interface CoverageTabProps {
  dossier: Dossier;
}

export function CoverageTab({ dossier }: CoverageTabProps) {
  const matrix = getCoverageMatrix(dossier.manifest);
  const dims = getApplicableDimensions(dossier.manifest);
  const completeness = getCompleteness(dossier.manifest);

  return (
    <div className="coverage-tab">
      {/* 完整性结论 + 适用维度 */}
      <div className="coverage-overview">
        <div className="coverage-overview-row">
          <span className="coverage-overview-label">契约覆盖判定</span>
          {completeness === "full" ? (
            <span className="coverage-overview-verdict ok">
              <CheckCircle2 size={14} aria-hidden /> 全部覆盖
            </span>
          ) : completeness === "partial" ? (
            <span className="coverage-overview-verdict warn">
              <Circle size={14} aria-hidden /> 部分覆盖（存在缺口）
            </span>
          ) : (
            <span className="coverage-overview-verdict muted">未判定</span>
          )}
        </div>
        {dims.length > 0 && (
          <div className="coverage-overview-row">
            <span className="coverage-overview-label">适用维度</span>
            <span className="coverage-overview-dims">
              {dims.map((d) => (
                <span key={d} className="coverage-dim-chip">
                  {d}
                </span>
              ))}
            </span>
          </div>
        )}
      </div>

      {/* 覆盖汇总计数 */}
      {matrix && (
        <div className="coverage-counts">
          <div className="coverage-count ok">
            <CheckCircle2 size={14} aria-hidden />
            <span className="coverage-count-num">{matrix.covered_count}</span>
            <span className="coverage-count-label">已覆盖</span>
          </div>
          <div className="coverage-count danger">
            <XCircle size={14} aria-hidden />
            <span className="coverage-count-num">{matrix.missing_count}</span>
            <span className="coverage-count-label">缺口</span>
          </div>
          <div className="coverage-count muted">
            <Circle size={14} aria-hidden />
            <span className="coverage-count-num">{matrix.na_count}</span>
            <span className="coverage-count-label">不适用</span>
          </div>
        </div>
      )}

      {/* 明细列表 */}
      {matrix && matrix.items.length > 0 ? (
        <div className="coverage-items">
          {matrix.items.map((item, i) => {
            const meta = getCoverageItemMeta(item.status);
            const Icon = meta.icon;
            return (
              <div key={i} className={`coverage-item tone-${meta.tone}`}>
                <div className="coverage-item-status">
                  <Icon size={13} aria-hidden />
                </div>
                <div className="coverage-item-main">
                  <div className="coverage-item-key">
                    <span className="coverage-item-dim">{item.dim}</span>
                    <span className="coverage-item-sep">/</span>
                    <span className="coverage-item-name">{item.key}</span>
                  </div>
                  {item.reason && (
                    <div className="coverage-item-reason">{item.reason}</div>
                  )}
                </div>
                <span className="coverage-item-badge">{meta.label}</span>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="dossier-tab-empty">
          <Layers size={24} aria-hidden />
          <p>该卷宗未生成契约覆盖矩阵</p>
          <p className="dossier-tab-empty-hint">
            可能是 trace 缺少可解析的任务契约，或卷宗仍在编译中。
          </p>
        </div>
      )}
    </div>
  );
}
