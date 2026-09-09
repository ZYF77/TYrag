/**
 * 一次性拆分脚本（Phase D，过程文档，不在构建链路中）。
 *
 * 【注意】Phase D 完成后，输入文件 src/pages/enterprise-console.css 与
 * src/components/layout/console-auth.css 已删除，本脚本不可直接复跑
 * （会 ENOENT）。保留仅为记录拆分与校验算法；后续 CSS 直接在
 * src/styles/ 家族文件上维护。
 *
 * 把整理后的 src/pages/enterprise-console.css 按顶层块（注释/规则/@media/@keyframes，
 * 深度 0）切分，并按选择器家族分桶到 src/styles/ 下的家族文件：
 *   tokens.css / workbench.css / console.css / harness.css / runtime-log.css /
 *   rag-diagnostics.css / question-composer.css / overlay.css / auth.css
 *
 * 规则：
 * - 家族按每个选择器自身的类前缀判定（console-overlay- 优先于 console-）；
 *   同一规则的选择器列表横跨家族时，按家族拆成多条同声明的规则；
 * - @media 块按其内部规则的选择器家族拆分内容（同一条件在不同家族文件中重复出现）；
 * - 家族文件内块按原文件相对顺序排列；
 * - index.css 的 @import 顺序 = 家族在原文件中首次出现的顺序（tokens 恒第一，
 *   auth.css（原 console-auth.css 内容）恒最后，保持其在层叠中位于企业样式之后）；
 * - @keyframes 按名称前缀分桶，无法识别的落入 tokens.css（恒最先加载，作用域全局）。
 *
 * 校验：把原文件与拆分结果分别展平为有序规则序列（带 media 上下文、选择器逐条展开），
 * 对比 a) 规则集合一致；b) 同一 (选择器, media) 组内相对顺序一致。任一不一致则退出码 1。
 *
 * 用法：node scripts/split-css.mjs
 */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SRC_CSS = join(root, 'src/pages/enterprise-console.css');
const SRC_AUTH_CSS = join(root, 'src/components/layout/console-auth.css');
const OUT_DIR = join(root, 'src/styles');

/* ---------------------------------- parsing --------------------------------- */

/** 切分文本为顶层块（深度 0），注释附着到其后的块（作为 leadingComments）。 */
function parseTopLevel(text) {
  const blocks = [];
  let i = 0;
  let pendingComments = [];
  const n = text.length;
  while (i < n) {
    // 跳过块间空白
    while (i < n && /\s/.test(text[i])) i += 1;
    if (i >= n) break;
    if (text.startsWith('/*', i)) {
      const end = text.indexOf('*/', i + 2);
      if (end < 0) throw new Error('未闭合注释');
      pendingComments.push(text.slice(i, end + 2));
      i = end + 2;
      continue;
    }
    // 读到块头：@规则或普通规则的选择器
    const start = i;
    let depth = 0;
    let seenBrace = false;
    while (i < n) {
      const ch = text[i];
      if (ch === '/' && text.startsWith('/*', i)) {
        const e = text.indexOf('*/', i + 2);
        i = e + 2; // 声明区内部注释留在原文里
        continue;
      }
      if (ch === '{') { depth += 1; seenBrace = true; i += 1; continue; }
      if (ch === '}') { depth -= 1; i += 1; if (depth === 0) break; continue; }
      if (!seenBrace && ch === ';' && text.startsWith('@import', start)) {
        // 允许 @import 语句（本文档源文件中没有，仅为完备性）
        i += 1;
        break;
      }
      i += 1;
    }
    if (depth !== 0) throw new Error(`括号不平衡 @${text.slice(start, start + 60)}`);
    const raw = text.slice(start, i).replace(/\s+$/, '');
    blocks.push({ raw, leadingComments: pendingComments });
    pendingComments = [];
  }
  if (pendingComments.length) {
    blocks.push({ raw: '', leadingComments: pendingComments }); // 尾部纯注释
  }
  return blocks;
}

function blockHeader(raw) {
  const b = raw.indexOf('{');
  return b < 0 ? raw.trim() : raw.slice(0, b).trim();
}

function blockBody(raw) {
  const b = raw.indexOf('{');
  return raw.slice(b + 1, raw.lastIndexOf('}')).trim();
}

