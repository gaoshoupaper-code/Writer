import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { CharacterMarkdownFile } from "../../lib/types";

type CharactersPanelProps = {
  characters: CharacterMarkdownFile[];
  activeFilename: string;
  loading: boolean;
  onSelectCharacter: (filename: string) => void;
};

export function CharactersPanel({ characters, activeFilename, loading, onSelectCharacter }: CharactersPanelProps) {
  const activeCharacter = characters.find((character) => character.filename === activeFilename) ?? characters[0];

  return (
    <section className="panel-surface content-panel" aria-label="人物角色">
      <div className="panel-heading">
        <div>
          <span className="section-kicker">Characters</span>
          <h2>人物角色</h2>
        </div>
        {loading ? <span className="outline-state">加载中</span> : null}
      </div>

      <div className="content-panel-body">
        {characters.length ? (
          <div className="character-layout">
            <aside className="character-sidebar" aria-label="人物信息表">
              <span className="field-label">人物信息表</span>
              <div className="character-list">
                {characters.map((character) => (
                  <button
                    className={`character-list-item${character.filename === activeCharacter?.filename ? " active" : ""}`}
                    key={character.filename}
                    type="button"
                    onClick={() => onSelectCharacter(character.filename)}
                  >
                    <span>{character.name}</span>
                    <small>{character.filename}</small>
                  </button>
                ))}
              </div>
            </aside>

            <article className="outline-markdown character-markdown">
              {activeCharacter?.markdown.trim() ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{activeCharacter.markdown}</ReactMarkdown> : <p>这个人物文件暂无内容。</p>}
            </article>
          </div>
        ) : (
          <div className="empty-state">
            <span className="placeholder-mark">暂无角色</span>
            <h3>当前工作目录中暂无人物信息文件</h3>
            <p>生成角色后，后端工作目录的 character 文件夹中的 Markdown 文件会在这里显示。</p>
          </div>
        )}
      </div>
    </section>
  );
}
