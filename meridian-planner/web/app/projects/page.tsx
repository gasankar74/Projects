"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Card } from "@/components/Charts";
import { Shell, useApp } from "@/components/Shell";
import { api, useApi } from "@/lib/api";
import { fmtDeadline, fmtDuration } from "@/lib/dates";
import type { Area, Project, Starter, Template } from "@/lib/types";

function ProjectCard({ p }: { p: Project }) {
  const { categories } = useApp();
  const total = p.open_count + p.done_count;
  const pct = total ? Math.round((100 * p.done_count) / total) : 0;
  const cat = categories.find((c) => c.id === p.category_id);
  return (
    <Link href={`/projects/${p.id}`} className="block rounded-lg border border-stone-200 bg-white p-3 hover:border-brand-500 dark:border-stone-800 dark:bg-stone-900">
      <div className="flex items-center gap-2">
        <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: p.color ?? cat?.color ?? "#a8a29e" }} aria-hidden />
        <span className="truncate font-medium">{p.name}</span>
        {p.status !== "active" && <span className="chip bg-stone-100 text-stone-600 dark:bg-stone-800 dark:text-stone-300">{p.status.replace("_", " ")}</span>}
      </div>
      <div className="mt-2 h-1.5 rounded-full bg-stone-100 dark:bg-stone-800" aria-label={`${pct}% done`}>
        <div className="h-1.5 rounded-full bg-emerald-500" style={{ width: `${pct}%` }} />
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-3 text-xs text-stone-500">
        <span>
          {p.done_count}/{total} done
        </span>
        {p.open_min > 0 && <span>{fmtDuration(p.open_min)} left</span>}
        {p.deadline && <span>Due {fmtDeadline(p.deadline)}</span>}
        {cat && <span>{cat.name}</span>}
      </div>
    </Link>
  );
}

