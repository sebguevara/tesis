import { NextResponse } from "next/server";

const BACKEND_API_URL = process.env.BACKEND_API_URL || "http://localhost:8000";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ jobId: string }> },
) {
  const { jobId } = await params;

  try {
    const response = await fetch(`${BACKEND_API_URL}/api/status/${jobId}/stream`, {
      method: "GET",
      cache: "no-store",
      headers: {
        Accept: "text/event-stream",
      },
    });

    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}));
      return NextResponse.json(
        { detail: data?.detail || "No se pudo abrir el stream de estado." },
        { status: response.status || 500 },
      );
    }

    return new Response(response.body, {
      status: 200,
      headers: {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
      },
    });
  } catch {
    return NextResponse.json(
      { detail: "No se pudo conectar con el backend de stream de estado." },
      { status: 500 },
    );
  }
}
