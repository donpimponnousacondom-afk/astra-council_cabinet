/** Research-specific API contracts; optional fields support older saved jobs/drafts. */
export interface ResearchConfiguration {
  [key: string]: unknown;
  profile_id?: string;
  system_prompt?: string;
  max_output_tokens?: number | null;
  timeout_seconds?: number | null;
  max_keyword?: number | null;
  limit?: number | null;
  max_parallel_jobs?: number | null;
  max_research_batches?: number | null;
  notify_on_completion?: boolean;
}

export type BackgroundJobState =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export type BackgroundNotification =
  | "none"
  | "pending"
  | "claimed"
  | "read"
  | "revoked";

export interface ResearchMetrics {
  duration_ms?: number | null;
  ttft_ms?: number | null;
  tps?: number | null;
  tps_source?: string;
  tokens?: Record<string, { value: number | null; source: string }> | null;
  inference_cost?: number | null;
  search_cost?: number | null;
  attempts?: number | null;
}

export interface BackgroundJobSummary {
  id: string;
  plugin: string;
  bot_id: string;
  channel_id: string;
  origin_turn_id: string;
  created_at: number;
  updated_at: number;
  deadline: number;
  state: BackgroundJobState;
  notification: BackgroundNotification;
  continuation_turn_id: string | null;
  error: string | null;
  metrics: ResearchMetrics | null;
}

export interface BackgroundJobDetail {
  job_id: string;
  state: BackgroundJobState;
  notification: BackgroundNotification;
  elapsed_seconds: number;
  error: string | null;
  assignment: string;
  metrics: ResearchMetrics | null;
  content: string;
  offset: number;
  total_chars: number;
  next_offset: number | null;
}
