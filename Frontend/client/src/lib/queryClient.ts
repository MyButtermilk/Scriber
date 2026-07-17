import { QueryClient, QueryFunction } from "@tanstack/react-query";
import { apiUrl } from "./backend";
import { fetchWithTimeout } from "./fetch-with-timeout";
import { friendlyError, responseErrorMessage } from "./request-errors";
import { translateNow } from "@/i18n";

const DEFAULT_API_REQUEST_TIMEOUT_MS = 30_000;

// Outlook's backend sync deliberately allows up to 60 seconds for Credential
// Manager token renewal plus Microsoft Graph pagination. Leave enough room for
// the final credential-backed status read and HTTP response so the WebView does
// not report a false failure while the backend successfully commits the sync.
export const OUTLOOK_SYNC_REQUEST_TIMEOUT_MS = 70_000;

interface ApiRequestOptions {
  timeoutMs?: number;
}

async function throwIfResNotOk(res: Response) {
  if (!res.ok) {
    throw new Error(await responseErrorMessage(res));
  }
}

export async function apiRequest(
  method: string,
  url: string,
  data?: unknown | undefined,
  options: ApiRequestOptions = {},
): Promise<Response> {
  try {
    const res = await fetchWithTimeout(apiUrl(url), {
      method,
      headers: data ? { "Content-Type": "application/json" } : {},
      body: data ? JSON.stringify(data) : undefined,
      credentials: "include",
    }, options.timeoutMs ?? DEFAULT_API_REQUEST_TIMEOUT_MS);

    await throwIfResNotOk(res);
    return res;
  } catch (error) {
    // Re-throw with a friendlier message
    throw new Error(friendlyError(error, "An unexpected error occurred."));
  }
}

type UnauthorizedBehavior = "returnNull" | "throw";
export const getQueryFn = <T,>({
  on401: unauthorizedBehavior,
}: {
  on401: UnauthorizedBehavior;
}): QueryFunction<T> => async ({ queryKey, signal }) => {
  try {
    const res = await fetchWithTimeout(apiUrl(queryKey.join("/") as string), {
      credentials: "include",
      cache: "no-store",
      signal,
    }, 15_000);

    if (unauthorizedBehavior === "returnNull" && res.status === 401) {
      return null as T;
    }

    await throwIfResNotOk(res);
    return (await res.json()) as T;
  } catch (error) {
    if (unauthorizedBehavior === "returnNull") {
      // Backend offline / not yet ready / CORS blocked: treat as empty and let the UI render.
      // The BackendOfflineBanner will handle showing the user a friendly message.
      return null as T;
    }
    // In strict mode surface the failure to React Query instead of silently returning null.
    throw error instanceof Error ? error : new Error(translateNow("Query failed"));
  }
};

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      queryFn: getQueryFn({ on401: "throw" }),
      refetchInterval: false,
      refetchOnWindowFocus: false,
      staleTime: Infinity,
      retry: false,
    },
    mutations: {
      retry: false,
    },
  },
});

