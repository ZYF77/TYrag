/** Keep in sync with Gateway citation_select.py and citation-text-cases.json. */
export const CITATION_MARKER_PATTERN = /\[(?:ID\s*[:：]\s*([0-9\u0660-\u0669\u06f0-\u06f9]+)|([0-9\u0660-\u0669\u06f0-\u06f9]+))\]/gi;

function balancedEnd(text: string, start: number, opening: string, closing: string): number {
  let depth = 0;
  for (let i = start; i < text.length; i++) {
    if (text[i] === '\\') { i++; continue; }
    if (text[i] === opening) depth++;
    else if (text[i] === closing && --depth === 0) return i + 1;
  }
  return start;
}

function protectedRanges(text: string): [number, number][] {
  const blocks: [number, number][] = [];
  const labels = new Set<string>();
  const label = (value: string) => value.trim().toLowerCase().replace(/\s+/g, ' ');
  let fence: string | undefined;
  let fenceStart = 0;
  let offset = 0;
  for (const line of text.match(/[^\n]*\n|[^\n]+$/g) ?? []) {
    const logical = line.replace(/^(?: {0,3}>[ \t]?)+/, '').replace(/^ {0,3}(?:[-+*]|[0-9]+[.)])[ \t]+/, '');
    const opening = /^ {0,3}(`{3,}|~{3,})/.exec(logical);
    const definition = /^ {0,3}\[([^\]\n]+)\]:/.exec(logical);
    if (fence !== undefined) {
      if (new RegExp(`^ {0,3}${fence[0]}{${fence.length},}[ \\t\\r\\n]*$`).test(logical)) {
        blocks.push([fenceStart, offset + line.length]);
        fence = undefined;
      }
    } else if (opening) {
      fence = opening[1]; fenceStart = offset;
    } else if (/^(?: {4}|\t)/.test(logical)) {
      blocks.push([offset, offset + line.length]);
    } else if (definition) {
      labels.add(label(definition[1]));
      blocks.push([offset, offset + line.length]);
    }
    offset += line.length;
  }
  if (fence !== undefined) blocks.push([fenceStart, text.length]);
  const ranges: [number, number][] = [];
  let block = 0;
  let i = 0;
  while (i < text.length) {
    if (block < blocks.length && i >= blocks[block][0]) {
      ranges.push(blocks[block]); i = blocks[block][1]; block++; continue;
    }
    const limit = block < blocks.length ? blocks[block][0] : text.length;
    const start = i;
    if (text[i] === '\\') {
      if (text.slice(i, i + 2) === '\\[') {
        const end = /\\?\]/.exec(text.slice(i + 2, limit));
        i = end ? i + 2 + end.index + end[0].length : Math.min(i + 2, limit);
      } else i = Math.min(i + 2, limit);
      ranges.push([start, i]); continue;
    }
    if (text[i] === '`') {
      const run = /^`+/.exec(text.slice(i))![0];
      const end = new RegExp('(?<!`)' + run + '(?!`)').exec(text.slice(i + run.length, limit));
      if (end) {
        i += run.length + end.index + end[0].length;
        ranges.push([start, i]); continue;
      }
      i += run.length; continue;
    }
    if (text[i] === '<') {
      const link = /^<(?:[A-Za-z][A-Za-z0-9+.-]*:[^<>\s]*|[^<>\s]+@[^<>\s]+)>/.exec(text.slice(i, limit));
      if (link) { i += link[0].length; ranges.push([start, i]); continue; }
    }
    if (text[i] === '[' || text.slice(i, i + 2) === '![') {
      const bracket = i + (text[i] === '!' ? 1 : 0);
      const end = balancedEnd(text, bracket, '[', ']');
      if (end > bracket && end <= limit) {
        const name = label(text.slice(bracket + 1, end - 1));
        let finish = labels.has(name) ? end : start;
        if (text[end] === '(') {
          finish = balancedEnd(text, end, '(', ')');
          if (finish === end) finish = start;
        } else if (text[end] === '[') {
          const refEnd = balancedEnd(text, end, '[', ']');
          if (refEnd > end && labels.has(label(text.slice(end + 1, refEnd - 1)) || name)) finish = refEnd;
        }
        if (finish > start && finish <= limit) {
          ranges.push([start, finish]); i = finish; continue;
        }
      }
    }
    i++;
  }
  return ranges;
}

const repairs: RegExp[] = [
  /\[+\s*ID\s*:\s*([0-9\u0660-\u0669\u06f0-\u06f9]+)\s*\]+/gi,
  /\[ID\[([0-9\u0660-\u0669\u06f0-\u06f9]+)\]\]/gi,
  /\[+\s*I\s*\[\s*D\s*\]\s*:\s*([0-9\u0660-\u0669\u06f0-\u06f9]+)\s*\]+/gi,
  /\[+\s*I\s*:\s*D\s*\]\s*:\s*([0-9\u0660-\u0669\u06f0-\u06f9]+)\s*\]+/gi,
  /\[+\s*I\s*:\s*D\s*:\s*([0-9\u0660-\u0669\u06f0-\u06f9]+)\s*\]+/gi,
  /\[\[\s*D\s*\]\s*:\s*([0-9\u0660-\u0669\u06f0-\u06f9]+)\s*\]+/gi,
];

export function transformCitationMarkers(text: string, render: (marker: string) => string): string {
  function replaceMarkers(value: string, format: (marker: string) => string): string {
    return value.replace(CITATION_MARKER_PATTERN, (match, explicit, legacy, offset: number) => {
      if (!explicit) {
        const before = value[offset - 1] ?? '';
        const after = value[offset + match.length] ?? '';
        if (before === ':' || before === ']' || before === '[' || after === ':' || after === ']' || /[A-Za-z0-9_]/.test(before) || /[A-Za-z0-9_]/.test(after)) return match;
      }
      const digits = (explicit ?? legacy).replace(/[\u0660-\u0669\u06f0-\u06f9]/g, (char: string) => String(char.charCodeAt(0) - (char.charCodeAt(0) >= 0x6f0 ? 0x6f0 : 0x660)));
      return format(digits.replace(/^0+(?=\d)/, ''));
    });
  }
  function prose(value: string): string {
    while (true) {
      const previous = value;
      for (const repair of repairs) value = value.replace(repair, '[ID:$1]');
      value = replaceMarkers(value, marker => `[ID:${marker}]`);
      if (previous === value) break;
    }
    return replaceMarkers(value, render);
  }
  let output = '';
  let offset = 0;
  for (const [start, end] of protectedRanges(text)) {
    output += prose(text.slice(offset, start)) + text.slice(start, end);
    offset = end;
  }
  return output + prose(text.slice(offset));
}
