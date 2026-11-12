export type Priority = "urgent" | "high" | "medium" | "low";
export type Status = "inbox" | "todo" | "in_progress" | "blocked" | "done" | "cancelled";

export interface User {
  id: string;
  email: string;
  name: string;
  timezone: string;
  ai_opt_in: boolean;
}

export interface Category {
  id: string;
  name: string;
  color: string;
  position: number;
  default_priority: Priority;
  open_count: number;
}

export interface Project {
  id: string;
  name: string;
  category_id: string | null;
  status: string;
  priority: Priority;
  deadline: string | null;
  color: string | null;
  client: string | null;
  notes: string | null;
  open_count: number;
  done_count: number;
}

export interface ChecklistItem {
  id: string;
  text: string;
  done: boolean;
  position: number;
}

export interface Task {
  id: string;
  title: string;
  description: string | null;
  notes: string | null;
  status: Status;
  priority: Priority;
  category_id: string | null;
  project_id: string | null;
  parent_task_id: string | null;
  client: string | null;
  deadline: string | null;
  deadline_type: "hard" | "soft";
  preferred_date: string | null;
  earliest_start: string | null;
  duration_min: number;
  remaining_min: number;
  completion_pct: number;
  can_split: boolean;
  min_session_min: number | null;
  max_session_min: number | null;
  is_fixed: boolean;
  pinned_start: string | null;
  locked: boolean;
  auto_schedule: boolean;
  preferred_time_of_day: "morning" | "afternoon" | "evening" | null;
  allowed_days: number[] | null;
  windows: { days: number[]; start: string; end: string }[] | null;
  buffer_before_min: number;
  buffer_after_min: number;
  energy: "high" | "medium" | "low" | null;
  calendar_id: string | null;
  recurrence: { rrule: string; text?: string; kind?: string } | null;
  tags: string[];
  links: { title: string; url: string }[];
  confidential: boolean;
  ai_allowed: boolean;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  source: string;
  checklist: ChecklistItem[];
  depends_on: string[];
  blocking: string[];
  overdue: boolean;
  scheduled_start: string | null;
  scheduled_end: string | null;
  schedule_status: "scheduled" | "partial" | "unscheduled" | "late" | null;
  at_risk: boolean;
  score: number | null;
}

export interface BlockReason {
  score?: number;
  parts?: Record<string, number>;
  why?: string[];
  bound_by?: string;
}

export interface Block {
  id: string;
  task_id: string;
  title: string;
  priority: Priority;
  category_id: string | null;
  color: string | null;
  start: string;
  end: string;
  session_index: number;
  session_count: number;
  locked: boolean;
  reason: BlockReason;
}

export interface PlanWarning {
  kind: "cannot_meet_deadline" | "will_miss_soft_deadline" | "at_risk" | "unscheduled" | "blocked_by_prerequisite" | "locked_conflict" | "no_slot_long_enough";
  task_id: string | null;
  message: string;
  data: Record<string, unknown>;
}

export interface Overload {
  shortfall_min: number;
  message: string;
  defer: { task_id: string; title: string; minutes: number; score: number }[];
  feasible_after_defer: boolean;
}

export interface Schedule {
  run: { id: string; created_at: string; trigger: string; duration_ms: number; restored_from_id: string | null; block_count: number } | null;
  blocks: Block[];
  warnings: PlanWarning[];
  overload: Overload | null;
  days: { date: string; work_min: number; free_min: number; planned_min: number }[];
  dirty: boolean;
  can_undo: boolean;
}

export interface ScheduleSettings {
  timezone: string;
  work_hours: Record<string, [string, string][]>;
  horizon_days: number;
  transition_min: number;
  daily_cap_min: number | null;
  default_min_session: number;
  freeze_min: number;
  risk_slack_min: number;
  grid_min: number;
  weights: Record<string, number>;
}

export interface TaskEvent {
  id: string;
  actor: string;
  action: string;
  diff: Record<string, unknown>;
  at: string;
}

export interface GoogleAccount {
  id: string;
  email: string;
  status: "ok" | "syncing" | "error" | "needs_reconnect";
  last_error: string | null;
  last_synced_at: string | null;
}

export interface Calendar {
  id: string;
  google_account_id: string;
  summary: string;
  color: string | null;
  access_role: string;
  time_zone: string | null;
  primary: boolean;
  selected: boolean;
  blocks_time: boolean;
  is_task_target: boolean;
  writable: boolean;
  last_synced_at: string | null;
  last_error: string | null;
}

export interface CalEvent {
  id: string;
  calendar_id: string;
  summary: string;
  location: string | null;
  html_link: string | null;
  start_at: string;
  end_at: string;
  all_day: boolean;
  start_date: string | null;
  end_date: string | null;
  transparency: string;
  visibility: string;
  status: string;
  is_busy: boolean;
  color: string | null;
  calendar_name: string | null;
  read_only: boolean;
}

export const PRIORITIES: Priority[] = ["urgent", "high", "medium", "low"];
export const PRIORITY_LABEL: Record<Priority, string> = { urgent: "Urgent", high: "High", medium: "Medium", low: "Low" };
export const STATUS_LABEL: Record<Status, string> = {
  inbox: "Inbox",
  todo: "To do",
  in_progress: "In progress",
  blocked: "Blocked",
  done: "Done",
  cancelled: "Cancelled",
};
