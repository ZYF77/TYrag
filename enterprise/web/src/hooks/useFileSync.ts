import { useState, useEffect, useCallback } from 'react';
import { api } from '../api/client';
import type { FileSyncItem } from '../api/types';

export function useFileSync() {
  const [items, setItems] = useState<FileSyncItem[]>([]);
  const [loading, setLoading] = useState(false);

  const fetchSyncStatus = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.listSyncStatus();
      setItems(data);
    } catch (err) {
      console.error('Failed to fetch sync status:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSyncStatus();
  }, [fetchSyncStatus]);

  return { items, loading, refresh: fetchSyncStatus };
}
