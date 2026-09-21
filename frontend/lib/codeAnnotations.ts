export interface WalkthroughStep {
  line_ref: string;
  text: string;
}

interface CodeReference {
  path: string;
  start: number;
  end: number;
}

export interface CodeAnnotation extends WalkthroughStep {
  id: number;
  reference: CodeReference | null;
  notice?: string;
}

/** 引用必须有明确文件与有效行号；不猜位置，也不丢弃无法定位的讲解。 */
function parseReference(lineRef: string): CodeReference | null {
  const match = lineRef.trim().match(/^(.+):([0-9]+)(?:-([0-9]+))?$/);
  if (!match) return null;
  const start = Number(match[2]);
  const end = Number(match[3] ?? match[2]);
  if (!match[1].trim() || !Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 1 || end < start) return null;
  return { path: match[1], start, end };
}

export function placeCodeAnnotations(steps: WalkthroughStep[] | undefined, visible: (CodeReference & { total?: number }) | null) {
  const byLine = new Map<number, CodeAnnotation[]>();
  const unplaced: CodeAnnotation[] = [];
  (steps ?? []).forEach((step, id) => {
    const reference = parseReference(step.line_ref);
    const annotation: CodeAnnotation = { ...step, id, reference };
    if (!reference) {
      unplaced.push({ ...annotation, notice: "引用格式无法定位" });
    } else if (!visible || reference.path !== visible.path) {
      unplaced.push({ ...annotation, notice: "尚未打开对应文件" });
    } else if (visible.total !== undefined && reference.end > visible.total) {
      unplaced.push({ ...annotation, reference: null, notice: "引用行号超出文件范围" });
    } else if (reference.end < visible.start || reference.start > visible.end) {
      unplaced.push({ ...annotation, notice: "不在当前代码范围" });
    } else {
      const line = Math.max(reference.start, visible.start);
      byLine.set(line, [...(byLine.get(line) ?? []), annotation]);
    }
  });
  return { byLine, unplaced };
}
