import LlmConfigPanel from "./LlmConfigPanel";

/**
 * 执行端模型配置页（scope 分家 2026-07-18，新建）。
 *
 * executor 给用户写正文时调用的大模型配置。
 * 与"进化端模型"独立——可使用不同厂商/模型，各自维护激活项。
 */
export default function ExecutorConfigPage() {
  return (
    <>
      <header className="page-header">
        <h1>执行端模型</h1>
        <p className="page-desc">
          executor 给用户写正文时调用的大模型配置。与"进化端模型"独立，
          可使用不同厂商或模型——例如进化端用强模型评估、执行端用快速模型写作。
          可保存多个配置，选一个激活，executor 运行时读激活项（约 60s 内生效）。
        </p>
      </header>
      <LlmConfigPanel scope="executor" />
    </>
  );
}
