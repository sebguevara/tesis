import { NextResponse } from "next/server";

const BACKEND_API_URL = process.env.BACKEND_API_URL || "http://localhost:8000";

export async function GET(request: Request) {
  try {
    const url = new URL(request.url);
    const sourceId = (url.searchParams.get("source_id") || "").trim();
    const sessionId = (url.searchParams.get("session_id") || "").trim();
    if (!sourceId) {
      return NextResponse.json({ detail: "source_id es obligatorio." }, { status: 400 });
    }

    const backendUrl = new URL(`${BACKEND_API_URL}/api/query/history`);
    backendUrl.searchParams.set("source_id", sourceId);
    if (sessionId) backendUrl.searchParams.set("session_id", sessionId);
    backendUrl.searchParams.set("limit", "100");

    const response = await fetch(backendUrl.toString(), {
      method: "GET",
      cache: "no-store",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      return NextResponse.json(
        { detail: data?.detail || "No se pudo obtener el historial del chat." },
        { status: response.status },
      );
    }
    return NextResponse.json(data);
  } catch {
    return NextResponse.json(
      { detail: "No se pudo conectar con el backend de historial." },
      { status: 500 },
    );
  }
}
