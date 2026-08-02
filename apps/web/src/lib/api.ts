"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const DEFAULT_API_URL = "http://localhost:8000";

export const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_URL || DEFAULT_API_URL
).replace(/\/$/, "");

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const remoteCache = new Map<string, unknown>();
let remoteCacheGeneration = 0;

function apiUrl(path: string) {
  return `${API_BASE_URL}${path.startsWith("/") ? path : `/${path}`}`;
}

async function readError(response: Response) {
  try {
    const body = (await response.json()) as {
      detail?: string | Array<{ msg?: string }>;
      message?: string;
    };
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      const detail = body.detail.map((item) => item.msg).filter(Boolean).join(", ");
      if (detail) return detail;
    }
    if (typeof body.message === "string" && body.message) return body.message;
  } catch {
    // The status text below remains the only truthful error information.
  }
  return response.statusText || "요청을 처리하지 못했습니다.";
}

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(apiUrl(path), {
    ...init,
    headers,
    cache: "no-store",
  });

  if (!response.ok) {
    throw new ApiError(await readError(response), response.status);
  }

  if ((init.method || "GET").toUpperCase() !== "GET") {
    remoteCache.clear();
    remoteCacheGeneration += 1;
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

interface RemoteState<T> {
  data?: T;
  error?: Error;
  loading: boolean;
}

function initialRemoteState<T>(path: string): RemoteState<T> {
  if (!remoteCache.has(path)) return { loading: true };
  return { data: remoteCache.get(path) as T, loading: false };
}

export function useRemote<T>(path: string) {
  const [revision, setRevision] = useState(0);
  const forceRefreshPath = useRef<string | null>(null);
  const [state, setState] = useState<RemoteState<T>>(() => initialRemoteState<T>(path));

  useEffect(() => {
    const forceRefresh = forceRefreshPath.current === path;
    forceRefreshPath.current = null;

    if (!forceRefresh && remoteCache.has(path)) {
      setState({ data: remoteCache.get(path) as T, loading: false });
      return;
    }

    const controller = new AbortController();
    const requestGeneration = remoteCacheGeneration;
    setState((current) =>
      forceRefresh
        ? { ...current, error: undefined, loading: true }
        : { loading: true },
    );

    apiRequest<T>(path, { signal: controller.signal })
      .then((data) => {
        if (requestGeneration !== remoteCacheGeneration) {
          setRevision((value) => value + 1);
          return;
        }
        remoteCache.set(path, data);
        setState({ data, loading: false });
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (requestGeneration !== remoteCacheGeneration) {
          setRevision((value) => value + 1);
          return;
        }
        setState({
          error: error instanceof Error ? error : new Error("알 수 없는 오류가 발생했습니다."),
          loading: false,
        });
      });

    return () => controller.abort();
  }, [path, revision]);

  const reload = useCallback(() => {
    forceRefreshPath.current = path;
    setState((current) => ({ ...current, loading: true, error: undefined }));
    setRevision((value) => value + 1);
  }, [path]);
  return { ...state, reload };
}

export function listFrom<T>(
  value: T[] | { items?: T[] } | { data?: T[] } | undefined,
): T[] {
  if (Array.isArray(value)) return value;
  if (value && "items" in value && Array.isArray(value.items)) return value.items;
  if (value && "data" in value && Array.isArray(value.data)) return value.data;
  return [];
}

/**
 * 비동기 분석이 끝날 때까지 대시보드 상태를 확인한다.
 *
 * `FLOWGUARD_ANALYSIS_ASYNC=on` 이면 `POST /api/v1/analyses` 가 202 로
 * 즉시 반환하므로, 완료 여부는 대시보드의 `refresh_status` 로 판단한다.
 * 동기 모드에서는 이 함수를 호출하지 않는다.
 */
export async function waitForAnalysis<T extends { refresh_status?: string }>(
  options: {
    intervalMs?: number;
    timeoutMs?: number;
    onProgress?: (snapshot: T) => void;
    signal?: AbortSignal;
  } = {},
): Promise<T | undefined> {
  const { intervalMs = 2000, timeoutMs = 120_000, onProgress, signal } = options;
  const startedAt = Date.now();

  while (Date.now() - startedAt < timeoutMs) {
    if (signal?.aborted) return undefined;
    try {
      const snapshot = await apiRequest<T>("/api/v1/dashboard", { signal });
      onProgress?.(snapshot);
      const status = snapshot.refresh_status;
      // 최신 리포트가 아직 없으면 refresh_status 가 비어 있을 수 있다.
      if (status && status !== "QUEUED" && status !== "RUNNING") {
        return snapshot;
      }
    } catch (error) {
      // 첫 분석 전에는 리포트가 없어 404 가 난다. 완료를 기다리는 중이므로 계속한다.
      if (!(error instanceof ApiError) || error.status !== 404) throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  return undefined;
}
