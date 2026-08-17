import {useCallback, useEffect, useRef, useState} from "react";


export interface InspectorResource<T> {
  data: T | null;
  isLoading: boolean;
  error: string | null;
  reload: () => void;
}

/**
 * Fetch one optional Inspector dataset only while its tab is visible. A request
 * token supplements AbortController because a transport can resolve after abort.
 */
export function useRunInspectorResource<T>(
  runId: string | null,
  enabled: boolean,
  load: (runId: string, signal: AbortSignal) => Promise<T>,
): InspectorResource<T> {
  const [data, setData] = useState<T | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const requestIdRef = useRef(0);

  useEffect(() => {
    if (!runId || !enabled) return;
    const controller = new AbortController();
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    setData(null);
    setIsLoading(true);
    setError(null);

    load(runId, controller.signal).then((result) => {
      if (controller.signal.aborted || requestIdRef.current !== requestId) return;
      setData(result);
    }).catch((caught: unknown) => {
      if (controller.signal.aborted || requestIdRef.current !== requestId) return;
      setError(caught instanceof Error ? caught.message : "Unable to load Inspector details");
    }).finally(() => {
      if (!controller.signal.aborted && requestIdRef.current === requestId) setIsLoading(false);
    });
    return () => controller.abort();
  }, [enabled, load, reloadToken, runId]);

  const reload = useCallback(() => setReloadToken((token) => token + 1), []);
  return {data, isLoading, error, reload};
}
