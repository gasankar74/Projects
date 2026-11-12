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
