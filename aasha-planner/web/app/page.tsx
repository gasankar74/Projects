"use client";

import { CalendarView } from "@/components/CalendarView";
import { Shell } from "@/components/Shell";
import { TaskPanel } from "@/components/TaskPanel";

export default function PlannerPage() {
  return (
    <Shell>
      <div className="flex h-full flex-col lg:flex-row">
        <section className="min-h-[60vh] min-w-0 flex-1 bg-white lg:min-h-0 dark:bg-stone-900">
          <CalendarView />
        </section>
        <aside className="h-[70vh] border-t border-stone-200 lg:h-full lg:w-96 lg:border-t-0 lg:border-l dark:border-stone-800">
          <TaskPanel />
        </aside>
      </div>
    </Shell>
  );
}
