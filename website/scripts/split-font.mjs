/**
 * 字体子集化脚本 —— 把 Noto Serif SC 的中文简体大包切成 web 分片。
 *
 * 为何需要：fontsource 提供的 chinese-simplified 单字重 woff2 约 1.5MB，
 * 首屏整包下载会拖慢加载。cn-font-split 按 unicode-range 切成数百个小包，
 * 浏览器只按需拉取页面实际用到的字（首屏通常 <100KB）。
 *
 * 用法：
 *   node scripts/split-font.mjs
 *
 * 何时跑：首次接入 / 新增文案出现新字时重跑（见 CLAUDE.md 提醒）。
 * 产物提交 git，不纳入每次 build。
 *
 * 输入源：@fontsource/noto-serif-sc/files/*.woff2（devDependency 提供的完整字符集）
 * 输出：public/fonts/noto-serif-sc/{weight}/  （分片 woff2 + index.css）
 */
import { fontSplit } from "cn-font-split";
import path from "node:path";
import fs from "node:fs/promises";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..");

// 只切我们用到的两个字重（400 正文 / 600 标题），废除原 900。
const weights = [
  { weight: 400, name: "regular" },
  { weight: 600, name: "semibold" },
];

const fontSourceDir = path.join(
  root,
  "node_modules/@fontsource/noto-serif-sc/files"
);
const outBase = path.join(root, "public/fonts/noto-serif-sc");

for (const { weight, name } of weights) {
  const input = path.join(
    fontSourceDir,
    `noto-serif-sc-chinese-simplified-${weight}-normal.woff2`
  );
  const outDir = path.join(outBase, name);

  console.log(`\n→ 切割 weight ${weight} (${name}) ...`);
  await fontSplit({
    input,
    outDir,
    // 生成 CSS 里的 url() 基路径，对应最终线上路径
    destUrl: `/fonts/noto-serif-sc/${name}/`,
    cssFileName: "index.css",
    targetType: "woff2",
    // 每个分片目标体积 ~100KB，平衡请求数与首屏
    chunkSize: 100 * 1024,
    testHTML: false,
    // 强制覆盖 CSS @font-face 声明：
    //   fontFamily —— 源 woff2 内嵌名是 "Noto Serif SC ExtraLight"（variable font
    //     实例名），强制统一为 "Noto Serif SC"，与 --font-serif token 对齐。
    //   fontWeight —— 显式锁定字重数值，避免浏览器按名推断错档。
    css: {
      fontFamily: "Noto Serif SC",
      fontWeight: String(weight),
      fontDisplay: "swap",
    },
    reporters: [
      {
        name: "log",
        report: (info) => {
          if (info.message) console.log(`  ${info.message}`);
        },
      },
    ],
  });
  console.log(`✓ weight ${weight} 完成 → ${outDir}`);

  // cn-font-split v7 的 cssFileName 参数未生效，固定输出 result.css。
  // 重命名为 index.css，并清理调试产物（index.html / *.proto / *.bin）。
  await fs.rename(path.join(outDir, "result.css"), path.join(outDir, "index.css"));
  for (const junk of ["index.html", "index.proto", "reporter.bin"]) {
    await fs.rm(path.join(outDir, junk), { force: true });
  }
  console.log(`  已整理：index.css + ${68} 个 woff2 分片`);
}

console.log("\n全部完成。");
