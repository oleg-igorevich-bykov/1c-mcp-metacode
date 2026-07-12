/*!
  Minimal Markdown renderer (safe-by-default) for 1C Buddy Chat UI.
  - Escapes HTML first to prevent XSS
  - Supports:
    * Code fences ```lang ... ```
    * Mermaid diagrams ```mermaid ... ```
    * Inline code `code`
    * Headings #, ##, ###, ####, #####, ######
    * Links [text](https://...)
    * Autolinks (bare URLs)
    * Bold **text** and __text__
    * Italic _text_
    * Strikethrough ~~text~~
    * Unordered lists (* item, - item) with nesting
    * Ordered lists (1. item, 2. item) with nesting
    * Blockquotes (> text)
    * Horizontal rules (---, ***, ___)
    * Tables (| Header | Header |)
    * Paragraphs and line breaks
  Note: Streaming re-renders full accumulated text each time.
*/
(function () {
  function escapeHTML(str) {
    return (str || "")
      .replace(/&/g, "&" + "amp;")
      .replace(/</g, "&" + "lt;")
      .replace(/>/g, "&" + "gt;")
      .replace(/"/g, "&" + "quot;")
      .replace(/'/g, "&" + "#39;");
  }

  function tryParseMarkdownLink(text, startIndex) {
    if (!text || text[startIndex] !== "[") return null;

    let labelEnd = startIndex + 1;
    let bracketDepth = 1;
    while (labelEnd < text.length) {
      const ch = text[labelEnd];
      if (ch === "\\") {
        labelEnd += 2;
        continue;
      }
      if (ch === "[") {
        bracketDepth++;
      } else if (ch === "]") {
        bracketDepth--;
        if (bracketDepth === 0) break;
      }
      labelEnd++;
    }

    if (bracketDepth !== 0 || text[labelEnd + 1] !== "(") {
      return null;
    }

    let urlEnd = labelEnd + 2;
    let parenDepth = 1;
    while (urlEnd < text.length) {
      const ch = text[urlEnd];
      if (ch === "\\") {
        urlEnd += 2;
        continue;
      }
      if (ch === "(") {
        parenDepth++;
      } else if (ch === ")") {
        parenDepth--;
        if (parenDepth === 0) break;
      }
      urlEnd++;
    }

    if (parenDepth !== 0) {
      return null;
    }

    const label = text.slice(startIndex + 1, labelEnd);
    const url = text.slice(labelEnd + 2, urlEnd).trim();
    if (!/^(https?:\/\/|metacode:\/\/)[^\s]+$/i.test(url)) {
      return null;
    }

    const isInternal = /^metacode:\/\//i.test(url);
    return {
      endIndex: urlEnd + 1,
      html: isInternal
        ? '<a href="' + url + '" data-console-link="1">' + label + '</a>'
        : '<a href="' + url + '" target="_blank" rel="noopener noreferrer nofollow">' + label + '</a>'
    };
  }

  function renderMarkdownLinks(text) {
    if (!text) return "";

    let result = "";
    let cursor = 0;

    while (cursor < text.length) {
      const startIndex = text.indexOf("[", cursor);
      if (startIndex === -1) {
        result += text.slice(cursor);
        break;
      }

      result += text.slice(cursor, startIndex);
      const parsed = tryParseMarkdownLink(text, startIndex);
      if (!parsed) {
        result += text[startIndex];
        cursor = startIndex + 1;
        continue;
      }

      result += parsed.html;
      cursor = parsed.endIndex;
    }

    return result;
  }

  function render(md) {
    if (!md) return "";

    // 0) Normalize various 1C code block formats to ```bsl
    // First, remove completely empty code blocks (``` immediately followed by ```)
    md = md.replace(/```[ \t]*\r?\n[ \t]*```/g, "");

    // Variant 0: ```1С or ```1с -> ```1c (normalize Russian С/с to Latin c)
    md = md.replace(/```1[Сс]\r?\n/g, "```1c\n");
    // Variant 1: ```<code> (HTML-like tag on same line)
    md = md.replace(/```<code>\r?\n/g, "```bsl\n");
    // Variant 2a: ```\n    <code>\n    ``` (empty block with standalone <code> line - just remove it)
    md = md.replace(/```[ \t]*\r?\n[ \t]*<code>[ \t]*\r?\n[ \t]*```/g, "");
    // Variant 2b: ```\n<code>...\n</code>\n``` (backticks containing <code> blocks with content)
    md = md.replace(/```[ \t]*\r?\n[ \t]*<code>[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*<\/code>[ \t]*\r?\n[ \t]*```/g, function(_m, code) {
      return "```bsl\n" + code.trim() + "\n```\n\n";
    });
    // Variant 2c: ```\n<code>\nКОД\n``` (unclosed <code> tag inside ``` block - most common from upstream)
    md = md.replace(/```[ \t]*\r?\n[ \t]*<code>[ \t]*\r?\n([\s\S]*?)```/g, function(_m, code) {
      return "```bsl\n" + code.trim() + "\n```";
    });
    // Variant 3: ```code (plain "code" as language name)
    md = md.replace(/```code\r?\n/g, "```bsl\n");
    // Variant 4a: Closed <code>...</code> blocks (standalone, not inside backticks)
    md = md.replace(/<code>[ \t]*\r?\n([\s\S]*?)\r?\n?<\/code>/g, function(_m, code) {
      return "```bsl\n" + code.trim() + "\n```\n\n";
    });

    // Variant 4b: Unclosed <code> blocks (upstream sometimes sends without closing tag)
    // Match <code>\n followed by content until: newline before block element, double newline, or end of text
    md = md.replace(/<code>[ \t]*\r?\n([\s\S]*?)(?=\r?\n[ \t]*(?:[#\*\-]|\d+\.|>)|(?:\r?\n){2,}|$)/g, function(_m, code) {
      return "```bsl\n" + code.trimEnd() + "\n```\n\n";
    });

    // 0.5) Fix malformed code blocks: normalize `` (two backticks) to ``` (three backticks) for closing fence
    // Upstream sometimes sends code blocks closed with `` instead of ```
    // Replace `` with ``` only when NOT preceded by ` AND NOT followed by `
    // This ensures we only fix truly malformed `` closings, not parts of valid ```
    md = md.replace(/(?<!`)``(?!`)/g, '```');

    // 1) Extract Mermaid diagrams FIRST (before code blocks)
    // NOTE: We don't escape HTML here because mermaid code needs to be processed as-is
    // The code is safe because it's stored as textContent in the div, not as innerHTML
    const mermaidBlocks = [];

    // Support format: ```\nmermaid\n...\n```
    md = md.replace(/```\r?\nmermaid\r?\n([\s\S]*?)```/g, function (_m, code) {
      // Use the same processing function for this format
      return processMermaidBlock(code.trim());
    });

    // Support standard format: ```mermaid\n...\n```
    md = md.replace(/```mermaid\r?\n([\s\S]*?)```/g, function (_m, code) {
      return processMermaidBlock(code.trim());
    });

    function processMermaidBlock(code) {
      // Fix brackets and parentheses in node text by replacing with Unicode lookalikes
      // This prevents Mermaid parser errors when node text contains special chars like "Массив[j]" or "Метод()"
      // Strategy: Process each line and replace nested brackets/parens inside node definitions
      let fixedCode = code.trim();

      // First pass: fix common typos like {text] instead of {text}
      // Only match cases where there are NO nested brackets inside
      fixedCode = fixedCode.replace(/(\w+)\{([^{}\[\]()]*?)\]/g, '$1{$2}');
      fixedCode = fixedCode.replace(/(\w+)\[([^{}\[\]()]*?)\}/g, '$1[$2]');
      fixedCode = fixedCode.replace(/(\w+)\(([^{}\[\]()]*?)\]/g, '$1($2)');

      // Fix parentheses in subgraph names by removing them
      // Example: "subgraph Внешний цикл (i)" -> "subgraph Внешний цикл - i"
      // Mermaid doesn't support special characters in subgraph names, so we replace (text) with - text
      fixedCode = fixedCode.replace(/(subgraph\s+[^\r\n(]+)\(([^)]+)\)/g, '$1- $2');

      // Process line by line with bracket depth tracking
      fixedCode = fixedCode.split('\n').map(line => {
        let result = '';
        let i = 0;

        while (i < line.length) {
          // Check if we're at a node definition (word followed by bracket/brace/paren)
          const nodeMatch = line.slice(i).match(/^(\w+)([\[\{\(])/);

          if (nodeMatch) {
            const nodeId = nodeMatch[1];
            const openChar = nodeMatch[2];
            const closeChar = openChar === '[' ? ']' : openChar === '{' ? '}' : ')';

            result += nodeId;
            i += nodeId.length;

            // Find the matching closing bracket
            let depth = 0;
            let nodeText = '';

            while (i < line.length) {
              const char = line[i];

              if (char === openChar) {
                depth++;
                if (depth === 1) {
                  // First opening bracket - keep it
                  nodeText += char;
                } else {
                  // Nested opening bracket - replace it
                  nodeText += (openChar === '[' ? '⦋' : openChar === '{' ? '{' : '⦅');
                }
              } else if (char === closeChar) {
                depth--;
                if (depth === 0) {
                  // Matching closing bracket - keep it
                  nodeText += char;
                  i++;
                  break;
                } else {
                  // Nested closing bracket - replace it
                  nodeText += (closeChar === ']' ? '⦌' : closeChar === '}' ? '}' : '⦆');
                }
              } else {
                // Check for other types of brackets inside BEFORE adding char
                if (openChar === '[' && char === '(') {
                  // Inside rectangular node, replace (...)
                  let parenDepth = 1;
                  i++;
                  let parenContent = '';
                  while (i < line.length && parenDepth > 0) {
                    if (line[i] === '(') parenDepth++;
                    else if (line[i] === ')') parenDepth--;
                    if (parenDepth > 0) parenContent += line[i];
                    i++;
                  }
                  nodeText += '⦅' + parenContent + '⦆';
                  i--;
                } else if (openChar === '{' && (char === '[' || char === '(')) {
                  // Inside diamond node, replace [...] or (...)
                  const innerOpen = char;
                  const innerClose = char === '[' ? ']' : ')';
                  let innerDepth = 1;
                  i++;
                  let innerContent = '';
                  while (i < line.length && innerDepth > 0) {
                    if (line[i] === innerOpen) innerDepth++;
                    else if (line[i] === innerClose) innerDepth--;
                    if (innerDepth > 0) innerContent += line[i];
                    i++;
                  }
                  nodeText += (innerOpen === '[' ? '⦋' : '⦅') + innerContent + (innerClose === ']' ? '⦌' : '⦆');
                  i--;
                } else {
                  // Normal character, just add it
                  nodeText += char;
                }
              }
              i++;
            }

            // Replace quotes with DOUBLE PRIME only inside node text
            // This prevents Mermaid parser from treating them as string delimiters inside nodes
            // U+2033 DOUBLE PRIME looks like " but is not treated as a quote by Mermaid
            // Edge labels (text between -- and -->) are NOT affected by this replacement
            result += nodeText.replace(/"/g, '″');
          } else {
            result += line[i];
            i++;
          }
        }

        return result;
      }).join('\n');

      // Store raw code in a data attribute to preserve special characters
      const safeCode = fixedCode.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      // Add control buttons for Mermaid diagrams: fullscreen, copy
      const html =
        '<div class="mermaid-wrapper" style="position:relative;" data-zoom="1">' +
          '<div class="mermaid-controls" style="position:absolute;top:8px;right:8px;display:flex;gap:4px;z-index:1;">' +
            '<button type="button" class="mermaid-fullscreen-btn" title="Развернуть на весь экран" aria-label="Развернуть" data-fullscreen ' +
              'style="padding:4px 8px;border-radius:6px;border:1px solid rgba(255,255,255,0.18);' +
                     'background:rgba(255,255,255,0.06);color:inherit;cursor:pointer;font-size:14px;line-height:1;opacity:.85;">⛶</button>' +
          '</div>' +
          '<div class="mermaid-controls-bottom" style="position:absolute;bottom:8px;right:8px;z-index:1;display:flex;gap:4px;">' +
            '<button type="button" class="mermaid-save-btn" title="Сохранить как PNG" aria-label="Сохранить" data-save-mermaid ' +
              'style="padding:4px 8px;border-radius:6px;border:1px solid rgba(255,255,255,0.18);' +
                     'background:rgba(255,255,255,0.06);color:inherit;cursor:pointer;font-size:12px;line-height:1;opacity:.85;">🖫</button>' +
            '<button type="button" class="mermaid-copy-btn" title="Скопировать" aria-label="Скопировать" data-copy-mermaid ' +
              'style="padding:4px 8px;border-radius:6px;border:1px solid rgba(255,255,255,0.18);' +
                     'background:rgba(255,255,255,0.06);color:inherit;cursor:pointer;font-size:12px;line-height:1;opacity:.85;">⧉</button>' +
          '</div>' +
          '<div class="mermaid-content" style="transform-origin:center center;transition:transform 0.2s ease;width:100%;">' +
            '<div class="mermaid" data-mermaid-code="' + safeCode.replace(/"/g, '&quot;') + '"></div>' +
          '</div>' +
        '</div>';
      const token = "§§MERMAID" + mermaidBlocks.length + "§§";
      mermaidBlocks.push(html);
      return token;
    }

    // 2) Extract fenced code blocks and replace with placeholders
    const codeBlocks = [];
    md = md.replace(/```([\w+-]*)\r?\n([\s\S]*?)```/g, function (_m, lang, code) {
      const normalizedLang = String(lang || "").toLowerCase();
      const cls = normalizedLang ? ' class="lang-' + normalizedLang + '"' : "";
      const langLabel = normalizedLang ? '<span class="code-block-lang">' + escapeHTML(normalizedLang) + '</span>' : '<span></span>';
      const html =
        '<div class="code-block-shell">' +
          '<div class="code-block-toolbar">' +
            langLabel +
            '<button type="button" class="code-copy-btn" title="Скопировать" aria-label="Скопировать" data-copy-code>⧉</button>' +
          '</div>' +
          '<pre class="code-block">' +
          '<code' + cls + '>' + escapeHTML(code) + '</code>' +
        '</pre>' +
        '</div>';
      const token = "§§CODEBLOCK" + codeBlocks.length + "§§";
      codeBlocks.push(html);
      return token;
    });

    // 1.5) Extract inline code and replace with placeholders (BEFORE escapeHTML to protect from link processing)
    const inlineCodes = [];
    md = md.replace(/`([^`]+)`/g, function (_m, code) {
      const token = "§§INLINECODE" + inlineCodes.length + "§§";
      inlineCodes.push("<code>" + escapeHTML(code) + "</code>");
      return token;
    });

    // 2) Escape remaining HTML
    md = escapeHTML(md);

    // 3) Horizontal rules (---, ***, ___)
    md = md.replace(/^(?:---+|\*\*\*+|___+)$/gm, "<hr>");

    // 4) Blockquotes (> text)
    md = md.replace(/(?:^|\n)((?:^>.*$(?:\n|$))+)/gm, function(_match, blockContent) {
      const content = blockContent.trim().split(/\n/).map(line => {
        return line.replace(/^>\s?/, '');
      }).join('<br>');
      return '\n<blockquote>' + content + '</blockquote>\n';
    });

    // 5) Headings (support up to ######)
    md = md.replace(/^###### (.*)$/gm, "<h6>$1</h6>");
    md = md.replace(/^##### (.*)$/gm, "<h5>$1</h5>");
    md = md.replace(/^#### (.*)$/gm, "<h4>$1</h4>");
    md = md.replace(/^### (.*)$/gm, "<h3>$1</h3>");
    md = md.replace(/^## (.*)$/gm, "<h2>$1</h2>");
    md = md.replace(/^# (.*)$/gm, "<h1>$1</h1>");

    // 6) Links: [text](https://...) with support for nested [] in link text
    md = renderMarkdownLinks(md);

    // 7) Autolinks: bare URLs (not already in markdown links or code)
    md = md.replace(
      /(?<!["'\(>])(https?:\/\/[^\s<]+[^\s<.,;:!?'")\]])/g,
      '<a href="$1" target="_blank" rel="noopener noreferrer nofollow">$1</a>'
    );

    // 8) Inline code already extracted at step 1.5, skip here

    // 9) Strikethrough ~~text~~
    md = md.replace(/~~([^~]+)~~/g, "<del>$1</del>");

     // 10) Bold and italic.
     // For underscore emphasis, require word boundaries around markers so
     // identifiers like mcp__tool__Name or snake_case are not parsed as markdown.
     md = md.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
     md = md.replace(/(?<![\p{L}\p{N}_])__([^_\n]+)__(?![\p{L}\p{N}_])/gu, "<strong>$1</strong>");
     md = md.replace(/(?<![\p{L}\p{N}_])_([^_\n]+)_(?![\p{L}\p{N}_])/gu, "<em>$1</em>");
     md = md.replace(/(^|[^\w*])\*([^*\n]+)\*(?!\*)/gm, "$1<em>$2</em>");

    // 11) Lists: unordered (*, -) and ordered (1., 2., etc.) with nesting support
    // Process lists before paragraph splitting
    function parseList(lines, startIndex = 0) {
      const result = [];
      let i = startIndex;

      while (i < lines.length) {
        const line = lines[i];
        const match = line.match(/^([ \t]*)([\*\-]|\d+\.)\s+(.+)$/);

        if (!match) break;

        const [, indent, , content] = match;
        const indentLevel = indent.length;

        // Check if next lines are more indented (nested)
        let j = i + 1;
        const nestedLines = [];
        while (j < lines.length) {
          const nextLine = lines[j];
          const nextMatch = nextLine.match(/^([ \t]*)([\*\-]|\d+\.)\s+/);
          if (!nextMatch) break;
          if (nextMatch[1].length <= indentLevel) break;
          nestedLines.push(nextLine);
          j++;
        }

        let itemHTML = content;
        if (nestedLines.length > 0) {
          // Determine nested list type
          const firstNestedMatch = nestedLines[0].match(/^[ \t]*([\*\-]|\d+\.)\s+/);
          const nestedIsOrdered = firstNestedMatch && /^\d+\.$/.test(firstNestedMatch[1]);
          const nestedTag = nestedIsOrdered ? 'ol' : 'ul';
          const nestedItems = parseList(nestedLines, 0);
          itemHTML += '<' + nestedTag + '>' + nestedItems + '</' + nestedTag + '>';
        }

        result.push('<li>' + itemHTML + '</li>');
        i = j;
      }

      return result.join('');
    }

    md = md.replace(/(?:^|\n)((?:[ \t]*[\*\-]\s+.+(?:\n|$))+)/g, function(_match, listContent) {
      const lines = listContent.trim().split(/\n/);
      const items = parseList(lines);
      return '\n<ul>' + items + '</ul>\n';
    });

    md = md.replace(/(?:^|\n)((?:[ \t]*\d+\.\s+.+(?:\n|$))+)/g, function(_match, listContent) {
      const lines = listContent.trim().split(/\n/);
      const items = parseList(lines);
      return '\n<ol>' + items + '</ol>\n';
    });

    // 12) Tables (GitHub Flavored Markdown style)
    // Match: header row | separator row | body rows
    md = md.replace(/(?:^|\n)(\|.+\|\n\|[\s\-:|]+\|\n(?:\|.+\|\n?)+)/gm, function(_match, tableContent) {
      const lines = tableContent.trim().split(/\n/);
      if (lines.length < 2) return tableContent;

      // Parse header
      const headerCells = lines[0].split('|').slice(1, -1).map(cell => cell.trim());

      // Parse separator (contains alignment info)
      const separatorCells = lines[1].split('|').slice(1, -1);
      const alignments = separatorCells.map(cell => {
        const trimmed = cell.trim();
        if (trimmed.startsWith(':') && trimmed.endsWith(':')) return 'center';
        if (trimmed.endsWith(':')) return 'right';
        if (trimmed.startsWith(':')) return 'left';
        return '';
      });

      // Build header
      let tableHTML = '<table><thead><tr>';
      headerCells.forEach((cell, i) => {
        const align = alignments[i] ? ` align="${alignments[i]}"` : '';
        tableHTML += `<th${align}>${cell}</th>`;
      });
      tableHTML += '</tr></thead>';

      // Parse body rows
      tableHTML += '<tbody>';
      for (let i = 2; i < lines.length; i++) {
        const cells = lines[i].split('|').slice(1, -1).map(cell => cell.trim());
        tableHTML += '<tr>';
        cells.forEach((cell, j) => {
          const align = alignments[j] ? ` align="${alignments[j]}"` : '';
          tableHTML += `<td${align}>${cell}</td>`;
        });
        tableHTML += '</tr>';
      }
      tableHTML += '</tbody></table>';

      return '\n' + tableHTML + '\n';
    });

    // 13) Paragraphs: split by blank lines, keep block elements as-is
    const blocks = md.split(/\n{2,}/);
    md = blocks
      .map((block) => {
        const trimmed = block.trim();
        if (!trimmed) return "";
        if (/^<h[1-6]|^<pre|^<ul|^<ol|^<blockquote|^<hr|^<table|^<div class="mermaid-wrapper"|^§§MERMAID/i.test(trimmed)) return trimmed;
        return "<p>" + trimmed.replace(/\n/g, "<br>") + "</p>";
      })
      .join("\n");

    // 14) Restore code blocks
    codeBlocks.forEach((html, i) => {
      md = md.replace("§§CODEBLOCK" + i + "§§", html);
    });

    // 15) Restore Mermaid diagrams
    mermaidBlocks.forEach((html, i) => {
      md = md.replace("§§MERMAID" + i + "§§", html);
    });

    // 16) Restore inline code
    inlineCodes.forEach((html, i) => {
      md = md.replace("§§INLINECODE" + i + "§§", html);
    });

    return md;
  }

  window.Markdown = {
    render,
  };

  // One-time delegated handler for copy buttons in code blocks and Mermaid diagrams.
  function installCopyHandlerOnce() {
    if (window.__onec_copy_btn_installed) return;
    window.__onec_copy_btn_installed = true;

    document.addEventListener("click", async (e) => {
      // Handle code block copy button
      const codeBtn = e.target.closest("[data-copy-code]");
      if (codeBtn) {
        const shell = codeBtn.closest(".code-block-shell");
        const pre = shell ? shell.querySelector("pre") : codeBtn.closest("pre");
        const code = pre ? pre.querySelector("code") : null;
        const text = code ? (code.innerText || code.textContent || "") : "";
        if (!text) return;

        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
          } else {
            // Fallback for older browsers
            const area = document.createElement("textarea");
            area.value = text;
            area.style.position = "fixed";
            area.style.opacity = "0";
            document.body.appendChild(area);
            area.focus();
            area.select();
            document.execCommand("copy");
            document.body.removeChild(area);
          }
          const orig = codeBtn.textContent;
          codeBtn.textContent = "✓";
          codeBtn.style.opacity = "1";
          codeBtn.title = "Скопировано";
          setTimeout(() => {
            codeBtn.textContent = orig || "⧉";
            codeBtn.title = "Скопировать";
          }, 1200);
        } catch (_) {
          // no-op on failure
        }
        return;
      }

      // Handle Mermaid zoom in button
      const zoomInBtn = e.target.closest("[data-zoom-in]");
      if (zoomInBtn) {
        const wrapper = zoomInBtn.closest(".mermaid-wrapper");
        if (wrapper) {
          const currentZoom = parseFloat(wrapper.getAttribute("data-zoom") || "1");
          const newZoom = Math.min(currentZoom + 0.25, 10); // Max 10x zoom (1000%)
          wrapper.setAttribute("data-zoom", newZoom);
          const content = wrapper.querySelector(".mermaid-content");
          if (content) {
            content.style.transform = `scale(${newZoom})`;
          }
        }
        return;
      }

      // Handle Mermaid zoom out button
      const zoomOutBtn = e.target.closest("[data-zoom-out]");
      if (zoomOutBtn) {
        const wrapper = zoomOutBtn.closest(".mermaid-wrapper");
        if (wrapper) {
          const currentZoom = parseFloat(wrapper.getAttribute("data-zoom") || "1");
          const newZoom = Math.max(currentZoom - 0.25, 0.1); // Min 0.1x zoom (10%)
          wrapper.setAttribute("data-zoom", newZoom);
          const content = wrapper.querySelector(".mermaid-content");
          if (content) {
            content.style.transform = `scale(${newZoom})`;
          }
        }
        return;
      }

      // Handle Mermaid fullscreen button
      const fullscreenBtn = e.target.closest("[data-fullscreen]");
      if (fullscreenBtn) {
        const wrapper = fullscreenBtn.closest(".mermaid-wrapper");
        const mermaidDiv = wrapper ? wrapper.querySelector(".mermaid") : null;
        const mermaidCode = mermaidDiv ? mermaidDiv.getAttribute("data-mermaid-code") : "";
        if (!mermaidCode) return;

        // Trigger fullscreen event (will be handled in app.js)
        const event = new CustomEvent("mermaid-fullscreen", {
          detail: { code: mermaidCode, svg: mermaidDiv.innerHTML }
        });
        document.dispatchEvent(event);
        return;
      }

      // Handle Mermaid diagram save button
      const saveBtn = e.target.closest("[data-save-mermaid]");
      if (saveBtn) {
        const wrapper = saveBtn.closest(".mermaid-wrapper");
        const mermaidDiv = wrapper ? wrapper.querySelector(".mermaid") : null;

        if (mermaidDiv && window.domtoimage) {
          try {
            const orig = saveBtn.textContent;
            saveBtn.textContent = "⏳";
            saveBtn.title = "Сохранение...";
            saveBtn.style.opacity = "1";

            // Конвертируем в PNG с увеличенным разрешением для лучшего качества
            const blob = await domtoimage.toBlob(mermaidDiv, {
              quality: 1,
              scale: 2,  // 2x для лучшего качества
              style: {
                transform: 'scale(1)',  // Сбросить любые трансформации
                transformOrigin: 'top left'
              }
            });

            // Скачиваем файл
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = `mermaid-diagram-${Date.now()}.png`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(a.href);

            // Визуальный фидбэк
            saveBtn.textContent = "✓";
            saveBtn.title = "Сохранено";
            setTimeout(() => {
              saveBtn.textContent = orig || "🖫";
              saveBtn.title = "Сохранить как PNG";
              saveBtn.style.opacity = "0.85";
            }, 1200);
          } catch (err) {
            console.error('Ошибка сохранения диаграммы:', err);
            saveBtn.textContent = "❌";
            saveBtn.title = "Ошибка сохранения";
            setTimeout(() => {
              saveBtn.textContent = "🖫";
              saveBtn.title = "Сохранить как PNG";
              saveBtn.style.opacity = "0.85";
            }, 1200);
          }
        }
        return;
      }

      // Handle Mermaid diagram copy button
      const mermaidBtn = e.target.closest("[data-copy-mermaid]");
      if (mermaidBtn) {
        const wrapper = mermaidBtn.closest(".mermaid-wrapper");
        const mermaidDiv = wrapper ? wrapper.querySelector(".mermaid") : null;
        const mermaidCode = mermaidDiv ? mermaidDiv.getAttribute("data-mermaid-code") : "";
        if (!mermaidCode) return;

        // Decode HTML entities to get original code
        const textarea = document.createElement("textarea");
        textarea.innerHTML = mermaidCode;
        const text = textarea.value;

        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
          } else {
            // Fallback for older browsers
            const area = document.createElement("textarea");
            area.value = text;
            area.style.position = "fixed";
            area.style.opacity = "0";
            document.body.appendChild(area);
            area.focus();
            area.select();
            document.execCommand("copy");
            document.body.removeChild(area);
          }
          const orig = mermaidBtn.textContent;
          mermaidBtn.textContent = "✓";
          mermaidBtn.style.opacity = "1";
          mermaidBtn.title = "Скопировано";
          setTimeout(() => {
            mermaidBtn.textContent = orig || "⧉";
            mermaidBtn.title = "Скопировать";
          }, 1200);
        } catch (_) {
          // no-op on failure
        }
        return;
      }

      // Handle Mermaid diagram fix button
      const fixBtn = e.target.closest("[data-fix-mermaid]");
      if (fixBtn) {
        const wrapper = fixBtn.closest(".mermaid-wrapper");
        const mermaidDiv = wrapper ? wrapper.querySelector(".mermaid") : null;
        if (!mermaidDiv) return;

        const mermaidCode = mermaidDiv.getAttribute("data-mermaid-code") || "";
        const errorMessage = mermaidDiv.getAttribute("data-error-message") || "Неизвестная ошибка";

        // Decode HTML entities to get original code
        const textarea = document.createElement("textarea");
        textarea.innerHTML = mermaidCode;
        const originalCode = textarea.value;

        // Compose message to send to assistant
        // Use 'text' instead of 'mermaid' to prevent rendering the broken diagram
        const message = `Исправь эту Mermaid диаграмму. Ошибка: ${errorMessage}\n\n\`\`\`text\n${originalCode}\n\`\`\``;

        // Get message input and send form
        const input = document.getElementById("message-input");
        const form = document.getElementById("send-form");

        if (input && form) {
          // Set the message in the input
          input.value = message;

          // Auto-resize textarea
          input.style.height = 'auto';
          input.style.height = Math.min(input.scrollHeight, 300) + 'px';

          // Visual feedback on button
          const orig = fixBtn.textContent;
          fixBtn.textContent = "✓";
          fixBtn.style.opacity = "1";
          setTimeout(() => {
            fixBtn.textContent = orig || "🔧";
          }, 1200);

          // Set flag to skip showing user message in chat
          window.__skipUserMessage = true;

          // Submit the form automatically
          if (typeof form.requestSubmit === "function") {
            form.requestSubmit();
          } else {
            // Fallback for older browsers
            form.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
          }
        }
        return;
      }
    });
  }

  installCopyHandlerOnce();
})();
