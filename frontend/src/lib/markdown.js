/* ============================================================
   markdown.js — 极简但「不会把内容改坏」的 Markdown 渲染器
   ------------------------------------------------------------
   设计目标（按重要性排序）：
     1. 安全      —— 先转义，绝不让内容里的 HTML 生效（防 XSS）
     2. 不破坏内容 —— 代码块内部绝不做格式化替换
     3. 够用      —— 覆盖模型最常输出的语法
     4. 无依赖    —— 不引入 marked/markdown-it 等库

   ★ 核心设计：Token 占位法
     朴素做法是先替换代码块、再替换加粗。但这样有 bug：
         `a**b**c`   （反引号代码里的 **）
     会被当成加粗处理，渲染成 <code>a<b>b</b>c</code> —— 内容被改坏了。

     正确做法：
       第 1 步：把代码块/行内代码「摘出来」换成占位符 ⟦0⟧ ⟦1⟧
       第 2 步：对剩下的正文做加粗/斜体/链接等替换
       第 3 步：把占位符换回真正的 <code>/<pre>
     这样代码内容全程不参与正文替换，不可能被误伤。

   ★ 迁移说明（原生 → Vue）：
     本文件逻辑与迁移前逐字一致，只做了两件事：
       1. 把 IIFE 改成 ES module 的命名导出；
       2. 导出 render() 与 esc() 两个函数。
     之所以不改写它，是因为它有三处踩过坑才写对的地方
     （占位符、先转义后解析、反引号优先级），重写一遍只会引入新 bug。
   ============================================================ */

/** HTML 转义 —— 所有内容进入 DOM 前都必须过这一关 */
export function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * 行内语法处理。输入必须已经过 esc()。
 */
function inline(s) {
  // 行内代码 `x` —— 它的内容已在上层被抽成占位符，这里不会再碰到
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  // 加粗：**x** 或 __x__
  s = s.replace(/\*\*(?!\s)([\s\S]+?)(?<!\s)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/__(?!\s)([\s\S]+?)(?<!\s)__/g, '<strong>$1</strong>');
  // 斜体：*x* 或 _x_（要求两侧不是空白，避免误伤 3*4*5 这类算式）
  s = s.replace(/(^|[^*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)/g, '$1<em>$2</em>');
  // 删除线
  s = s.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
  // 链接 [文字](url) —— 只允许 http/https，防 javascript: 伪协议
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  // 裸 URL（简单处理，避免重复匹配已在 href 里的）
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
    '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  return s;
}

/** 按 Markdown 表格分隔行判断是不是表格 */
function isTableSep(line) {
  return /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(line) && line.includes('-');
}

function splitRow(line) {
  return line.replace(/^\s*\|/, '').replace(/\|\s*$/, '')
             .split('|').map((c) => c.trim());
}

/**
 * 主渲染函数。返回可安全交给 v-html 的 HTML 字符串。
 */
export function render(src) {
  if (!src) return '';

  const tokens = [];

  /* ---------- 第 0 步：先在原始文本上做整体转义 ----------
     注意顺序：先转义，再做任何解析。
     这样后续所有替换操作面对的都是「已无害」的文本。 */
  let text = esc(src);

  /* ---------- 第 1 步：抽出代码块（``` 或 ~~~） ---------- */
  text = text.replace(
    /^[ \t]*(?:```|~~~)([^\n`]*)\n([\s\S]*?)^[ \t]*(?:```|~~~)[ \t]*$/gm,
    (_m, lang, body) => {
      const i = tokens.length;
      const cls = lang.trim() ? ` class="lang-${esc(lang.trim())}"` : '';
      // body 已在第 0 步整体转义过，直接用
      tokens.push(`<pre class="code-block"><code${cls}>${body.replace(/\n$/, '')}</code></pre>`);
      return `\uE000${i}\uE001`;
    }
  );

  /* ---------- 第 2 步：抽出行内代码 ---------- */
  text = text.replace(/`([^`\n]+)`/g, (_m, body) => {
    const i = tokens.length;
    tokens.push(`<code>${body}</code>`);
    return `\uE000${i}\uE001`;
  });

  /* ---------- 第 3 步：正文块级解析 ---------- */
  const lines = text.split('\n');
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // 空行
    if (/^\s*$/.test(line)) { i++; continue; }

    // 占位符独占一行 = 代码块
    if (/^\uE000\d+\uE001$/.test(line.trim())) {
      out.push(line.trim());
      i++;
      continue;
    }

    // 标题 # ## ### （用 div 而非 h1-h3，避免撑破聊天气泡）
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const lvl = Math.min(h[1].length, 4);
      out.push(`<div class="md-h md-h${lvl}">${inline(h[2])}</div>`);
      i++;
      continue;
    }

    // 引用 >
    if (/^\s*&gt;\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*&gt;\s?/, ''));
        i++;
      }
      out.push(`<blockquote>${inline(buf.join('<br>'))}</blockquote>`);
      continue;
    }

    // 表格：当前行有 | 且下一行是分隔行
    if (line.includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const head = splitRow(line);
      i += 2;                                  // 跳过表头和分隔行
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && !/^\s*$/.test(lines[i])) {
        rows.push(splitRow(lines[i]));
        i++;
      }
      const th = head.map((c) => `<th>${inline(c)}</th>`).join('');
      const tb = rows.map((r) =>
        '<tr>' + r.map((c) => `<td>${inline(c)}</td>`).join('') + '</tr>'
      ).join('');
      out.push(`<div class="md-table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${tb}</tbody></table></div>`);
      continue;
    }

    // 无序列表 - * +
    if (/^\s*[-*+]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(`<li>${inline(lines[i].replace(/^\s*[-*+]\s+/, ''))}</li>`);
        i++;
      }
      out.push(`<ul>${items.join('')}</ul>`);
      continue;
    }

    // 有序列表 1. 2)
    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        items.push(`<li>${inline(lines[i].replace(/^\s*\d+[.)]\s+/, ''))}</li>`);
        i++;
      }
      out.push(`<ol>${items.join('')}</ol>`);
      continue;
    }

    // 分隔线
    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) {
      out.push('<hr>');
      i++;
      continue;
    }

    // 普通段落：连续非空行合并
    const buf = [];
    while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^\uE000\d+\uE001$/.test(lines[i].trim())) {
      buf.push(lines[i]);
      i++;
    }
    out.push(`<p>${inline(buf.join('<br>'))}</p>`);
  }

  /* ---------- 第 4 步：还原占位符 ---------- */
  let html = out.join('\n');
  html = html.replace(/\uE000(\d+)\uE001/g, (_m, n) => tokens[Number(n)] ?? '');
  return html;
}
