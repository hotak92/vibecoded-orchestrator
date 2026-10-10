// Models module for the golden-fixture repo (TypeScript, regex-parsed).

export interface Named {
  name: string;
}

export class User implements Named {
  name: string;

  constructor(name: string) {
    this.name = name;
  }

  greeting(): string {
    return `hello ${this.name}`;
  }
}

export function makeUser(name: string): User {
  return new User(name);
}

export const identity = (x: number): number => x;
