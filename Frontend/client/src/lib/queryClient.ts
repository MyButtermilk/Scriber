import { QueryClient, QueryFunction } from "@tanstack/react-query";
import { apiUrl } from "./backend";
import { friendlyError, responseErrorMessage } from "./request-errors";

async function throwIfResNotOk(res: Response) {
  if (!res.ok) {
    throw new Error(await responseErrorMessage(res));
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
    throw new Error(friendlyError(error, "An unexpected error occurred."));
  }
}

type UnauthorizedBehavior = "returnNull" | "throw";
export const getQueryFn = <T,>({
  on401: unauthorizedBehavior,
}: {
  on401: UnauthorizedBehavior;
}): QueryFunction<T> => async ({ queryKey }) => {
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

