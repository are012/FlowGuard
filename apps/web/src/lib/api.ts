"use client";

import { useCallback, useEffect, useState } from "react";

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
      return body.detail.map((item) => item.msg).filter(Boolean).join(", ");
    }
    if (body.message) return body.message;
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

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

interface RemoteState<T> {
  data?: T;
  error?: Error;
  loading: boolean;
}

export function useRemote<T>(path: string) {
  const [revision, setRevision] = useState(0);
  const [state, setState] = useState<RemoteState<T>>({ loading: true });

  useEffect(() => {
    const controller = new AbortController();

    apiRequest<T>(path, { signal: controller.signal })
      .then((data) => setState({ data, loading: false }))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setState({
          error: error instanceof Error ? error : new Error("알 수 없는 오류가 발생했습니다."),
          loading: false,
        });
      });

    return () => controller.abort();
  }, [path, revision]);

  const reload = useCallback(() => {
    setState((current) => ({ ...current, loading: true, error: undefined }));
    setRevision((value) => value + 1);
  }, []);
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
