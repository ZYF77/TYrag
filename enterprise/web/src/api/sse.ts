import type { SseEvent } from './v2Types';

export interface SseParser {
  feed(chunk: string): void;
  end(): void;
}

/** Parse browser ReadableStream chunks without assuming event boundaries. */
export function createSseParser(onEvent: (event: SseEvent) => void): SseParser {
  let buffer = '';
  let eventName = '';
  let dataLines: string[] = [];

  const dispatch = () => {
    if (!eventName && dataLines.length === 0) return;
    const data = dataLines.join('\n');
    onEvent({
      event: eventName || (data === '[DONE]' ? 'stream.end' : 'message'),
      data,
    });
    eventName = '';
    dataLines = [];
  };

  const consumeLine = (line: string) => {
    if (line === '') {
      dispatch();
      return;
    }
    if (line.startsWith(':')) return;

    const separator = line.indexOf(':');
    const field = separator === -1 ? line : line.slice(0, separator);
    let value = separator === -1 ? '' : line.slice(separator + 1);
    if (value.startsWith(' ')) value = value.slice(1);

    if (field === 'event') eventName = value;
    if (field === 'data') dataLines.push(value);
  };

  const consumeAvailableLines = () => {
    while (true) {
      const lineBreak = buffer.search(/[\r\n]/);
      if (lineBreak === -1) return;

      const breakChar = buffer[lineBreak];
      if (breakChar === '\r' && lineBreak + 1 === buffer.length) return;

      const line = buffer.slice(0, lineBreak);
      const remove = breakChar === '\r' && buffer[lineBreak + 1] === '\n' ? 2 : 1;
      buffer = buffer.slice(lineBreak + remove);
      consumeLine(line);
    }
  };

  return {
    feed(chunk: string) {
      buffer += chunk;
      consumeAvailableLines();
    },
    end() {
      if (buffer) {
        consumeLine(buffer.replace(/\r$/, ''));
        buffer = '';
      }
      dispatch();
    },
  };
}
