// A minimal server-sent-events parser: feed it text as it arrives, get back
// the `data` of each complete event. Comment lines (": keep-alive") and other
// fields are ignored, which is all the CarePoint streams use.

export class SseParser {
  private buffer = "";
  private data: string[] = [];

  push(chunk: string): string[] {
    this.buffer += chunk;
    const events: string[] = [];
    let newline = this.buffer.search(/\r?\n/);
    while (newline !== -1) {
      const line = this.buffer.slice(0, newline);
      this.buffer = this.buffer.slice(newline + (this.buffer[newline] === "\r" ? 2 : 1));
      if (line === "") {
        if (this.data.length > 0) events.push(this.data.join("\n"));
        this.data = [];
      } else if (line.startsWith("data:")) {
        this.data.push(line.slice(line.startsWith("data: ") ? 6 : 5));
      }
      newline = this.buffer.search(/\r?\n/);
    }
    return events;
  }
}
