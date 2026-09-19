import { IOverviewTotal } from './interface';

export interface IIngestionStatus {
  cancel_count?: number;
  done_count?: number;
  fail_count?: number;
  running_count?: number;
  unstart_count?: number;
}

export interface IIngestionSummary {
  doc_num?: number;
  chunk_num?: number;
  token_num?: number;
  status?: IIngestionStatus;
}

/**
 * Adapt GET /v1/datasets/{id}/ingestions/summary into overview card totals.
 *
 * Product mapping (Gateway/AiFeed local KB overview):
 * - pending (待解析) big = status.unstart_count
 * - pending success (已入库) = doc_num
 * - pending fail = 0 (do NOT derive download fail from source_type!=local)
 * - processing big = status.running_count
 * - processing success = status.done_count
 * - processing fail = status.fail_count
 *
 * Keeps ingestions/summary contract unchanged (frontend adapter only).
 * Compat aliases: downloaded===pending (old field name).
 */
export function mapOverviewTotal(data?: IIngestionSummary): IOverviewTotal {
  const status = data?.status ?? {};
  const pending = status.unstart_count ?? 0;
  return {
    pending,
    ingested: data?.doc_num ?? 0,
    pendingFailed: 0,
    processing: status.running_count ?? 0,
    finished: status.done_count ?? 0,
    failed: status.fail_count ?? 0,
    cancelled: status.cancel_count ?? 0,
    // compat with older UI field name
    downloaded: pending,
  };
}