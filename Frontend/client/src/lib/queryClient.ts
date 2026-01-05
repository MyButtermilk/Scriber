import { QueryClient, QueryFunction } from "@tanstack/react-query";
import { apiUrl } from "./backend";

/**
 * Converts raw fetch errors into user-friendly messages
 */
function friendlyErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    const msg = error.message.toLowerCase();
    if (msg.includes("failed to fetch") || msg.includes("networkerror") || msg.includes("network request failed")) {
      return "Cannot connect to the backend. Please ensure the application is running.";
    }
    if (msg.includes("timeout") || msg.includes("aborted")) {
      return "Request timed out. The backend may be busy or unresponsive.";
    }
    if (msg.includes("cors") || msg.includes("cross-origin")) {
      return "Connection blocked. Please check your network settings.";
    }
    return error.message;
  }
  return "An unexpected error occurred";
}

async function throwIfResNotOk(res: Response) {
  if (!res.ok) {
    const text = (await res.text()) || res.statusText;
    throw new Error(`${res.status}: ${text}`);
  }
}

export async function apiRequest(
  method: string,
  url: string,
  data?: unknown | undefined,
): Promise<Response> {
  try {
    const res = await fetch(apiUrl(url), {
      method,
      headers: data ? { "Content-Type": "application/json" } : {},
      body: data ? JSON.stringify(data) : undefined,
      credentials: "include",
    });

    await throwIfResNotOk(res);
    return res;
  } catch (error) {
    // Re-throw with a friendlier message
    throw new Error(friendlyErrorMessage(error));
  }
}

type UnauthorizedBehavior = "returnNull" | "throw";
export const getQueryFn: <T>(options: {
  on401: UnauthorizedBehavior;
}) => QueryFunction<T> =
  ({ on401: unauthorizedBehavior }) =>
    async ({ queryKey }) => {
      try {
        const res = await fetch(apiUrl(queryKey.join("/") as string), {
          credentials: "include",
        });

        if (unauthorizedBehavior === "returnNull" && res.status === 401) {
          return null as T;
        }

        await throwIfResNotOk(res);
        return (await res.json()) as T;
      } catch {
        // Backend offline / not yet ready / CORS blocked: treat as empty and let the UI render.
        // The BackendOfflineBanner will handle showing the user a friendly message.
        return null as T;
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

