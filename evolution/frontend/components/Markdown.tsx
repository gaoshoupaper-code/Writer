import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Prompt / Skill 正文渲染。
 * 取代裸 <pre>：把 # ** - > | 这些符号噪音消化成真正的文档排版。
 * 配合 globals.css 的 .prose-doc 使用。
 *
 * GFM 开启：表格、删除线、任务列表、自动链接。
 */
function MarkdownImpl({ children }: { children: string }) {
  return (
    <div className="prose-doc">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  );
}

export const Markdown = memo(MarkdownImpl);
