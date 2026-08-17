import {useState, type FormEvent, type KeyboardEvent} from "react";


interface ComposerProps {
  canCreate: boolean;
  canStop: boolean;
  isCreating: boolean;
  isStopping: boolean;
  hasRun: boolean;
  onCreate: (prompt: string) => Promise<void>;
  onStop: () => Promise<void>;
}

export function Composer({
  canCreate,
  canStop,
  isCreating,
  isStopping,
  hasRun,
  onCreate,
  onStop,
}: ComposerProps) {
  const [prompt, setPrompt] = useState("");
  const normalizedPrompt = prompt.trim();

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    if (!canCreate || !normalizedPrompt || isCreating) return;
    try {
      await onCreate(normalizedPrompt);
      setPrompt("");
    } catch {
      // App owns the visible API error; keep the prompt so the user can retry.
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }

  return (
    <form className="composer" onSubmit={(event) => void submit(event)}>
      <textarea
        aria-label="Agent task"
        disabled={!canCreate || isCreating}
        onChange={(event) => setPrompt(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={
          canCreate
            ? "Describe a task for the Agent…"
            : "This run is active. Stop it before starting another task."
        }
        rows={2}
        value={prompt}
      />
      <div className="composer__footer">
        <span>
          {hasRun && canCreate
            ? "Sending starts a new run"
            : "Enter to send · Shift+Enter for a new line"}
        </span>
        <div className="composer__actions">
          {canStop && (
            <button
              className="button button--danger-ghost"
              disabled={isStopping}
              onClick={() => void onStop()}
              type="button"
            >
              {isStopping ? "Stopping…" : "Stop"}
            </button>
          )}
          <button
            className="button button--primary"
            disabled={!canCreate || !normalizedPrompt || isCreating}
            type="submit"
          >
            {isCreating ? "Starting…" : "Send"}
          </button>
        </div>
      </div>
    </form>
  );
}
