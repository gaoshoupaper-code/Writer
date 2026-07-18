import LlmConfigPanel from "./LlmConfigPanel";
import AppUpdateCheck from "./AppUpdateCheck";

/**
 * 进化端模型配置页（scope 分家 2026-07-18）。
 *
 * 进化 Agent 做 evaluate / evolve 时用的 LLM 配置。
 * 含 App 更新检查（D22：从原 config.tsx 迁移过来）。
 */
export default function EvolutionConfigPage() {
  return (
    <>
      <header className="page-header">
        <h1>进化端模型</h1>
        <p className="page-desc">
          进化 Agent 做评估（evaluate）和进化（evolve）时调用的大模型配置。
          可保存多个配置，选一个激活——运行时读激活项。API Key 加密存储在服务器，运行时解密调用。
        </p>
      </header>
      <LlmConfigPanel scope="evolution" />
      <AppUpdateCheck />
    </>
  );
}