/** 选择器列表逐条展开（本项目选择器不含顶层逗号出现在括号内的情况，已核实）。 */
function splitSelectorList(header) {
  return header.split(',').map((s) => s.trim()).filter(Boolean);
}

/* ------------------------------- family mapping ------------------------------ */

const FAMILY_FILES = [
  'tokens.css',
  'workbench.css',
  'console.css',
  'harness.css',
  'runtime-log.css',
  'rag-diagnostics.css',
  'question-composer.css',
  'overlay.css',
  'auth.css',
];

function familyOfClassName(cls) {
  if (cls.startsWith('.console-overlay-')) return 'overlay.css';
  if (cls.startsWith('.runtime-')) return 'runtime-log.css';
  if (cls.startsWith('.rag-')) return 'rag-diagnostics.css';
  if (cls.startsWith('.question-composer')) return 'question-composer.css';
  if (cls.startsWith('.workbench')) return 'workbench.css';
  if (cls.startsWith('.harness-')) return 'harness.css';
  // diag-* 仅被 harness 侧组件（DeviceModalForm/HarnessChat/TransientAttachmentPanel）使用
  if (cls.startsWith('.diag-')) return 'harness.css';
  if (cls.startsWith('.console-')) return 'console.css';
  return null;
}

function familyOfSelectorItem(item) {
  if (item === ':root') return 'tokens.css';
  const cls = item.match(/\.[A-Za-z0-9_-]+/);
  if (cls) return familyOfClassName(cls[0]);
  // 元素选择器等全局基础
  return 'tokens.css';
}

function familyOfKeyframes(name) {
  return familyOfClassName(`.${name}`) ?? 'tokens.css';
}

/** 一条规则的家族：所有选择器项同族时返回该族；否则返回 null（需拆分）。 */
function familyOfRule(header) {
  const items = splitSelectorList(header);
  const fams = [...new Set(items.map(familyOfSelectorItem))];
  return fams.length === 1 ? fams[0] : null;
}

/* ------------------------------ flatten for check ---------------------------- */

/** 展平：每条 (选择器项, media上下文, 声明体) 一个条目，保持出现顺序。 */
function flatten(blocks, mediaCtx = '') {
  const entries = [];
  for (const block of blocks) {
    if (!block.raw) continue;
    const header = blockHeader(block.raw);
    if (header.startsWith('@media')) {
      const inner = parseTopLevel(blockBody(block.raw));
      entries.push(...flatten(inner, header));
      continue;
    }
    if (header.startsWith('@keyframes')) {
      entries.push({ selector: header, media: mediaCtx, body: blockBody(block.raw) });
      continue;
    }
    if (header.startsWith('@')) {
      throw new Error(`未处理的 at-rule: ${header}`);
    }
    for (const item of splitSelectorList(header)) {
      entries.push({ selector: item, media: mediaCtx, body: blockBody(block.raw) });
    }
  }
  return entries;
}

function compareFlattened(label, original, result) {
  const problems = [];
  const keyOf = (e) => `${e.media}::${e.selector}`;
  // 声明体按空白归一化后比较（拆分重排只允许缩进/换行差异，不允许内容差异）
  const norm = (e) => e.body.replace(/\s+/g, ' ').trim();
  const sig = (e) => `${e.media}::${e.selector}::${norm(e)}`;

  // a) 集合一致（含重复计数）
  const count = (list) => {
    const m = new Map();
    for (const e of list) m.set(sig(e), (m.get(sig(e)) ?? 0) + 1);
    return m;
  };
  const a = count(original);
  const b = count(result);
  for (const [k, v] of a) if (b.get(k) !== v) problems.push(`${label} 缺失/数量不符: ${k} 原=${v} 新=${b.get(k) ?? 0}`);
  for (const [k, v] of b) if (!a.has(k)) problems.push(`${label} 多出: ${k} 新=${v}`);

  // b) 同一 (selector, media) 组内相对顺序一致
  const group = (list) => {
    const m = new Map();
    for (const e of list) {
      if (!m.has(keyOf(e))) m.set(keyOf(e), []);
      m.get(keyOf(e)).push(norm(e));
    }
    return m;
  };
  const ga = group(original);
  const gb = group(result);
  for (const [k, seq] of ga) {
    const other = gb.get(k) ?? [];
    if (seq.length !== other.length || seq.some((v, idx) => v !== other[idx])) {
      problems.push(`${label} 顺序不一致: ${k}`);
    }
  }
  return problems;
}

