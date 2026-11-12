(options) => {
  // Only visible DOM is read. No form values, cookies, page scripts or event handlers.
  const maxChars = options.max_chars || 12000;
  const truncation = {text: false, sections: false, tables: false, links: false, scan: false};
  let budget = Math.floor(maxChars * .65), visited = 0, blocks = 0, clips = 0, stopped = false;
  const LIMIT = 6000;
  const TABLE = 'table,[role="table"],[role="grid"]';
  const ROW = 'tr,[role="row"]';
  const CELL = 'th,td,[role="cell"],[role="gridcell"],[role="columnheader"],[role="rowheader"]';
  const BLOCKED = 'script,style,noscript,template,svg,canvas,iframe,object,embed,form,input,textarea,select,[hidden],[inert],[aria-hidden="true"]';
  const compact = raw => String(raw || '').replace(/\s+/g, ' ').trim();
  const clip = (value, size) => [...String(value || '')].slice(0, size).join('');
  const take = (raw, limit = 1600) => {
    const text = compact(raw), cap = Math.max(0, Math.min(limit, budget));
    const value = clip(text, cap);
    if (value.length < text.length) { truncation.text = true; clips++; }
    budget -= value.length;
    return value;
  };
  const visit = () => { if (++visited > LIMIT) { truncation.scan = true; return false; } return true; };
  const visible = el => !el.matches(BLOCKED) && el.getClientRects().length > 0
    && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
  const selector = el => {
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) {
      const value = '#' + CSS.escape(el.id); return value.length <= 512 ? value : '';
    }
    const parts = [];
    for (let node = el; node && parts.length < 6; node = node.parentElement) {
      let piece = node.tagName.toLowerCase();
      if (node.id && document.querySelectorAll('#' + CSS.escape(node.id)).length === 1) {
        parts.unshift('#' + CSS.escape(node.id)); break;
      }
      if (node.parentElement) {
        const peers = [...node.parentElement.children].filter(sibling => sibling.tagName === node.tagName);
        if (peers.length > 1) piece += ':nth-of-type(' + (peers.indexOf(node) + 1) + ')';
      }
      parts.unshift(piece);
    }
    const value = parts.join(' > '); return value.length <= 512 ? value : '';
  };
  const source = el => ({tag: el.tagName.toLowerCase(), id: clip(el.id, 100),
    classes: [...el.classList].slice(0, 3).map(value => clip(value, 60)),
    role: clip(el.getAttribute('role'), 60), selector: selector(el)});
  let scope;
  if (options.selector) {
    try {
      if (options.selector.startsWith('/') || options.selector.startsWith('(')) {
        const matches = document.evaluate(options.selector, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        if (matches.snapshotLength !== 1) return {error: matches.snapshotLength ? 'selector_ambiguous' : 'selector_missing', matches: matches.snapshotLength};
        scope = matches.snapshotItem(0);
      } else {
        const matches = document.querySelectorAll(options.selector);
        if (matches.length !== 1) return {error: matches.length ? 'selector_ambiguous' : 'selector_missing', matches: matches.length};
        scope = matches[0];
      }
      if (!scope || scope.nodeType !== Node.ELEMENT_NODE) return {error: 'invalid_selector'};
    } catch { return {error: 'invalid_selector'}; }
  } else scope = document.querySelector('main,[role="main"],article') || document.body;
  if (!scope || scope.closest(BLOCKED) || !visible(scope)) return {error: 'selector_missing', matches: 0};
  const result = {schema_version: 1, page: {url: location.href, title: take(document.title, 300),
    language: clip(document.documentElement.lang, 30)},
    selection: {selector: options.selector || '', source: source(scope)},
    sections: [], tables: [], links: [], truncation,
    limits: {max_chars: maxChars, max_sections: 24, max_tables: 4, max_rows_per_table: 30,
      max_columns: 12, max_links: 32, max_scanned_nodes: LIMIT,
      content: options.selector ? 'selected_visible_dom' : 'visible_main_dom',
      excludes: ['form_values', 'frames', 'shadow_dom']}};
  const root = {id: 'section-0', parent_id: null, level: 0, heading: result.page.title,
    source: source(scope), blocks: []};
  result.sections.push(root);
  const stack = [root], seenLinks = new Set();
  const current = () => stack[stack.length - 1];
  const link = (el, section) => {
    if (el.tagName !== 'A') return;
    let url;
    try { url = new URL(el.getAttribute('href') || '', location.href); } catch { return; }
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return;
    if (url.href.length > 4096) { truncation.links = true; return; }
    const label = clip(compact(textOf(el, section, null, 0, false)), 180), key = section.id + '\n' + url.href + '\n' + label;
    if (seenLinks.has(key)) return;
    seenLinks.add(key);
    if (result.links.length >= 32) { truncation.links = true; return; }
    result.links.push({id: 'link-' + (result.links.length + 1), section_id: section.id,
      label: take(label, 180), url: url.href, source: source(el)});
  };
  const textOf = (el, section, ownerTable = null, depth = 0, collectLinks = true) => {
    if (depth > 24 || !visit()) { truncation.scan = true; return ''; }
    if (el.nodeType === Node.TEXT_NODE) {
      if (el.data.length > 8000) { truncation.text = true; clips++; }
      return clip(el.data, 8000);
    }
    if (el.nodeType !== Node.ELEMENT_NODE || !visible(el)) return '';
    if (ownerTable && el.matches(TABLE) && el !== ownerTable) return '';
    if (collectLinks) link(el, section);
    const parts = [];
    for (const child of el.childNodes) {
      parts.push(textOf(child, section, ownerTable, depth + 1, collectLinks));
      if (truncation.scan || parts.join('').length > 8000) { truncation.text = true; break; }
    }
    return parts.join(el.matches('ul,ol,dl') ? '\n' : ' ');
  };
  const addBlock = (el, kind, text) => {
    if (!compact(text)) return;
    if (blocks >= 96 || budget <= 0) { truncation.text = true; return; }
    current().blocks.push({id: 'block-' + (++blocks), type: kind, text: take(text, kind === 'code' ? 4000 : 1600), source: source(el)});
  };
  const table = el => {
    if (result.tables.length >= 4 || budget < 100) { truncation.tables = true; return; }
    const previousClips = clips, section = current(), item = {id: 'table-' + (result.tables.length + 1), section_id: section.id,
      caption: take(el.querySelector('caption')?.innerText || el.getAttribute('aria-label') || '', 180),
      columns: [], rows: [], row_count: 0, partial: !!el.querySelector(TABLE), source: source(el)};
    const rows = el.querySelectorAll(ROW);
    for (const row of rows) {
      if (!visit()) { item.partial = true; break; }
      if (row.closest(TABLE) !== el || !visible(row)) continue;
      const cells = [...row.querySelectorAll(CELL)].filter(cell => cell.closest(ROW) === row && cell.closest(TABLE) === el && visible(cell));
      const header = item.row_count === 0 && item.columns.length === 0 && cells.length > 0
        && (row.closest('thead') || cells.every(cell => cell.getAttribute('role') === 'columnheader'
          || cell.tagName === 'TH' && cell.getAttribute('scope') !== 'row'));
      if (!header) item.row_count++;
      if (item.rows.length >= 30 || budget < 64) { item.partial = true; continue; }
      if (cells.length > 12) item.partial = true;
      const values = cells.slice(0, 12).map(cell => take(textOf(cell, section, el), 240));
      if (header) item.columns = values;
      else item.rows.push(values);
    }
    if (clips > previousClips) item.partial = true;
    if (item.partial) truncation.tables = true;
    result.tables.push(item);
    section.blocks.push({id: 'block-' + (++blocks), type: 'table', ref: item.id});
  };
  const walk = (el, depth = 0) => {
    if (stopped) return;
    if (depth > 32 || !visit()) { truncation.scan = true; return; }
    if (el.nodeType === Node.TEXT_NODE) {
      const text = compact(el.data);
      if (text) addBlock(el.parentElement, 'text', text);
      return;
    }
    if (el.nodeType !== Node.ELEMENT_NODE || !visible(el)) return;
    if (!options.selector && el !== scope && el.matches('nav,aside,footer,[role="navigation"],[role="complementary"]')) return;
    if (/^H[1-6]$/.test(el.tagName)) {
      if (result.sections.length >= 24) { truncation.sections = true; stopped = true; return; }
      const level = Number(el.tagName[1]);
      while (stack.length > 1 && current().level >= level) stack.pop();
      const owner = el.closest('section,article,[role="region"]');
      const item = {id: 'section-' + result.sections.length, parent_id: current().id, level,
        heading: take(textOf(el, current()), 240), source: source(owner && scope.contains(owner) ? owner : el), blocks: []};
      result.sections.push(item); stack.push(item); return;
    }
    if (el.matches(TABLE)) { table(el); return; }
    if (el.matches('p,pre,blockquote,ul,ol,dl,button,time,a,span,strong,em,b,i,small,label')) {
      const type = el.matches('ul,ol,dl') ? 'list' : el.tagName === 'PRE' ? 'code'
        : el.tagName === 'BLOCKQUOTE' ? 'quote' : 'paragraph';
      addBlock(el, type, textOf(el, current())); return;
    }
    for (const child of el.childNodes) {
      walk(child, depth + 1);
      if (truncation.scan || stopped) break;
    }
  };
  walk(scope);
  if (options.query) {
    const terms = compact(options.query).toLowerCase().split(/[\s,.;:!?]+/).filter(term => term.length > 2).slice(0, 20);
    const matches = result.sections.map(section => {
      const text = (section.heading + ' ' + section.blocks.map(block => block.text || '').join(' ') + ' '
        + result.tables.filter(table => table.section_id === section.id).map(table => JSON.stringify(table)).join(' ')).toLowerCase();
      return {id: section.id, score: terms.filter(term => text.includes(term)).length};
    }).filter(item => item.score).sort((a, b) => b.score - a.score);
    result.focus = {query: options.query, matching_section_ids: matches.map(item => item.id), method: 'lexical_hint'};
  }
  // Bound the serialized structure as well as its text. Never silently return a
  // full-looking partial table or a dangling reference to an omitted section.
  while (JSON.stringify(result).length > maxChars) {
    truncation.text = true;
    const section = [...result.sections].reverse().find(item => item.blocks.some(block => block.type !== 'table'));
    if (section) { section.blocks.splice(section.blocks.map(block => block.type !== 'table').lastIndexOf(true), 1); continue; }
    const tab = [...result.tables].reverse().find(item => item.rows.length > 0);
    if (tab) { tab.rows.pop(); tab.partial = true; truncation.tables = true; continue; }
    if (result.links.length) { result.links.pop(); truncation.links = true; continue; }
    if (result.tables.length) {
      const removed = result.tables.pop(); truncation.tables = true;
      for (const item of result.sections) item.blocks = item.blocks.filter(block => block.ref !== removed.id);
      continue;
    }
    if (result.sections.length > 1) {
      const removed = result.sections.pop(); truncation.sections = true;
      if (result.focus) result.focus.matching_section_ids = result.focus.matching_section_ids.filter(id => id !== removed.id);
      continue;
    }
    // Metadata alone can exceed a tiny caller budget (for example a long query).
    if (result.focus) { delete result.focus; continue; }
    break;
  }
  return result;
}
