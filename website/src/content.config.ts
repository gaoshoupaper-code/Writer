import { defineCollection, z } from "astro:content";
import { glob } from "astro/loaders";

/// 官网内容集合（D18：Astro Content Collections 作为 CMS）。
/// 内容用 markdown 文件管理，git 版本化，构建时生成静态站。
///
/// features 集合：功能介绍页内容（每个功能一篇 md）。
/// 改内容 = 改 md 文件 + 重新构建部署，无需 CMS 后台。

const features = defineCollection({
  loader: glob({ pattern: "**/*.md", base: "./src/content/features" }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    icon: z.string().optional(),
    order: z.number().default(0),
  }),
});

export const collections = { features };