function NewProject({ areas, templates, onDone }: { areas: Area[]; templates: Template[]; onDone: () => void }) {
  const { categories, reloadMeta } = useApp();
  const router = useRouter();
  const [name, setName] = useState("");
  const [area, setArea] = useState("");
  const [cat, setCat] = useState("");
  const [tpl, setTpl] = useState("");
  const [start, setStart] = useState(() => new Date().toISOString().slice(0, 10));
  const [err, setErr] = useState<string | null>(null);
  return (
    <form
      className="grid gap-2 sm:grid-cols-2 lg:grid-cols-[2fr_1fr_1fr_1.4fr_auto]"
      onSubmit={async (e) => {
        e.preventDefault();
        if (!name.trim()) return;
        setErr(null);
        try {
          let id: string;
          if (tpl) {
            const r = await api<{ project: Project }>(`/templates/${tpl}/instantiate`, {
              method: "POST",
              json: { name: name.trim(), start_date: start, area_id: area || null, category_id: cat || null },
            });
            id = r.project.id;
          } else {
            const p = await api<Project>("/projects", { method: "POST", json: { name: name.trim(), area_id: area || null, category_id: cat || null, start_date: start } });
            id = p.id;
          }
          reloadMeta();
          onDone();
          router.push(`/projects/${id}`);
        } catch (e) {
          setErr((e as Error).message);
        }
      }}
    >
      <input className="input" placeholder="New project name" value={name} onChange={(e) => setName(e.target.value)} />
      <select className="input" value={area} onChange={(e) => setArea(e.target.value)} aria-label="Area">
        <option value="">No area</option>
        {areas.map((a) => (
          <option key={a.id} value={a.id}>
            {a.name}
          </option>
        ))}
      </select>
      <select className="input" value={cat} onChange={(e) => setCat(e.target.value)} aria-label="Category">
        <option value="">No category</option>
        {categories.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </select>
      <div className="flex gap-1">
        <select className="input" value={tpl} onChange={(e) => setTpl(e.target.value)} aria-label="Template">
          <option value="">Blank project</option>
          {templates.map((t) => (
            <option key={t.id} value={t.id}>
              From “{t.name}”
            </option>
          ))}
        </select>
        <input className="input !w-36" type="date" value={start} onChange={(e) => setStart(e.target.value)} title="Start date (template deadlines count from here)" aria-label="Start date" />
      </div>
      <button className="btn-primary justify-center">Create</button>
      {err && <div className="text-sm text-red-600 sm:col-span-2 lg:col-span-5">{err}</div>}
    </form>
  );
}

function Templates({ data, reload }: { data: { templates: Template[]; starters: Starter[] }; reload: () => void }) {
  const [err, setErr] = useState<string | null>(null);
  const have = new Set(data.templates.map((t) => t.name));
  return (
    <Card title="Templates">
      <p className="mb-3 text-xs text-stone-500">
        A template is a list of tasks with estimates, dependencies and due dates counted from the project start. Save any project as a template from its page.
      </p>
      <ul className="mb-4 space-y-1 text-sm">
        {data.templates.map((t) => (
          <li key={t.id} className="flex items-center gap-2">
            <span className="font-medium">{t.name}</span>
            <span className="text-xs text-stone-500">{t.items.length} tasks</span>
            <button
              className="ml-auto text-xs text-stone-400 hover:text-red-600"
              onClick={async () => {
                if (!confirm(`Delete the template “${t.name}”? Projects made from it are not affected.`)) return;
                await api(`/templates/${t.id}`, { method: "DELETE" });
                reload();
              }}
            >
              Delete
            </button>
          </li>
        ))}
        {data.templates.length === 0 && <li className="text-stone-400">No templates yet.</li>}
      </ul>
      <div className="text-xs font-semibold text-stone-500">Starters</div>
      <ul className="mt-1 space-y-2 text-sm">
        {data.starters.map((s) => (
          <li key={s.key} className="flex items-start gap-2">
            <div className="min-w-0 flex-1">
              <div className="font-medium">{s.name}</div>
              <div className="text-xs text-stone-500">{s.description}</div>
            </div>
            <button
              className="btn text-xs"
              disabled={have.has(s.name)}
              onClick={async () => {
                setErr(null);
                try {
                  await api(`/templates/starters/${s.key}`, { method: "POST" });
                  reload();
                } catch (e) {
                  setErr((e as Error).message);
                }
              }}
            >
              {have.has(s.name) ? "Added" : "Add"}
            </button>
          </li>
        ))}
      </ul>
      {err && <div className="mt-2 text-sm text-red-600">{err}</div>}
    </Card>
  );
}

function Projects() {
  const { data: projects, reload } = useApi<Project[]>("/projects?include_archived=true");
  const { data: areas, reload: reloadAreas } = useApi<Area[]>("/areas");
  const { data: tpls, reload: reloadTpls } = useApi<{ templates: Template[]; starters: Starter[] }>("/templates");
  const [newArea, setNewArea] = useState("");
  const [showDone, setShowDone] = useState(false);
  const list = (projects ?? []).filter((p) => showDone || (p.status !== "done" && p.status !== "archived"));
  const groups: { area: Area | null; items: Project[] }[] = [
    ...(areas ?? []).map((a) => ({ area: a, items: list.filter((p) => p.area_id === a.id) })),
    { area: null, items: list.filter((p) => !p.area_id || !(areas ?? []).some((a) => a.id === p.area_id)) },
  ];
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl space-y-4 p-4">
        <div className="flex items-center gap-2">
          <h1 className="text-lg font-semibold">Projects</h1>
          <label className="ml-auto flex items-center gap-1.5 text-xs text-stone-500">
            <input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} /> Show done and archived
          </label>
        </div>
        <Card title="New project">
          <NewProject areas={areas ?? []} templates={tpls?.templates ?? []} onDone={reload} />
        </Card>
        {groups.map(({ area, items }) =>
          area || items.length ? (
            <section key={area?.id ?? "none"}>
              <div className="mb-2 flex items-center gap-2">
                <h2 className="text-sm font-semibold text-stone-600 dark:text-stone-300">{area ? area.name : "No area"}</h2>
                {area && (
                  <>
                    <button
                      className="text-xs text-stone-400 hover:text-stone-700"
                      onClick={async () => {
                        const n = prompt("Rename area", area.name);
                        if (n && n.trim()) {
                          await api(`/areas/${area.id}`, { method: "PATCH", json: { name: n.trim() } });
                          reloadAreas();
                        }
                      }}
                    >
                      Rename
                    </button>
                    <button
                      className="text-xs text-stone-400 hover:text-red-600"
                      onClick={async () => {
                        if (!confirm(`Delete the area “${area.name}”? Its projects stay, without an area.`)) return;
                        await api(`/areas/${area.id}`, { method: "DELETE" });
                        reloadAreas();
                        reload();
                      }}
                    >
                      Delete
                    </button>
                  </>
                )}
              </div>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {items.map((p) => (
                  <ProjectCard key={p.id} p={p} />
                ))}
                {items.length === 0 && <div className="text-sm text-stone-400">No projects in this area.</div>}
              </div>
            </section>
          ) : null,
        )}
        <form
          className="flex max-w-sm gap-2"
          onSubmit={async (e) => {
            e.preventDefault();
            if (!newArea.trim()) return;
            await api("/areas", { method: "POST", json: { name: newArea.trim() } });
            setNewArea("");
            reloadAreas();
          }}
        >
          <input className="input" placeholder="New area (for example Legal practice)" value={newArea} onChange={(e) => setNewArea(e.target.value)} />
          <button className="btn">Add area</button>
        </form>
        {tpls && <Templates data={tpls} reload={reloadTpls} />}
      </div>
    </div>
  );
}

export default function ProjectsPage() {
  return (
    <Shell>
      <Projects />
    </Shell>
  );
}
