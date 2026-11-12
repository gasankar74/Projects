"""What the AI assistant may read.

A task's details (title, notes, project, checklist) reach the model only when the task is visible:
- the task's own "Allow the AI assistant" box wins;
- otherwise a category or project set to "hidden" hides every task in it;
- otherwise a confidential task is hidden, unless its category or project is set to "allowed" or the user has opted
  in for all tasks;
- everything else is visible.
A hidden task is still shown to the model as "Confidential task" with its timing (duration, deadline, planned time),
so the assistant can plan around it without knowing what it is.
"""

from __future__ import annotations

from ..models import Category, Project, Task, User


def visible(task: Task, user: User, cat: Category | None, proj: Project | None) -> bool:
    if task.ai_allowed:
        return True
    access = {(cat.ai_access if cat else "default"), (proj.ai_access if proj else "default")}
    if "hidden" in access:
        return False
    if task.confidential:
        return "allowed" in access or bool(user.ai_opt_in)
    return True


def project_visible(proj: Project | None) -> bool:
    return proj is None or proj.ai_access != "hidden"