/* --------------------------------- splitting --------------------------------- */

function familyBuckets() {
  const buckets = new Map(FAMILY_FILES.map((f) => [f, []]));
  return buckets;
}

/**
 * 把顶层块分配到家族桶。
 * - @media：内部规则按家族拆分；同一原 @media 块在同一家族中的规则合并为一个
 *   @media 块，位置 = 原 @media 块在该家族序列中的插入点。
 * - 普通规则横跨家族时拆分为多条（每族一条，选择器取其子集）。
 */
function splitIntoFamilies(sourceBlocks, familyOverride) {
  const buckets = familyBuckets();
  const insert = (family, node) => buckets.get(family).push(node);
  for (const block of sourceBlocks) {
    if (!block.raw) {
      // 尾部纯注释：跟到最后的家族（auth 或 tokens）
      if (block.leadingComments.length) {
        insert(familyOverride ?? 'tokens.css', { kind: 'raw', text: block.leadingComments.join('\n\n') });
      }
      continue;
    }
    const header = blockHeader(block.raw);
    const lead = block.leadingComments.length
      ? `${block.leadingComments.join('\n\n')}\n`
      : '';
    if (header.startsWith('@media')) {
      const inner = parseTopLevel(blockBody(block.raw));
      const cond = header;
      // family -> { insertIndex 映射到该 family 已push 的 media 节点 }
      const mediaNodes = new Map();
      for (const innerBlock of inner) {
        if (!innerBlock.raw) continue;
        const innerHeader = blockHeader(innerBlock.raw);
        const innerLead = innerBlock.leadingComments.length
          ? `${innerBlock.leadingComments.join('\n\n')}\n  `
          : '';
        if (innerHeader.startsWith('@keyframes')) {
          // media 内 keyframes：本项目不存在该形态，显式报错以防静默丢失
          throw new Error(`@media 内出现 @keyframes，请扩展脚本: ${innerHeader}`);
        }
        const items = splitSelectorList(innerHeader);
        const byFamily = new Map();
        for (const item of items) {
          const fam = familyOverride ?? familyOfSelectorItem(item);
          if (!byFamily.has(fam)) byFamily.set(fam, []);
          byFamily.get(fam).push(item);
        }
        for (const [fam, famItems] of byFamily) {
          let node = mediaNodes.get(fam);
          if (!node) {
            node = { kind: 'media', cond, family: fam, children: [] };
            mediaNodes.set(fam, node);
            insert(fam, node);
          }
          node.children.push({
            kind: 'rule',
            header: famItems.join(',\n  '),
            body: blockBody(innerBlock.raw),
            lead: innerLead,
          });
        }
      }
      continue;
    }
    if (header.startsWith('@keyframes')) {
      const fam = familyOverride ?? familyOfKeyframes(header.replace('@keyframes', '').trim());
      insert(fam, { kind: 'raw', text: lead + block.raw, keyframes: header });
      continue;
    }
    if (header.startsWith('@')) throw new Error(`未处理的 at-rule: ${header}`);
    const fam = familyOverride ?? familyOfRule(header);
    if (fam) {
      insert(fam, { kind: 'rule', header, body: blockBody(block.raw), lead });
    } else {
      // 横跨家族：按族拆分
      const items = splitSelectorList(header);
      const byFamily = new Map();
      for (const item of items) {
        const f = familyOfSelectorItem(item);
        if (!byFamily.has(f)) byFamily.set(f, []);
        byFamily.get(f).push(item);
      }
      for (const [f, famItems] of byFamily) {
        insert(f, { kind: 'rule', header: famItems.join(',\n  '), body: blockBody(block.raw), lead });
      }
    }
  }
  return buckets;
}

function renderRule(rule) {
  const header = rule.header.replace(/,\s*$/, '');
  return `${rule.lead ?? ''}${header} {\n  ${rule.body.replace(/\n/g, '\n  ')}\n}`;
}

function renderMedia(node) {
  const inner = node.children
    .map((c) => (c.kind === 'raw' ? `  ${c.text}` : `  ${renderRule({ ...c, lead: c.lead ? c.lead.trimEnd() + '\n' : '' })}`))
    .join('\n\n');
  return `${node.cond} {\n${inner}\n}`;
}

