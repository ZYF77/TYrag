import { RunningStatus, RunningStatusMap } from '../dataset/constant';
import { LogTabs } from './dataset-common';

export interface DocumentLog {
  fileName: string;
  status: RunningStatus;
  statusName: typeof RunningStatusMap;
}

export interface FileLogsTableProps {
  data: Array<IFileLogItem & DocumentLog>;
  pageCount: number;
  pagination: {
    current: number;
    pageSize: number;
    total: number;
  };
  setPagination: (pagination: { page: number; pageSize: number }) => void;
  loading?: boolean;
  active: (typeof LogTabs)[keyof typeof LogTabs];
}

export interface IOverviewTotal {
  /** status.unstart_count — pending-parse big number */
  pending: number;
  /** doc_num — ingested / file list total for pending-card success */
  ingested: number;
  /** pending-card fail; fixed 0 (no download-fail from source_type) */
  pendingFailed: number;
  /** status.running_count */
  processing: number;
  /** status.done_count */
  finished: number;
  /** status.fail_count */
  failed: number;
  /** status.cancel_count */
  cancelled: number;
  /** compat alias of pending (legacy field name) */
  downloaded: number;
}

export interface IFileLogItem {
  create_date: string;
  create_time: number;
  document_id: string;
  document_name: string;
  document_suffix: string;
  document_type: string;
  dsl: any;
  path: string[];
  task_id: string;
  id: string;
  name: string;
  kb_id: string;
  operation_status: string;
  parser_id: string;
  pipeline_id: string;
  pipeline_title: string;
  avatar: string;
  process_begin_at: null | string;
  process_duration: number;
  progress: number;
  progress_msg: string;
  source_type?: string;
  source_from?: string;
  status: string;
  task_type: string;
  tenant_id: string;
  update_date: string;
  update_time: number;
}
export interface IFileLogList {
  logs: Array<IFileLogItem & DocumentLog>;
  total: number;
}
