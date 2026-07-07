export type ExtractedCharacter = {
  name: string;
  description: string;
};

const CHARACTER_SECTION_TITLES = new Set([
  "人物角色",
  "角色",
  "主要角色",
  "角色设定",
  "人物设定",
  "人物小传",
  "角色弧光",
]);

const headingPattern = /^(#{1,6})\s+(.+?)\s*$/;
const listItemPattern = /^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+?)\s*$/;
const characterDividerPattern = /[：:—-]/;

function cleanHeadingTitle(title: string) {
  return title.replace(/[：:：\s]+$/g, "").trim();
}

function parseCharacterLine(line: string): ExtractedCharacter | null {
  const listMatch = line.match(listItemPattern);
  if (!listMatch) return null;

  const content = listMatch[1].trim();
  const dividerMatch = content.match(characterDividerPattern);
  if (!dividerMatch || dividerMatch.index === undefined) return null;

  const name = content.slice(0, dividerMatch.index).replace(/^[《「『“"]|[》」』”"]$/g, "").trim();
  const description = content.slice(dividerMatch.index + dividerMatch[0].length).trim();

  if (!name || !description) return null;

  return { name, description };
}

export function extractCharactersFromOutline(markdown: string): ExtractedCharacter[] {
  const lines = markdown.split(/\r?\n/);
  const characters: ExtractedCharacter[] = [];
  let activeHeadingDepth: number | null = null;

  for (const line of lines) {
    const headingMatch = line.match(headingPattern);

    if (headingMatch) {
      const depth = headingMatch[1].length;
      const title = cleanHeadingTitle(headingMatch[2]);

      if (activeHeadingDepth !== null && depth <= activeHeadingDepth) {
        activeHeadingDepth = null;
      }

      if (CHARACTER_SECTION_TITLES.has(title)) {
        activeHeadingDepth = depth;
      }

      continue;
    }

    if (activeHeadingDepth === null) continue;

    const character = parseCharacterLine(line);
    if (character) {
      characters.push(character);
    }
  }

  return characters;
}