function renderBucket(nodes) {
  return `${nodes
    .map((node) => {
      if (node.kind === 'raw') return node.text;
      if (node.kind === 'media') return renderMedia(node);
      if (node.kind === 'rule') return renderRule(node);
      throw new Error(`未知节点类型: ${JSON.stringify(node).slice(0, 200)}`);
    })
    .join('\n\n')}\n`;
}

/* ------------------------------- flatten output ------------------------------ */

function flattenRendered(filesContent) {
  const all = [];
  for (const [file, content] of filesContent) {
    const blocks = parseTopLevel(content);
    for (const e of flatten(blocks)) all.push(e);
  }
  return all;
}

/* ----------------------------------- main ------------------------------------ */

const mainBlocks = parseTopLevel(readFileSync(SRC_CSS, 'utf-8'));
const authBlocks = parseTopLevel(readFileSync(SRC_AUTH_CSS, 'utf-8'));

const buckets = splitIntoFamilies(mainBlocks, null);
const authBucket = splitIntoFamilies(authBlocks, 'auth.css');
buckets.get('auth.css').push(...authBucket.get('auth.css'));

// 首次出现顺序：展平原文件，按家族记录首个条目位置；tokens 恒第一，auth 恒最后。
const flat = flatten(mainBlocks);
const firstSeen = new Map();
{
  // 用展平条目反推家族首现位置
  const familiesInOrder = [];
  const seen = new Set();
  const scan = (blocks, mediaCtx) => {
    for (const block of blocks) {
      if (!block.raw) continue;
      const header = blockHeader(block.raw);
      if (header.startsWith('@media')) {
        scan(parseTopLevel(blockBody(block.raw)), header);
        continue;
      }
      if (header.startsWith('@keyframes')) {
        const fam = familyOfKeyframes(header.replace('@keyframes', '').trim());
        if (!seen.has(fam)) { seen.add(fam); familiesInOrder.push(fam); }
        continue;
      }
      for (const item of splitSelectorList(header)) {
        const fam = familyOfSelectorItem(item);
        if (!seen.has(fam)) { seen.add(fam); familiesInOrder.push(fam); }
      }
    }
  };
  scan(mainBlocks, '');
  const ordered = ['tokens.css', ...familiesInOrder.filter((f) => f !== 'tokens.css' && f !== 'auth.css'), 'auth.css'];
  ordered.forEach((f, idx) => firstSeen.set(f, idx));
}

const importOrder = [...firstSeen.keys()].sort((x, y) => firstSeen.get(x) - firstSeen.get(y));

mkdirSync(OUT_DIR, { recursive: true });
const filesContent = [];
for (const fam of importOrder) {
  const nodes = buckets.get(fam) ?? [];
  if (!Array.isArray(nodes)) { console.error("BAD nodes for", fam, JSON.stringify(nodes)); process.exit(2); }
  const content = renderBucket(nodes);
  filesContent.push([fam, content]);
  writeFileSync(join(OUT_DIR, fam), content);
}

const indexCss = `${importOrder.map((f) => `@import './${f}';`).join('\n')}\n`;
writeFileSync(join(OUT_DIR, 'index.css'), indexCss);
filesContent.push(['index.css', indexCss]);

// 校验
const originalEntries = [...flat, ...flatten(authBlocks)];
const resultEntries = flattenRendered(filesContent.filter(([f]) => f !== 'index.css'));
const problems = compareFlattened('split', originalEntries, resultEntries);

// 报告
console.log('=== split-css 校验报告 ===');
for (const [fam, content] of filesContent) {
  const lines = content.split('\n').length;
  console.log(`${fam.padEnd(22)} ${String(lines).padStart(5)} 行`);
}
console.log(`规则条目：原文件 ${originalEntries.length} 条 / 拆分结果 ${resultEntries.length} 条`);
if (problems.length) {
  console.log(`校验失败，共 ${problems.length} 处：`);
  for (const p of problems) console.log(` - ${p}`);
  process.exit(1);
}
console.log('校验通过：规则集合一致，同 (选择器, media) 组内相对顺序一致。');
console.log(`@import 顺序：${importOrder.join(' -> ')}`);
