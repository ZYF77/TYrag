import { useState, useEffect, useCallback } from 'react';
import { api } from '../api/client';
import type { Conversation, CreateConversationRequest, ErrorResponse } from '../api/types';

export function useConversations() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ErrorResponse | null>(null);

  const fetchConversations = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.listConversations();
      setConversations(data);
      if (data.length > 0 && !activeId) {
        setActiveId(data[0].conversationId);
      }
    } catch (err) {
      setError(
        'code' in (err as object) ? (err as ErrorResponse) : { code: 'UNKNOWN', message: String(err), requestId: 'conv-list-err' },
      );
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const createConversation = useCallback(
    async (req: CreateConversationRequest = {}) => {
      setError(null);
      try {
        const conv = await api.createConversation(req);
        setConversations((prev) => [conv, ...prev]);
        setActiveId(conv.conversationId);
        return conv;
      } catch (err) {
        setError(
          'code' in (err as object) ? (err as ErrorResponse) : { code: 'UNKNOWN', message: String(err), requestId: 'conv-create-err' },
        );
        throw err;
      }
    },
    [],
  );

  useEffect(() => {
    fetchConversations();
  }, [fetchConversations]);

  return {
    conversations,
    activeId,
    setActiveId,
    loading,
    error,
    createConversation,
    refresh: fetchConversations,
  };
}
