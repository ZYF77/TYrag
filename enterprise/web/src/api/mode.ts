export type ApiMode = 'mock' | 'demo' | 'gateway';

function normalizeMode(value: string | undefined): ApiMode {
  const mode = (value ?? 'mock').toLowerCase();
  if (mode === 'demo' || mode === 'gateway') {
    return mode;
  }
  return 'mock';
}

export const API_MODE: ApiMode = normalizeMode(
  import.meta.env.VITE_API_MODE as string | undefined,
);

export function isDemoMode(): boolean {
  return API_MODE === 'demo';
}

export function isMockMode(): boolean {
  return API_MODE === 'mock';
}
