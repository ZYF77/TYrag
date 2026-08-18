jest.mock('@/components/llm-setting-items/next', () => {
  const { z } = require('zod');
  return {
    LlmSettingEnabledSchema: {},
    LlmSettingFieldSchema: {
      temperature: z.number().optional(),
    },
  };
});

jest.mock('@/components/metadata-filter', () => {
  const { z } = require('zod');
  return { MetadataFilterSchema: { meta_data_filter: z.object({}).optional() } };
});

jest.mock('@/components/rerank', () => {
  const { z } = require('zod');
  return {
    rerankFormSchema: {
      rerank_id: z.string().optional(),
      top_k: z.number().optional(),
    },
  };
});

jest.mock('@/components/similarity-slider', () => {
  const { z } = require('zod');
  return {
    similarityThresholdSchema: { similarity_threshold: z.number() },
    vectorSimilarityWeightSchema: { vector_similarity_weight: z.number() },
  };
});

jest.mock('@/components/top-n-item', () => {
  const { z } = require('zod');
  return { topnSchema: { top_n: z.number().optional() } };
});

jest.mock('@/constants/chat', () => ({
  WebSearchProvider: { Tavily: 'tavily', Querit: 'querit' },
}));

jest.mock('@/hooks/common-hooks', () => ({ useTranslate: jest.fn() }));

import { createChatSettingSchema } from './use-chat-setting-schema';

describe('chat settings schema', () => {
  it('allows the backend default empty system prompt', () => {
    const result = createChatSettingSchema((key) => key).safeParse({
      name: 'Assistant',
      icon: '',
      dataset_ids: [],
      prompt_config: {
        quote: false,
        keyword: false,
        tts: false,
        refine_multiturn: true,
        system: '',
      },
      llm_setting: {},
      similarity_threshold: 0.2,
      vector_similarity_weight: 0.2,
      top_n: 8,
    });

    expect(result.success).toBe(true);
  });
});
