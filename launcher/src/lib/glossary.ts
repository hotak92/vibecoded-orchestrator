// Plain-English explanations for jargon scattered across the launcher
// (KG dashboard, Code Graph, project permissions, MCP page, etc.).
//
// Used by:
//   - <Term>            — inline tooltip wrapper
//   - /glossary route   — full alphabetical reference
//
// Add new entries when you find a new piece of jargon a non-tech user
// would not understand. Keep entries short and use plain English; if a
// term needs more detail, link out via `learnMore`.

export interface GlossaryEntry {
  /** Stable lookup key — kebab-case, lowercase. */
  key: string;
  /** Display label (the word as it appears in the UI). */
  label: string;
  /** One-sentence ELI5. */
  short: string;
  /** Optional longer explanation for the glossary page. */
  long?: string;
  /** Optional "learn more" link. */
  learnMore?: { label: string; href: string };
}

const entries: GlossaryEntry[] = [
  {
    key: 'kg',
    label: 'Knowledge Graph',
    short:
      "A searchable memory of notes, decisions, and patterns the project has accumulated.",
    long:
      "The Knowledge Graph is where Claude stores reusable knowledge across sessions: design decisions, recurring bug fixes, conventions, references. It's a folder of Markdown files plus a vector index so search can find things by meaning, not just keywords.",
  },
  {
    key: 'code-graph',
    label: 'Code Graph',
    short:
      "A searchable index of your code's structure — modules, classes, functions, and how they call each other.",
    long:
      "The Code Graph is built by parsing your source tree. It lets you ask 'what calls this function?' or 'find all auth middleware' and get answers without reading file by file.",
  },
  {
    key: 'code-function',
    label: 'CodeFunction',
    short:
      "A function in your code, indexed by the Code Graph so you can search and inspect it.",
    long:
      "CodeFunction is one of five entity types the Code Graph stores (the others: CodeModule, CodeClass, CodeAPI, CodeInteraction). Each entry has a name, signature, body, and lists of calls/types/imports.",
  },
  {
    key: 'code-class',
    label: 'CodeClass',
    short:
      "A class in your code, with its methods, parent classes, and fields recorded.",
  },
  {
    key: 'code-module',
    label: 'CodeModule',
    short:
      "A source file in your project, with its imports and a summary recorded.",
  },
  {
    key: 'code-api',
    label: 'CodeAPI',
    short:
      "An HTTP/RPC endpoint exposed by your code — its path, method, and handler function.",
  },
  {
    key: 'code-interaction',
    label: 'CodeInteraction',
    short:
      "A cross-service call your code makes — for example, an HTTP request to another service or a queue publish.",
  },
  {
    key: 'mcp-server',
    label: 'MCP server',
    short:
      "A small program that gives Claude additional capabilities — like searching the web or reading databases.",
    long:
      "MCP (Model Context Protocol) is an open standard for plugging tools into Claude. An MCP server runs on your machine and exposes a set of tools or resources Claude can call.",
    learnMore: {
      label: 'modelcontextprotocol.io',
      href: 'https://modelcontextprotocol.io',
    },
  },
  {
    key: 'host-base',
    label: 'Standard host (base)',
    short:
      "The standard Orchestrator install: Knowledge Graph, Code Graph, and 16 hooks. Pick this if unsure.",
  },
  {
    key: 'host-mao',
    label: 'MAO host',
    short:
      "Multi-Agent Orchestrator — adds 10 specialist agents and a Maestro coordinator on top of Standard. Beta.",
  },
  {
    key: 'embedding',
    label: 'Embedding',
    short:
      "A list of numbers that captures the meaning of a piece of text or code, so a search can find it by similarity.",
    long:
      "Embeddings are how the KG and Code Graph search by meaning. The model 'qwen3-embedding:0.6b' converts text to a 1024-dim vector; closer vectors mean closer meaning.",
  },
  {
    key: 'embedding-mode',
    label: 'Embedding mode',
    short:
      "Which embedding backend the Knowledge Graph uses — local Ollama (free) or a cloud provider.",
  },
  {
    key: 'permission-write-scope',
    label: 'Permission write scope',
    short:
      "Where a permission rule applies — to this project, to all your projects, or only at runtime.",
    long:
      "Write scope decides where the permission setting is saved. 'Project' means it lives in the project's settings file; 'user' means it applies to every project; 'session' means it lasts only until you restart.",
  },
  {
    key: 'access-mode',
    label: 'Access mode',
    short:
      "Who can see a KG or Code Graph entry — only this project, a chosen list of projects, or everyone (shared).",
  },
  {
    key: 'tier-free',
    label: 'Free tier',
    short:
      "The Orchestrator with all open-source features: KG, Code Graph, hooks. No license needed.",
  },
  {
    key: 'tier-pro',
    label: 'Pro tier',
    short:
      "Adds RL-scored retrieval (search results re-ranked by what's worked before), curated agent packs, and auto-updates.",
  },
  {
    key: 'orchestrator',
    label: 'Orchestrator',
    short:
      "The infrastructure layer this launcher installs into a project — KG, Code Graph, hooks, MCP servers.",
  },
];

const byKey = new Map<string, GlossaryEntry>();
for (const e of entries) byKey.set(e.key, e);

export function getEntry(key: string): GlossaryEntry | undefined {
  return byKey.get(key);
}

export function allEntries(): GlossaryEntry[] {
  return [...entries].sort((a, b) =>
    a.label.localeCompare(b.label, undefined, { sensitivity: 'base' }),
  );
}
