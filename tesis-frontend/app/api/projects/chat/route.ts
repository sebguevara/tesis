import { NextResponse } from "next/server";

const BACKEND_API_URL = process.env.BACKEND_API_URL || "http://localhost:8000";

export async function POST(request: Request) {
  const controller = new AbortController();
  const timeoutMs = 28000;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const body = await request.json();
    const response = await fetch(`${BACKEND_API_URL}/api/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: controller.signal,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      return NextResponse.json(
        { detail: data?.detail || "No se pudo completar la consulta." },
        { status: response.status },
      );
    }
    return NextResponse.json(data);
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      return NextResponse.json(
        { detail: "La consulta tardó demasiado. Intenta con una pregunta más específica." },
        { status: 504 },
      );
    }
    return NextResponse.json(
      { detail: "No se pudo conectar con el backend de chat." },
      { status: 500 },
    );
  } finally {
    clearTimeout(timer);
  }
}
