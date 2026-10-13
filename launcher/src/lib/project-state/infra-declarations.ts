// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.102 W5-A (M3): pure helpers for the Identity tab's "Infrastructure
// this project owns" panel (InfraDeclarationsPanel.svelte).
//
// The backend validates authoritatively
// (`vct_launcher_core::db::infra_declarations::normalise`, mirrored by
// `vco_lib/infra_declarations.py::normalise`); this mirror only lets the
// panel name a bad entry BEFORE the round trip. A value it accepts that the
// backend refuses still surfaces as the command's error.

export type InfraKind = 'external_collection' | 'unmanaged_container' | 'local_port';

/** Wire shape of `get_project_infra_declarations` (Rust `InfraDeclarations`). */
export interface InfraDeclarations {
  external_collections: string[];
  unmanaged_containers: string[];
  local_ports: string[];
}

export interface InfraDeclarationsWriteResult {
  declarations: InfraDeclarations;
  warnings: string[];
}

export interface KindSpec {
  kind: InfraKind;
  field: keyof InfraDeclarations;
  label: string;
  hint: string;
  placeholder: string;
}

/** The three lists, in render order. */
export const KIND_SPECS: readonly KindSpec[] = [
  {
    kind: 'external_collection',
    field: 'external_collections',
    label: 'External collections',
    hint:
      "Weaviate classes this project's own runtime owns. VCO never reports them as unclaimed, " +
      'never treats them as a leftover or as evidence for a KG binding.',
    placeholder: 'MyRuntime_AgentMemory',
  },
  {
    kind: 'unmanaged_container',
    field: 'unmanaged_containers',
    label: 'Unmanaged containers',
    hint:
      'Containers this project runs itself (its own Weaviate or Ollama). VCO never starts, heals, ' +
      're-creates, labels or adopts them.',
    placeholder: 'my-project-weaviate',
  },
  {
    kind: 'local_port',
    field: 'local_ports',
    label: 'Local ports',
    hint:
      "Loopback ports of this project's own services. WebFetch may reach them from this project, " +
      'and VCO never probes them for adoption.',
    placeholder: '11436',
  },
];

const TRIM = /^[ \t\n\r\v\f]+|[ \t\n\r\v\f]+$/g;
const CLASS_RE = /^[A-Za-z][A-Za-z0-9_]{0,254}$/;
const CONTAINER_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$/;
const PORT_RE = /^[0-9]+$/;

/** Client-side mirror of the backend `normalise`; `null` = invalid. */
export function normaliseValue(kind: InfraKind, raw: string): string | null {
  const text = raw.replace(TRIM, '');
  if (!text) return null;
  if (kind === 'external_collection') {
    return CLASS_RE.test(text) ? text[0].toUpperCase() + text.slice(1) : null;
  }
  if (kind === 'unmanaged_container') {
    return CONTAINER_RE.test(text) ? text : null;
  }
  if (!PORT_RE.test(text)) return null;
  const port = Number(text);
  return port >= 1 && port <= 65535 ? String(port) : null;
}

/** The list after adding `raw`, or an error naming why it was refused. */
export function addValue(
  kind: InfraKind,
  current: readonly string[],
  raw: string,
): { values: string[] } | { error: string } {
  const value = normaliseValue(kind, raw);
  if (value === null) {
    return { error: `'${raw.replace(TRIM, '')}' is not a valid ${kind.replace('_', ' ')}` };
  }
  if (current.includes(value)) return { values: [...current] };
  return { values: [...current, value] };
}

/** The list without `value`. */
export function removeValue(current: readonly string[], value: string): string[] {
  return current.filter((v) => v !== value);
}

/** The args `set_project_infra_declarations` takes. */
export function setPayload(projectId: string, kind: InfraKind, values: readonly string[]) {
  return { projectId, kind, values: [...values] };
}

export const EMPTY_DECLARATIONS: InfraDeclarations = {
  external_collections: [],
  unmanaged_containers: [],
  local_ports: [],
};
