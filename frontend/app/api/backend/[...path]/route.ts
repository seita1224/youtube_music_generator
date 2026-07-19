import { type NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/server/backend-proxy";
import { assertCsrfForUnsafe } from "@/lib/server/csrf";
import {
  SESSION_COOKIE_NAME,
  verifySessionToken,
} from "@/lib/server/session";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type RouteContext = { params: Promise<{ path?: string[] }> };

async function handle(
  request: NextRequest,
  context: RouteContext,
): Promise<Response> {
  const token = request.cookies.get(SESSION_COOKIE_NAME)?.value;
  const session = await verifySessionToken(token);
  if (!session) {
    return Response.json({ detail: "Unauthorized" }, { status: 401 });
  }

  const csrf = assertCsrfForUnsafe(request);
  if (csrf) {
    return csrf;
  }

  const { path } = await context.params;
  return proxyToBackend(request, path ?? []);
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
export const HEAD = handle;
export const OPTIONS = handle;
