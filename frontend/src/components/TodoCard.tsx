import type {WorkspaceTask} from "../types/runtime";
import type {LocalTodo} from "./inspectorData";


interface TodoCardProps {
  title: string;
  kind: "local" | "persistent";
  todos?: LocalTodo[];
  tasks?: WorkspaceTask[];
}

/** Present local todo_write entries separately from durable SQLite task rows. */
export function TodoCard({title, kind, todos = [], tasks = []}: TodoCardProps) {
  const isLocal = kind === "local";
  const isEmpty = isLocal ? todos.length === 0 : tasks.length === 0;

  return (
    <section className="inspector-card todo-card" aria-label={title}>
      <header className="inspector-card__header">
        <div>
          <p>{isLocal ? "Current run" : "Workspace SQLite"}</p>
          <h3>{title}</h3>
        </div>
        <span className={`source-badge source-badge--${kind}`}>
          {isLocal ? "todo_write" : "persistent"}
        </span>
      </header>
      {isEmpty ? (
        <p className="inspector-empty-copy">
          {isLocal ? "No todo_write checklist was recorded for this run." : "No persistent tasks found."}
        </p>
      ) : isLocal ? (
        <ul className="todo-list">
          {todos.map((todo, index) => (
            <li key={`${todo.content}-${index}`}>
              <span className={`todo-state todo-state--${todo.status}`} aria-hidden="true" />
              <span>{todo.content}</span>
              <small>{todo.status.replace("_", " ")}</small>
            </li>
          ))}
        </ul>
      ) : (
        <ul className="todo-list">
          {tasks.map((task) => (
            <li key={task.task_id}>
              <span className={`todo-state todo-state--${task.status}`} aria-hidden="true" />
              <span className="todo-list__copy">
                <strong>{task.subject}</strong>
                {task.blockedBy.length > 0 && <small>Blocked by: {task.blockedBy.join(", ")}</small>}
                {task.worktree_name && <small>Worktree: {task.worktree_name}</small>}
              </span>
              <small>{task.status.replace("_", " ")}</small>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
