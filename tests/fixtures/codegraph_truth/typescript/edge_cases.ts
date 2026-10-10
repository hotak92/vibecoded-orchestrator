// Edge cases for the definitions gate (TypeScript).
export interface Shape<T> {
  area(): number;
  scale(factor: T): Shape<T>;
}

export abstract class Base<T> implements Shape<T> {
  abstract area(): number;
  scale(factor: T): Shape<T> {
    return this;
  }
}

class Square extends Base<number> {
  constructor(private side: number) {
    super();
  }
  area(): number {
    return this.side * this.side;
  }
}

export const pick = <T,>(xs: T[], i: number): T => xs[i];

export const summarize = (
  rows: string[],
): { count: number; first: string } => {
  return { count: rows.length, first: rows[0] };
};

export function groupBy<K extends string, V>(
  items: V[],
  key: (v: V) => K,
): Record<K, V[]> {
  const out = {} as Record<K, V[]>;
  for (const item of items) {
    (out[key(item)] ||= []).push(item);
  }
  return out;
}

function outer(): () => number {
  function inner(): number {
    return 1;
  }
  return inner;
}

const label = "function fake() {}";

export default async function* stream(): AsyncGenerator<number> {
  yield label.length;
}
