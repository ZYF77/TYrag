import {
  LayoutRecognizeModelTypes,
  ModelTypeMap,
  VlmModelTypes,
  modelMatchesTypes,
} from '../model-type-filters';

describe('VLM / layout-recognize model type constants', () => {
  it('keeps default VLM and PDF parser aligned on canonical VLM types', () => {
    expect([...VlmModelTypes]).toEqual(['vision', 'image2text']);
    expect(ModelTypeMap.img2txt_id).toEqual([...VlmModelTypes]);
    expect([...LayoutRecognizeModelTypes]).toEqual([
      'vision',
      'image2text',
      'ocr',
    ]);
    expect(ModelTypeMap.layout_recognize).toEqual([
      ...LayoutRecognizeModelTypes,
    ]);
  });
});

describe('layout-recognize model type matching', () => {
  const cases: Array<{ model_type: string[]; visible: boolean }> = [
    { model_type: ['chat', 'vision'], visible: true },
    { model_type: ['vision'], visible: true },
    { model_type: ['image2text'], visible: true },
    { model_type: ['ocr'], visible: true },
    { model_type: ['chat'], visible: false },
    { model_type: ['embedding'], visible: false },
  ];

  it.each(cases)(
    'model_type $model_type → visible=$visible',
    ({ model_type, visible }) => {
      expect(
        modelMatchesTypes(model_type, LayoutRecognizeModelTypes),
      ).toBe(visible);
    },
  );

  it('does not regress to legacy image2text-only filtering', () => {
    // Old PDF parser filter missed API-canonical `vision` models.
    expect(modelMatchesTypes(['vision'], ['image2text', 'ocr'])).toBe(false);
    expect(modelMatchesTypes(['chat', 'vision'], LayoutRecognizeModelTypes)).toBe(
      true,
    );
  });
});
