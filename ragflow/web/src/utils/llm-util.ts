import { getCachedLlmList } from './llm-cache';

// The names of the large models returned by the interface are similar to "deepseek-r1___OpenAI-API"
export function getRealModelName(llmName: string) {
  return llmName.split('__').at(0) ?? '';
}

// Get tenant model ID from LLM list by model name and factory ID
export function getTenantModelId(
  llmList: Record<string, any>,
  modelName: string,
  factoryId: string,
): string {
  // Iterate through all providers in the LLM list
  for (const [provider, data] of Object.entries(llmList)) {
    if (data.llm && Array.isArray(data.llm)) {
      // Handle /v1/llm/my_llms format
      const model = data.llm.find(
        (m: any) => m.name === modelName && provider === factoryId,
      );
      if (model && model.id) {
        return model.id;
      }
    } else if (Array.isArray(data)) {
      // Handle /v1/llm/list format
      const model = data.find(
        (m: any) => m.llm_name === modelName && m.fid === factoryId,
      );
      if (model && model.id) {
        return model.id;
      }
    }
  }
  return '';
}

/** Build "modelName@instanceName@providerName" */
export function buildModelValue(model: {
  model_name: string;
  model_instance: string;
  model_provider: string;
}) {
  return `${model.model_name}@${model.model_instance}@${model.model_provider}`;
}

/**
 * Parse "modelName@instanceName@providerName" (or the 2-part
 * "modelName@providerName" form where the instance defaults to "default").
 *
 * The composite key is right-anchored: the *last* '@'-separated field is the
 * provider, the second-to-last is the instance, and everything to the left of
 * the second-to-last '@' is the bare model name. Some model names legitimately
 * contain '@' themselves (e.g. LM Studio embedding IDs such as
 * `text-embedding-nomic-embed-text-v1.5@q8_0`), producing four-`@` composite
 * keys like `text-embedding-nomic-embed-text-v1.5@q8_0@lmstudio@LM-Studio`.
 *
 * A naive `split("@")` (or anchoring on the first '@') mis-parses these keys
 * — PATCH /api/v1/models/default then sends `model_name="…v1.5"` and
 * `model_instance="q8_0@lmstudio"`, and the server replies
 * `Instance 'q8_0@lmstudio' not found for provider 'LM-Studio'`.
 *
 * Right-anchored split mirrors `api/db/joint_services/tenant_model_service.py`
 * `split_model_name` and the Go `parseModelName` (PR #16468 family).
 */
export function parseModelValue(val: string) {
  if (!val) return null;
  const lastAt = val.lastIndexOf('@');
  if (lastAt === -1) return null;
  const secondLastAt = val.lastIndexOf('@', lastAt - 1);
  if (secondLastAt === -1) {
    // 2-part form: "modelName@providerName" — instance defaults to "default".
    return {
      model_name: val.substring(0, lastAt),
      model_instance: 'default',
      model_provider: val.substring(lastAt + 1),
    };
  }
  return {
    model_name: val.substring(0, secondLastAt),
    model_instance: val.substring(secondLastAt + 1, lastAt),
    model_provider: val.substring(lastAt + 1),
  };
}

// Extract model name and factory ID from a model UUID
// Supports both "model_name@factory_id" and "model_name@factory_id#instance_name".
// Uses right-anchored split for the same reason as parseModelValue:
// model names may contain '@' themselves, so a naive split('@') drops the
// last portion of the model name into factoryId.
export function parseModelUuid(uuid: string): {
  modelName: string;
  factoryId: string;
} {
  const hashIndex = uuid.indexOf('#');
  const core = hashIndex === -1 ? uuid : uuid.slice(0, hashIndex);
  const lastAt = core.lastIndexOf('@');
  if (lastAt === -1) {
    return { modelName: core, factoryId: '' };
  }
  return {
    modelName: core.substring(0, lastAt),
    factoryId: core.substring(lastAt + 1),
  };
}

// Model parameter to tenant parameter mapping
type ModelParamMap = {
  [key: string]: string;
};

const modelParamMap: ModelParamMap = {
  llm_id: 'tenant_llm_id',
  embd_id: 'tenant_embd_id',
  asr_id: 'tenant_asr_id',
  tts_id: 'tenant_tts_id',
  img2txt_id: 'tenant_img2txt_id',
  rerank_id: 'tenant_rerank_id',
};

// API endpoint whitelist - only these endpoints will have tenant parameters added
const API_WHITELIST = [
  '/api/v1/users/me/models',
  '/api/v1/chats',
  '/v1/canvas/set',
  '/v1/canvas/setting',
  '/api/v1/searches/',
  '/api/v1/memories',
  '/api/v1/datasets',
  '/v1/dataflow/set',
];

// Check if the URL is in the whitelist
export function isUrlInWhitelist(url: string): boolean {
  return API_WHITELIST.some((endpoint) => url.includes(endpoint));
}

// Add tenant model ID parameters to request data
export function addTenantParams(data: any, url?: string): any {
  if (!data || typeof data !== 'object') return data;

  // If URL is provided and not in whitelist, return original data
  if (url && !isUrlInWhitelist(url)) {
    return data;
  }

  // Handle arrays
  if (Array.isArray(data)) {
    return data.map((item) => addTenantParams(item, url));
  }

  const newData = { ...data };
  const llmList = getCachedLlmList();

  // Iterate through model parameters and add corresponding tenant parameters
  for (const [paramName, tenantParamName] of Object.entries(modelParamMap)) {
    if (newData[paramName]) {
      try {
        const parsed = parseModelValue(newData[paramName]);
        if (parsed && llmList) {
          const tenantModelId = getTenantModelId(
            llmList,
            parsed.model_name,
            parsed.model_provider,
          );
          if (tenantModelId) {
            newData[tenantParamName] = tenantModelId;
          }
        } else if (!parsed) {
          // Bare tenant-model id — keep the pair in sync even without LLM cache.
          newData[tenantParamName] = newData[paramName];
        }
      } catch (error) {
        console.error(`Error processing ${paramName}:`, error);
      }
    }
  }

  // Recursively process nested objects
  for (const [key, value] of Object.entries(newData)) {
    if (value && typeof value === 'object' && !modelParamMap[key]) {
      newData[key] = addTenantParams(value, url);
    }
  }

  return newData;
}

/** Coerce a model_type list from GET into the string the chat API expects. */
export function chatSaveModelType(selectedModelType: unknown): string {
  if (Array.isArray(selectedModelType)) {
    if (selectedModelType.includes('chat')) {
      return 'chat';
    }
    return selectedModelType.includes('vision') ? 'vision' : 'chat';
  }
  if (selectedModelType === 'vision' || selectedModelType === 'chat') {
    return selectedModelType;
  }
  return 'chat';
}

/**
 * Chat GET returns llm_id as a composite model name. Copying that name into
 * tenant_llm_id makes older backends fail with 102 (`must be a valid tenant
 * model id`). Only overwrite tenant_llm_id when llm_id is a bare UUID.
 */
export function tenantLlmIdForChatSave(
  llmId: string | undefined,
): string | undefined {
  if (!llmId) {
    return undefined;
  }
  if (parseModelValue(llmId)) {
    return undefined;
  }
  return llmId;
}

export function chatAssistantSaveModelFields(
  llmId: string | undefined,
  selectedModelType: unknown,
): { tenant_llm_id?: string; model_type: string } {
  const model_type = chatSaveModelType(selectedModelType);
  const tenant_llm_id = tenantLlmIdForChatSave(llmId);
  if (tenant_llm_id) {
    return { tenant_llm_id, model_type };
  }
  return { model_type };
}
