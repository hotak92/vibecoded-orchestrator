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

export enum Level {
  Low,
  High,
}

export type Pair<T> = [T, T];

namespace Shapes {
  export function unit(): number {
    return 1;
  }
}

class Widget {
  onClick = (e: Event): void => {
    e.preventDefault();
  };
  static make = async () => new Widget();
}

declare function ambient(x: number): string;

function overloaded(a: string): string;
function overloaded(a: number): number;
function overloaded(a: any): any {
  return a;
}
