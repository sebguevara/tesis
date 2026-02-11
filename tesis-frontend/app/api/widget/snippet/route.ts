import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";

const BACKEND_API_URL = process.env.BACKEND_API_URL || "http://localhost:8000";
const WIDGET_QUERY_URL =
  process.env.NEXT_PUBLIC_WIDGET_QUERY_URL || `${BACKEND_API_URL}/api/widget/query`;

function normalizeDomainInput(value: string): string {
  const raw = (value || "").trim();
  if (!raw) return "";
  try {
    const url = new URL(raw);
    return url.hostname;
  } catch {
    return raw.replace(/^https?:\/\//i, "").split("/")[0] || "";
  }
}

export async function GET(request: Request) {
  try {
    const { userId } = await auth();
    if (!userId) {
      return NextResponse.json({ detail: "No autenticado." }, { status: 401 });
    }

    const url = new URL(request.url);
    const requestedSourceId = (url.searchParams.get("source_id") || "").trim();
    const requestedDomain = normalizeDomainInput(url.searchParams.get("domain") || "");

    const userResp = await fetch(
      `${BACKEND_API_URL}/api/auth/users/${encodeURIComponent(userId)}`,
      { method: "GET", cache: "no-store" },
    );
    const userData = await userResp.json().catch(() => ({}));
    if (!userResp.ok) {
      return NextResponse.json(
        { detail: userData?.detail || "No se pudo obtener credenciales del usuario." },
        { status: userResp.status },
      );
    }

    const apiKey = (userData?.user?.api_key || "").trim();
    if (!apiKey) {
      return NextResponse.json(
        { detail: "El usuario no tiene api_key disponible." },
        { status: 404 },
      );
    }

    let sourceId = requestedSourceId;
    if (!sourceId && requestedDomain) {
      const lookupResp = await fetch(
        `${BACKEND_API_URL}/api/sources/lookup?domain=${encodeURIComponent(requestedDomain)}`,
        { method: "GET", cache: "no-store" },
      );
      const lookupData = await lookupResp.json().catch(() => ({}));
      if (lookupResp.ok) {
        sourceId = (lookupData?.source_id || "").trim();
      }
    }

    if (!sourceId) {
      return NextResponse.json(
        { detail: "No se pudo resolver source_id para generar el snippet." },
        { status: 404 },
      );
    }

    return NextResponse.json({
      ok: true,
      source_id: sourceId,
      api_key: apiKey,
      widget_query_url: WIDGET_QUERY_URL,
    });
  } catch {
    return NextResponse.json(
      { detail: "No se pudo generar metadata del snippet." },
      { status: 500 },
    );
  }
}
