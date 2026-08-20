/**
 * Shared model-type filters for VLM surfaces.
 *
 * Backend canonical type is `vision`; `image2text` is kept as a legacy alias
 * so older API payloads / persisted rows still match.
 */

/** Canonical VLM types (+ legacy alias). Shared by default-model VLM. */
export const VlmModelTypes = ['vision', 'image2text'] as const;

/** PDF / layout-recognize parser dropdown: VLM + OCR providers. */
export const LayoutRecognizeModelTypes = [
  ...VlmModelTypes,
  'ocr',
] as const;

/** Maps form field names to their supported model types. */
export const ModelTypeMap: Record<string, string[]> = {
  llm_id: ['chat', 'vision'],
  embd_id: ['embedding'],
  img2txt_id: [...VlmModelTypes],
  asr_id: ['asr'],
  rerank_id: ['rerank'],
  tts_id: ['tts'],
  layout_recognize: [...LayoutRecognizeModelTypes],
};

export function modelMatchesTypes(
  modelType: string[] | undefined,
  allowed: readonly string[],
): boolean {
  return modelType?.some((t) => allowed.includes(t)) ?? false;
}
