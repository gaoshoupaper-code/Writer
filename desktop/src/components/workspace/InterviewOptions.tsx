import { useState } from "react";
import type { AskUserOption } from "../../lib/types";

type InterviewOptionsProps = {
  options: AskUserOption[];
  multiSelect: boolean;
  onSubmit: (resume: string) => Promise<void>;
  disabled?: boolean;
};

// D-6：mode 问题宽松判定——options 恰为 auto/interactive 时，此题为流程二选一，
// 不渲染「自定义/补充」入口（X4：mode 不设自定义）。
function isModeQuestion(options: AskUserOption[]): boolean {
  if (options.length !== 2) return false;
  const labels = options.map((o) => o.label);
  return labels.includes("auto") && labels.includes("interactive");
}

/**
 * 访谈选项化 UI（设计 §4.2）。
 * - 选项点击选中：multiSelect 决定单选(radio)/多选(checkbox)。
 * - 「自定义/补充」入口（mode 题除外）：点击后内联展开输入框；语义双兼——
 *   选了普通选项 + 框内有字 = 补充选中项；未选 + 框内有字 = 纯自定义。
 * - 「提交」按钮：组装 resume 字符串（§6.2）后回调 onSubmit；全空时禁用。
 */
export function InterviewOptions({ options, multiSelect, onSubmit, disabled }: InterviewOptionsProps) {
  const modeQuestion = isModeQuestion(options);
  const [selected, setSelected] = useState<string[]>([]);
  const [customOpen, setCustomOpen] = useState(false);
  const [customText, setCustomText] = useState("");
  // 点2：提交后永久灰显，不随 loading 复活（修复重复提交 bug）；失败时解锁可重试
  const [submitted, setSubmitted] = useState(false);

  function toggle(label: string) {
    setSelected((cur) => {
      if (multiSelect) {
        return cur.includes(label) ? cur.filter((l) => l !== label) : [...cur, label];
      }
      // 单选：点已选中项取消，点新项替换
      return cur[0] === label ? [] : [label];
    });
  }

  // §6.2 resume 组装规则
  function buildResume(): string {
    const labels = selected;
    const text = customText.trim();
    if (labels.length && text) return `${labels.join("、")}（${text}）`;
    if (labels.length) return labels.join("、");
    return text;
  }

  const resume = buildResume();
  const canSubmit = resume.length > 0 && !disabled && !submitted;

  async function handleSubmit() {
    if (!canSubmit) return;
    setSubmitted(true);
    try {
      await onSubmit(resume);
    } catch {
      // resume 失败（performSubmit 在 resume 失败时 re-throw）：解锁让用户重试。
      // 错误提示由 performSubmit 内部 toast 负责，这里不重复弹窗。
      setSubmitted(false);
    }
  }

  return (
    <div className="interview-options">
      <div className="interview-options-list">
        {options.map((opt) => {
          const checked = selected.includes(opt.label);
          return (
            <button
              key={opt.label}
              type="button"
              className={`interview-option${checked ? " checked" : ""}`}
              onClick={() => toggle(opt.label)}
              disabled={disabled || submitted}
            >
              <span className="interview-option-mark">
                {multiSelect ? (checked ? "☑" : "☐") : checked ? "◉" : "○"}
              </span>
              <span className="interview-option-body">
                <span className="interview-option-label">{opt.label}</span>
                {opt.description ? <span className="interview-option-desc">{opt.description}</span> : null}
              </span>
            </button>
          );
        })}
      </div>

      {modeQuestion ? null : (
        <div className="interview-custom">
          <button
            type="button"
            className={`interview-option custom${customOpen ? " checked" : ""}`}
            onClick={() => setCustomOpen((v) => !v)}
            disabled={disabled || submitted}
          >
            <span className="interview-option-mark">{customOpen ? "◉" : "○"}</span>
            <span className="interview-option-label">自定义 / 补充</span>
          </button>
          {customOpen ? (
            <textarea
              className="interview-custom-input"
              value={customText}
              onChange={(e) => setCustomText(e.target.value)}
              placeholder={selected.length ? "补充说明（可选）" : "输入你的想法"}
              rows={2}
              disabled={disabled || submitted}
            />
          ) : null}
        </div>
      )}

      <div className="interview-options-actions">
        <button type="button" className="interview-submit" onClick={handleSubmit} disabled={!canSubmit}>
          提交
        </button>
      </div>
    </div>
  );
}
